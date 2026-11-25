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
	"fmt"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

// heartbeatWriteSQL 复核期间写 binlog 的心跳语句：不设 sql_log_bin=0，产生行事件 + XID，
// 推动 DTS syncer 刷新 checkpoint（03 方案第 6.4 节）。
const heartbeatWriteSQL = "REPLACE INTO infodba_schema.check_heartbeat(uid) VALUE(@@server_id)"

// PreCheck 切换前预检（不加锁，03 方案第 6 节步骤 3 / 7.5 节）：
// 连通性、残留会话清理、source 地址比对、任务覆盖、长查询、心跳试写、SBM 轮询。
func (c *Comp) PreCheck(ctx context.Context) error {
	if err := c.Init(); err != nil {
		return err
	}
	p := c.Params

	// 1. 连通所有 Spider，并清理临时账号残留会话（A5）
	for _, ep := range p.SpiderEndpoints {
		worker, err := ep.connect()
		if err != nil {
			return fmt.Errorf("预检连接 Spider %s(%s) 失败: %w", ep.addr(), ep.Role, err)
		}
		if err = killResidualSessions(ctx, worker, ep); err != nil {
			worker.Close()
			return err
		}
		if err = checkNoLongQuery(ctx, worker, ep, p.LongQuerySec); err != nil {
			worker.Close()
			return err
		}
		worker.Close()
	}

	// 2. 连通所有分片 remote master，并试写一次心跳（无权限 / 表不存在 → 预检失败）
	for _, s := range p.ShardSources {
		worker, err := s.connect()
		if err != nil {
			return fmt.Errorf("预检连接分片 %s(%s) 失败: %w", s.SourceName, s.addr(), err)
		}
		wctx, cancel := context.WithTimeout(ctx, nodeOpTimeout)
		_, err = worker.Db.ExecContext(wctx, heartbeatWriteSQL)
		cancel()
		worker.Close()
		if err != nil {
			return fmt.Errorf("预检心跳试写失败 %s(%s): %w", s.SourceName, s.addr(), err)
		}
	}

	// 3. DTS source 实际地址与 shard_sources 一致（C1：分片主从切换后 source 连旧 master）
	for _, s := range p.ShardSources {
		cfg, err := dtsutil.FetchSourceConfig(p.DtsMasterAddr, s.SourceName)
		if err != nil {
			return err
		}
		if cfg.Host != s.Host || cfg.Port != s.Port {
			return fmt.Errorf(
				"source %s 实际连接 %s:%d 与入参分片 %s:%d 不一致（分片可能已发生主从切换），拒绝执行",
				s.SourceName, cfg.Host, cfg.Port, s.Host, s.Port,
			)
		}
	}

	// 4. 任务状态：报错 / 非 Running / 覆盖全部分片 / blocking_ddls / SBM==0（轮询，判定复用 dtsutil）
	if err := dtsutil.PollReadyBeforeLock(
		p.statusFetcher(ctx),
		p.PrecheckPollMax,
		ctxSleeper(ctx, dtsutil.PrecheckPollInterval),
		p.shardSourceCheck(),
	); err != nil {
		return fmt.Errorf("预检失败（未加锁）: %w", err)
	}
	logger.Info(
		"spider dts-cutover 预检通过: task=%s spiders=%d shards=%d", p.TaskName, len(p.SpiderEndpoints), len(p.ShardSources),
	)
	return nil
}

// killResidualSessions 清理临时账号在 Spider 上的残留会话（A5）：
// 上次进程被 SIGKILL 后 180 秒内重试，旧锁会话仍在；读锁与新锁共享，解锁后仍挡业务写入。
// 同一流程由上层 AcquireRunLock 串行化；按「同一 key 不会并发执行」的前提，这里清掉的会话都是上次执行残留。
func killResidualSessions(ctx context.Context, worker *native.DbWorker, ep SpiderEndpoint) error {
	q := `
SELECT ID
FROM information_schema.PROCESSLIST
WHERE USER = ? AND ID <> CONNECTION_ID()`
	rows, err := worker.Db.QueryContext(ctx, q, ep.User)
	if err != nil {
		return fmt.Errorf("查询 Spider %s 残留会话失败: %w", ep.addr(), err)
	}
	var ids []int64
	for rows.Next() {
		var id int64
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return fmt.Errorf("扫描残留会话 ID 失败: %w", err)
		}
		ids = append(ids, id)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("遍历残留会话失败: %w", err)
	}
	rows.Close()
	if len(ids) == 0 {
		return nil
	}
	for _, id := range ids {
		kctx, cancel := context.WithTimeout(ctx, nodeOpTimeout)
		_, kerr := worker.Db.ExecContext(kctx, fmt.Sprintf("KILL %d", id))
		cancel()
		if kerr != nil {
			return fmt.Errorf("KILL Spider %s 残留会话 %d 失败: %w", ep.addr(), id, kerr)
		}
		logger.Info("已清理 Spider %s 残留会话 id=%d user=%s", ep.addr(), id, ep.User)
	}
	return nil
}

