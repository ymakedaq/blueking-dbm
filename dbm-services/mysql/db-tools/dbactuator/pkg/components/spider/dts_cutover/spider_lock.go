/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package dts_cutover

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"sync"
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

// spiderLockNode 单个 Spider 节点的锁管理状态。
type spiderLockNode struct {
	ep       SpiderEndpoint
	worker   *native.DbWorker // 管理连接池（KILL QUERY 等）
	lockConn *sql.Conn        // 专用持锁连接，禁止走连接池
	threadID int64
	result   NodeLockResult
}

// runtime 单次切换的运行时状态。
type runtime struct {
	ctx          context.Context
	p            *Params
	spiderNodes  []*spiderLockNode
	shardWorkers map[string]*native.DbWorker // source_name → 分片 remote master
	lockTables   []dtsutil.LockedTable
	lockAcquired time.Time
	lockReleased time.Time
	ticker       *binlogTicker
}

func newRuntime(ctx context.Context, p *Params) (*runtime, error) {
	rt := &runtime{ctx: ctx, p: p, shardWorkers: make(map[string]*native.DbWorker, len(p.ShardSources))}
	for _, s := range p.ShardSources {
		worker, err := s.connect()
		if err != nil {
			rt.close()
			return nil, fmt.Errorf("连接分片 %s(%s) 失败: %w", s.SourceName, s.addr(), err)
		}
		rt.shardWorkers[s.SourceName] = worker
	}
	return rt, nil
}

// close 释放所有连接（解锁应已由调用方先执行）。
func (rt *runtime) close() {
	for _, node := range rt.spiderNodes {
		if node == nil {
			continue
		}
		if node.lockConn != nil {
			_ = node.lockConn.Close()
			node.lockConn = nil
		}
		if node.worker != nil {
			node.worker.Close()
			node.worker = nil
		}
	}
	for name, worker := range rt.shardWorkers {
		if worker != nil {
			worker.Close()
		}
		delete(rt.shardWorkers, name)
	}
}

func (rt *runtime) firstSpiderMaster() SpiderEndpoint {
	for _, ep := range rt.p.SpiderEndpoints {
		if ep.Role == roleSpiderMaster {
			return ep
		}
	}
	return rt.p.SpiderEndpoints[0]
}

func (rt *runtime) markLockAcquired() {
	rt.lockAcquired = time.Now()
}

func (rt *runtime) lockHeldMs() int64 {
	if rt.lockAcquired.IsZero() {
		return 0
	}
	end := rt.lockReleased
	if end.IsZero() {
		end = time.Now()
	}
	return end.Sub(rt.lockAcquired).Milliseconds()
}

func (rt *runtime) nodeResults() []NodeLockResult {
	out := make([]NodeLockResult, 0, len(rt.spiderNodes))
	for _, node := range rt.spiderNodes {
		out = append(out, node.result)
	}
	return out
}

// lockAllSpiders 并行锁定所有 Spider（03 方案第 7.1 节）：
// 持锁节点集合必须原子——整轮全有或全无，最多 lock_retry 轮，间隔 2 秒。
func (rt *runtime) lockAllSpiders() error {
	var lastErr error
	for round := 1; round <= rt.p.LockRetry; round++ {
		if round > 1 {
			// 上一轮存在“释放未确认”的节点时不再重试：无法确认旧锁是否还在
			if hasUnconfirmedRelease(lastErr) {
				return fmt.Errorf("上一轮存在释放未确认的节点，不再重试: %w", lastErr)
			}
			// 重试前快速检查长查询（只查不轮询）：阻塞源仍在时直接失败，不浪费重试次数
			if qerr := rt.quickCheckLongQueries(); qerr != nil {
				lastErr = qerr
				logger.Warn("第 %d 轮重试前长查询检查未通过: %s", round, qerr.Error())
				if round < rt.p.LockRetry {
					time.Sleep(lockRetryInterval)
				}
				continue
			}
		}
		nodes, err := rt.lockOneRound()
		if err == nil {
			rt.spiderNodes = nodes
			logger.Info("全部 Spider 加锁成功 round=%d nodes=%d", round, len(nodes))
			return nil
		}
		lastErr = err
		logger.Warn("第 %d/%d 轮加锁失败: %s", round, rt.p.LockRetry, err.Error())
		if hasUnconfirmedRelease(err) {
			return fmt.Errorf("存在释放未确认的节点，不再重试: %w", err)
		}
		if round < rt.p.LockRetry {
			time.Sleep(lockRetryInterval)
		}
	}
	return fmt.Errorf("整轮加锁 %d 轮均失败（任务保持 Running，位点文件不产生）: %w", rt.p.LockRetry, lastErr)
}