// checkNoLongQuery 加锁前检查 Spider 上无超过 longQuerySec 的查询 / 未提交事务（A4 #4）。
// 长查询和空闲事务都会让 FLUSH TABLES ... WITH READ LOCK 长时间排队，卡住整个节点。
func checkNoLongQuery(ctx context.Context, worker *native.DbWorker, ep SpiderEndpoint, longQuerySec int) error {
	const q = `
SELECT COUNT(*)
FROM (
    SELECT ID
    FROM information_schema.PROCESSLIST
    WHERE COMMAND = 'Query' AND TIME > ? AND ID <> CONNECTION_ID()
    UNION
    SELECT trx_mysql_thread_id AS ID
    FROM information_schema.INNODB_TRX
    WHERE trx_mysql_thread_id <> CONNECTION_ID()
      AND TIMESTAMPDIFF(SECOND, trx_started, NOW()) > ?
) t`
	var n int
	if err := worker.Db.QueryRowContext(ctx, q, longQuerySec, longQuerySec).Scan(&n); err != nil {
		return fmt.Errorf("查询 Spider %s 长查询/长事务失败: %w", ep.addr(), err)
	}
	if n > 0 {
		return fmt.Errorf("Spider %s 存在 %d 个执行超过 %d 秒的查询或事务，暂不适合切换", ep.addr(), n, longQuerySec)
	}
	return nil
}

// resolveLockTables 在第一个 spider_master 上展开一次逻辑表清单（03 方案第 6 节步骤 4）；
// global 模式不用表清单加锁，但仍做展开与存在性校验，提前发现范围错误。
func (rt *runtime) resolveLockTables() error {
	p := rt.p
	firstMaster := rt.firstSpiderMaster()
	worker, err := firstMaster.connect()
	if err != nil {
		return fmt.Errorf("连接首个 spider_master %s 展开表清单失败: %w", firstMaster.addr(), err)
	}
	defer worker.Close()

	tables, err := dtsutil.ResolveTablesForPrecheck(worker.Db, p.SyncScope, p.LockTables)
	if err != nil {
		return fmt.Errorf("在 spider_master %s 展开表清单失败: %w", firstMaster.addr(), err)
	}
	rt.lockTables = tables
	logger.Info("表清单已展开（一次，所有 Spider 共用）tables=%d mode=%s", len(tables), p.LockMode)

	if p.LockMode == LockModeGlobal {
		return nil
	}
	// tables 模式：每个 Spider 上校验表存在与 FLUSH 语句长度（各自 max_allowed_packet）
	flushSQL, err := dtsutil.BuildFlushTablesReadLockSQL(tables)
	if err != nil {
		return err
	}
	for _, ep := range p.SpiderEndpoints {
		if err = verifySpiderLockable(ep, tables, flushSQL); err != nil {
			return err
		}
	}
	return nil
}

func verifySpiderLockable(ep SpiderEndpoint, tables []dtsutil.LockedTable, flushSQL string) error {
	w, err := ep.connect()
	if err != nil {
		return fmt.Errorf("连接 Spider %s 校验表清单失败: %w", ep.addr(), err)
	}
	defer w.Close()
	if err = dtsutil.VerifyBaseTablesExist(w.Db, tables); err != nil {
		return fmt.Errorf("Spider %s 校验表存在失败: %w", ep.addr(), err)
	}
	if err = dtsutil.CheckFlushSQLBudget(w.Db, flushSQL); err != nil {
		return fmt.Errorf("Spider %s: %w", ep.addr(), err)
	}
	return nil
}