// quickCheckLongQueries 每轮重试前的快速长查询检查（只查不轮询）。
func (rt *runtime) quickCheckLongQueries() error {
	for _, ep := range rt.p.SpiderEndpoints {
		worker, err := ep.connect()
		if err != nil {
			return fmt.Errorf("连接 Spider %s 失败: %w", ep.addr(), err)
		}
		qctx, cancel := context.WithTimeout(rt.ctx, nodeOpTimeout)
		err = checkNoLongQuery(qctx, worker, ep, rt.p.LongQuerySec)
		cancel()
		worker.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// lockOneRound 一轮并行加锁：任一节点失败立即取消整轮 ctx，随后全部回滚。
func (rt *runtime) lockOneRound() ([]*spiderLockNode, error) {
	p := rt.p
	roundCtx, cancel := context.WithCancel(rt.ctx)
	defer cancel()

	nodes := make([]*spiderLockNode, len(p.SpiderEndpoints))
	var wg sync.WaitGroup
	var mu sync.Mutex
	firstErrIdx := -1
	for i, ep := range p.SpiderEndpoints {
		nodes[i] = &spiderLockNode{
			ep:     ep,
			result: NodeLockResult{Host: ep.Host, Port: ep.Port, Role: ep.Role},
		}
		wg.Add(1)
		go func(node *spiderLockNode, idx int) {
			defer wg.Done()
			if lerr := rt.lockOneNode(roundCtx, node); lerr != nil {
				node.result.LockErr = lerr.Error()
				mu.Lock()
				if firstErrIdx < 0 || idx < firstErrIdx {
					firstErrIdx = idx
					// 取消整轮：不让其他节点各自跑满 lock_wait_timeout
					cancel()
				}
				mu.Unlock()
			}
		}(nodes[i], i)
	}
	wg.Wait()

	if firstErrIdx >= 0 {
		rt.rollbackRound(nodes)
		err := fmt.Errorf("加锁失败节点: %s", summarizeLockErrors(nodes))
		if hasUnconfirmedNode(nodes) {
			err = fmt.Errorf("%s；且存在释放未确认的节点", err.Error())
		}
		return nil, err
	}
	return nodes, nil
}

// lockOneNode 单节点加锁序列（固定在一条 *sql.Conn 上执行，禁止用连接池）：
// SET SESSION wait_timeout → SELECT CONNECTION_ID() → dtsutil.FlushReadLock（SET lock_wait_timeout + FLUSH ... WITH READ LOCK）。
func (rt *runtime) lockOneNode(roundCtx context.Context, node *spiderLockNode) error {
	p := rt.p
	worker, err := node.ep.connect()
	if err != nil {
		node.worker = worker
		return fmt.Errorf("连接失败: %w", err)
	}
	node.worker = worker

	conn, err := worker.Db.Conn(roundCtx)
	if err != nil {
		return fmt.Errorf("获取持锁连接失败: %w", err)
	}
	node.lockConn = conn

	setupCtx, setupCancel := context.WithTimeout(roundCtx, nodeOpTimeout)
	defer setupCancel()
	waitSQL := fmt.Sprintf("SET SESSION wait_timeout = %d", p.SessionWaitTimeout)
	if _, err = conn.ExecContext(setupCtx, waitSQL); err != nil {
		return fmt.Errorf("设置 wait_timeout 失败: %w", err)
	}
	// 先拿 thread_id：FLUSH 挂起时回滚要用它 KILL QUERY
	if err = conn.QueryRowContext(setupCtx, "SELECT CONNECTION_ID()").Scan(&node.threadID); err != nil {
		return fmt.Errorf("获取 CONNECTION_ID 失败: %w", err)
	}

	flushSQL, ferr := rt.buildFlushSQL()
	if ferr != nil {
		return ferr
	}
	if err = dtsutil.FlushReadLock(roundCtx, conn, flushSQL, p.LockWaitTimeoutSec); err != nil {
		return fmt.Errorf("FLUSH 失败: %w", err)
	}
	node.result.Locked = true
	logger.Info(
		"Spider %s(%s) 加锁成功 thread_id=%d tables=%d", node.ep.addr(), node.ep.Role, node.threadID, len(rt.lockTables),
	)
	return nil
}

func (rt *runtime) buildFlushSQL() (string, error) {
	if rt.p.LockMode == LockModeGlobal {
		return "FLUSH TABLES WITH READ LOCK", nil
	}
	return dtsutil.BuildFlushTablesReadLockSQL(rt.lockTables)
}

// rollbackRound 整轮回滚（03 方案第 7.1 节步骤 3-6）：
// 回滚范围是全部连接，不只是“报告成功”的节点——取消与 FLUSH 成功之间存在竞态。
func (rt *runtime) rollbackRound(nodes []*spiderLockNode) {
	for _, node := range nodes {
		rt.killPendingFlush(node)
		rt.releaseNode(node)
	}
}

// killPendingFlush 用管理连接结束仍在服务端排队 / 执行的 FLUSH。
// KILL QUERY 同一用户自己的线程，无需额外权限；对已成功持锁（空闲）节点是 no-op，也跳过。
func (rt *runtime) killPendingFlush(node *spiderLockNode) {
	if node.result.Locked || node.threadID == 0 || node.worker == nil {
		return
	}
	kctx, kcancel := context.WithTimeout(context.Background(), nodeOpTimeout)
	defer kcancel()
	if _, err := node.worker.Db.ExecContext(kctx, fmt.Sprintf("KILL QUERY %d", node.threadID)); err != nil {
		logger.Warn("KILL QUERY %d 于 %s 失败: %s", node.threadID, node.ep.addr(), err.Error())
		return
	}
	node.result.Killed = true
	logger.Info("已 KILL QUERY Spider %s 挂起的 FLUSH thread_id=%d", node.ep.addr(), node.threadID)
}

// releaseNode UNLOCK TABLES（独立 ctx 5 秒）后关闭连接；都失败则标记“释放未确认”。
func (rt *runtime) releaseNode(node *spiderLockNode) {
	if node.lockConn != nil {
		uctx, ucancel := context.WithTimeout(context.Background(), nodeOpTimeout)
		err := dtsutil.UnlockTables(uctx, node.lockConn)
		ucancel()
		if err == nil {
			node.result.UnlockOK = true
		} else {
			node.result.UnlockErr = err.Error()
		}
		// 连接关闭后服务端会话结束，锁一定释放
		if cerr := node.lockConn.Close(); cerr != nil && err != nil {
			node.result.ReleaseUnconfirmed = true
			logger.Error("Spider %s UNLOCK 与关闭连接均失败，释放未确认: unlock=%s close=%s",
				node.ep.addr(), err.Error(), cerr.Error())
		}
		node.lockConn = nil
	}
	if node.worker != nil {
		node.worker.Close()
		node.worker = nil
	}
}

// unlockAllSpiders 逆序解锁（03 方案第 7.2 节 A3/A4）：固定 Background ctx，不复用主 ctx。
func (rt *runtime) unlockAllSpiders() {
	for i := len(rt.spiderNodes) - 1; i >= 0; i-- {
		node := rt.spiderNodes[i]
		if node == nil {
			continue
		}
		rt.releaseNode(node)
		if node.result.UnlockOK {
			logger.Info("Spider %s(%s) 已 UNLOCK TABLES", node.ep.addr(), node.ep.Role)
		}
	}
	rt.lockReleased = time.Now()
}

// checkLocksAlive 锁连接存活检查：SELECT 1，任一失败即失败（A1）。
func (rt *runtime) checkLocksAlive() error {
	var mu sync.Mutex
	var errs []string
	var wg sync.WaitGroup
	for _, node := range rt.spiderNodes {
		if node == nil || node.lockConn == nil {
			continue
		}
		wg.Add(1)
		go func(node *spiderLockNode) {
			defer wg.Done()
			ectx, ecancel := context.WithTimeout(context.Background(), nodeOpTimeout)
			defer ecancel()
			if _, err := node.lockConn.ExecContext(ectx, "SELECT 1"); err != nil {
				mu.Lock()
				errs = append(errs, fmt.Sprintf("%s: %s", node.ep.addr(), err.Error()))
				mu.Unlock()
			}
		}(node)
	}
	wg.Wait()
	if len(errs) > 0 {
		return fmt.Errorf("锁连接存活检查失败（锁可能已丢失）: %s", strings.Join(errs, "; "))
	}
	return nil
}

func summarizeLockErrors(nodes []*spiderLockNode) string {
	var msgs []string
	for _, node := range nodes {
		if node.result.LockErr != "" {
			msgs = append(msgs, fmt.Sprintf("%s(%s): %s", node.ep.addr(), node.ep.Role, node.result.LockErr))
		}
	}
	if len(msgs) == 0 {
		return "未知错误"
	}
	return strings.Join(msgs, "; ")
}

func hasUnconfirmedNode(nodes []*spiderLockNode) bool {
	for _, node := range nodes {
		if node != nil && node.result.ReleaseUnconfirmed {
			return true
		}
	}
	return false
}

func hasUnconfirmedRelease(err error) bool {
	return err != nil && strings.Contains(err.Error(), "释放未确认")
}
