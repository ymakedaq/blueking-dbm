/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

// Package dts_cutover 在 DTS Master 主机上执行 TenDBCluster DTS 安全切换（spider dts-cutover）：
//
// 预检（不加锁）→ 锁定源集群所有 SPIDER_MASTER + SPIDER_MNT（整轮全有或全无）→ 并行取各分片 remote master
// 位点快照 → 持锁复核（心跳写入推动 checkpoint 刷新）→ 停任务并收敛（三值判定）→ 位点文件落盘 → 解锁。
//
// 与 mysql dts-cutover 的区别：加锁对象是 Spider 接入层而非源表；快照直接查各分片 remote master，
// 不用 DTS 缓存的 master_binlog；依赖上层 AcquireRunLock 串行化与位点文件状态机，支持幂等重试；
// 不改现有 mysql 命令的行为。
// 任务状态判定、轮询、表清单展开、FLUSH 加锁原语、Master OpenAPI 均复用 pkg/util/dtsutil。
package dts_cutover

import (
	"context"
	"fmt"
	"strings"
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/components"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

const (
	// LockModeTables 逐表读锁（默认）
	LockModeTables = "tables"
	// LockModeGlobal 全局读锁（兜底）
	LockModeGlobal = "global"

	roleSpiderMaster = "spider_master"

	// 轮询默认值（预检 / 复核次数）复用 dtsutil.Default*，以下为 spider 特有
	defaultLockWaitTimeoutSec = 10
	defaultLockRetry          = 3
	defaultLongQuerySec       = 10
	defaultStopConvergeSec    = 15
	defaultSessionWaitTimeout = 180
	defaultTickIntervalMs     = 1000

	// maxLockHeldSec 持锁总时长上限（03 方案第 8 节），session_wait_timeout 必须大于它
	maxLockHeldSec = 90

	lockRetryInterval = 2 * time.Second
	// nodeOpTimeout 解锁 / 存活检查等收尾动作的单节点超时（不复用主 ctx）
	nodeOpTimeout = 5 * time.Second
)

// Comp TenDBCluster DTS cutover 组件。
type Comp struct {
	GeneralParam *components.GeneralParam `json:"general"`
	Params       *Params                  `json:"extend"`
	// Ctx 由命令入口注入（signal.NotifyContext）；nil 时退化为 Background
	Ctx context.Context `json:"-"`
}

// Params Flow → actuator payload（与 02 方案第 6.3 节 payload 一一对应）。
type Params struct {
	DtsMasterAddr   string              `json:"dts_master_addr" validate:"required"`
	DeployPath      string              `json:"deploy_path" validate:"required"` // 位点文件目录基准
	TaskName        string              `json:"task_name" validate:"required"`
	SpiderEndpoints []SpiderEndpoint    `json:"spider_endpoints" validate:"required,gt=0,dive"`
	ShardSources    []ShardSource       `json:"shard_sources" validate:"required,gt=0,dive"`
	SyncScope       *dtsutil.SyncScope  `json:"sync_scope"`
	LockTables      []dtsutil.TableItem `json:"lock_tables"`
	// LockMode tables(默认) | global
	LockMode string `json:"lock_mode"`
	// LockWaitTimeoutSec 服务端等锁上限，默认 10
	LockWaitTimeoutSec int `json:"lock_wait_timeout_sec"`
	// LockRetry 整轮加锁重试次数，默认 3
	LockRetry int `json:"lock_retry"`
	// LongQuerySec 长查询阈值（秒），默认 10
	LongQuerySec int `json:"long_query_sec"`
	// PrecheckPollMax 预检轮询次数（1s 间隔），默认 30
	PrecheckPollMax int `json:"precheck_poll_max"`
	// CatchupPollMax 持锁复核最大次数（0.5s 间隔），默认 60
	CatchupPollMax int `json:"catchup_poll_max"`
	// CatchupRecheck 连续通过次数，默认 3
	CatchupRecheck int `json:"catchup_recheck"`
	// StopConvergeSec 停任务出错后持锁收敛上限（秒），默认 15
	StopConvergeSec int `json:"stop_converge_sec"`
	// SessionWaitTimeout 锁连接 wait_timeout 兜底（秒），默认 180
	SessionWaitTimeout int `json:"session_wait_timeout"`
	// TickIntervalMs 复核期间心跳写入间隔（毫秒），默认 1000
	TickIntervalMs int `json:"tick_interval_ms"`
	// PositionFileKey 位点文件键 root_id_task_name
	PositionFileKey string `json:"position_file_key" validate:"required"`
}

// SpiderEndpoint 源集群 Spider 节点连接信息（临时账号）。
type SpiderEndpoint struct {
	Host     string `json:"host" validate:"required"`
	Port     int    `json:"port" validate:"required,gt=0"`
	Role     string `json:"role" validate:"required,oneof=spider_master spider_mnt"`
	User     string `json:"user" validate:"required"`
	Password string `json:"password" validate:"required"`
}

// ShardSource 单个分片：source 名 + 该分片 remote master（DTS 拉 binlog 的节点）。
type ShardSource struct {
	SourceName string `json:"source_name" validate:"required"`
	ShardID    int    `json:"shard_id" validate:"gte=0"`
	Host       string `json:"host" validate:"required"`
	Port       int    `json:"port" validate:"required,gt=0"`
	User       string `json:"user" validate:"required"`
	Password   string `json:"password" validate:"required"`
}

// NodeLockResult 单个 Spider 节点的加锁 / 解锁结果。
type NodeLockResult struct {
	Host               string `json:"host"`
	Port               int    `json:"port"`
	Role               string `json:"role"`
	Locked             bool   `json:"locked"`
	LockErr            string `json:"lock_err,omitempty"`
	Killed             bool   `json:"killed"` // KILL QUERY 结束过挂起的 FLUSH
	UnlockOK           bool   `json:"unlock_ok"`
	ReleaseUnconfirmed bool   `json:"release_unconfirmed"`
	UnlockErr          string `json:"unlock_err,omitempty"`
}

// SpiderCutoverOutput 供 Flow 落库的输出；沿用 mysql cutover 位点结构并追加锁信息。
type SpiderCutoverOutput struct {
	dtsutil.CutoverPositionOutput
	LockMode     string                         `json:"lock_mode"`
	LockSnapshot map[string]dtsutil.BinlogCoord `json:"lock_snapshot"`
	// FromFile 本次输出由幂等检查直接读位点文件返回，未重新加锁
	FromFile   bool             `json:"from_file"`
	LockHeldMs int64            `json:"lock_held_ms"`
	Nodes      []NodeLockResult `json:"nodes"`
}

// Example payload 示例。
func (c *Comp) Example() interface{} {
	return Comp{
		Params: &Params{
			DtsMasterAddr:   "127.0.0.2:18301",
			DeployPath:      "/data/dts/demo",
			TaskName:        "task-a",
			PositionFileKey: "20260101120000_20260101120000-1_task-a",
			SpiderEndpoints: []SpiderEndpoint{
				{Host: "127.0.0.11", Port: 25000, Role: "spider_master", User: "u", Password: "p"},
				{Host: "127.0.0.12", Port: 25000, Role: "spider_mnt", User: "u", Password: "p"},
			},
			ShardSources: []ShardSource{
				{SourceName: "task-a-s0", ShardID: 0, Host: "127.0.0.21", Port: 20000, User: "u", Password: "p"},
				{SourceName: "task-a-s1", ShardID: 1, Host: "127.0.0.22", Port: 20000, User: "u", Password: "p"},
			},
			SyncScope: &dtsutil.SyncScope{
				DoDBs:    []string{"app"},
				DoTables: []dtsutil.TableItem{{Schema: "*", Table: "*"}},
			},
			LockMode:           LockModeTables,
			LockWaitTimeoutSec: defaultLockWaitTimeoutSec,
			LockRetry:          defaultLockRetry,
			LongQuerySec:       defaultLongQuerySec,
			PrecheckPollMax:    dtsutil.DefaultPrecheckPollMax,
			CatchupPollMax:     dtsutil.DefaultCatchupPollMax,
			CatchupRecheck:     dtsutil.DefaultCatchupRecheck,
			StopConvergeSec:    defaultStopConvergeSec,
			SessionWaitTimeout: defaultSessionWaitTimeout,
			TickIntervalMs:     defaultTickIntervalMs,
		},
	}
}

// positiveOr 正数原样返回，否则返回默认值（payload 缺省字段回填）。
func positiveOr(v, def int) int {
	if v > 0 {
		return v
	}
	return def
}

// tickInterval 心跳写入间隔（Init 已回填默认值）。
func (p *Params) tickInterval() time.Duration {
	return time.Duration(positiveOr(p.TickIntervalMs, defaultTickIntervalMs)) * time.Millisecond
}

// ctx 返回注入的 ctx（未注入时 Background）。
func (c *Comp) ctx() context.Context {
	if c.Ctx != nil {
		return c.Ctx
	}
	return context.Background()
}

func (ep SpiderEndpoint) addr() string {
	return fmt.Sprintf("%s:%d", ep.Host, ep.Port)
}

func (ep SpiderEndpoint) connect() (*native.DbWorker, error) {
	return native.InsObject{Host: ep.Host, Port: ep.Port, User: ep.User, Pwd: ep.Password}.Conn()
}

func (s ShardSource) addr() string {
	return fmt.Sprintf("%s:%d", s.Host, s.Port)
}

func (s ShardSource) connect() (*native.DbWorker, error) {
	return native.InsObject{Host: s.Host, Port: s.Port, User: s.User, Pwd: s.Password}.Conn()
}

// Init 参数校验与默认值回填（03 方案第 5 节）。
func (c *Comp) Init() error {
	if c.Params == nil {
		return fmt.Errorf("params 为空")
	}
	p := c.Params
	for _, field := range []struct {
		name, value string
	}{
		{"task_name", p.TaskName},
		{"dts_master_addr", p.DtsMasterAddr},
		{"deploy_path", p.DeployPath},
		{"position_file_key", p.PositionFileKey},
	} {
		if strings.TrimSpace(field.value) == "" {
			return fmt.Errorf("%s 为空", field.name)
		}
	}
	if len(p.SpiderEndpoints) == 0 {
		return fmt.Errorf("spider_endpoints 为空")
	}
	if len(p.ShardSources) == 0 {
		return fmt.Errorf("shard_sources 为空")
	}

	hasMaster := false
	seenSpider := make(map[string]struct{}, len(p.SpiderEndpoints))
	for _, ep := range p.SpiderEndpoints {
		key := ep.addr()
		if _, ok := seenSpider[key]; ok {
			return fmt.Errorf("spider_endpoints 重复地址 %s", key)
		}
		seenSpider[key] = struct{}{}
		if ep.Role == roleSpiderMaster {
			hasMaster = true
		}
	}
	if !hasMaster {
		return fmt.Errorf("spider_endpoints 至少需要一个 spider_master")
	}

	seenShard := make(map[int]struct{}, len(p.ShardSources))
	seenSource := make(map[string]struct{}, len(p.ShardSources))
	for _, s := range p.ShardSources {
		if _, ok := seenShard[s.ShardID]; ok {
			return fmt.Errorf("shard_sources 重复 shard_id=%d", s.ShardID)
		}
		seenShard[s.ShardID] = struct{}{}
		if _, ok := seenSource[s.SourceName]; ok {
			return fmt.Errorf("shard_sources 重复 source_name=%s", s.SourceName)
		}
		seenSource[s.SourceName] = struct{}{}
	}
	for i := 0; i < len(p.ShardSources); i++ {
		if _, ok := seenShard[i]; !ok {
			return fmt.Errorf("shard_id 必须从 0 连续，缺少 shard_id=%d", i)
		}
	}

	if p.LockMode == "" {
		p.LockMode = LockModeTables
	}
	if p.LockMode != LockModeTables && p.LockMode != LockModeGlobal {
		return fmt.Errorf("lock_mode 只允许 tables/global，当前为 %q", p.LockMode)
	}
	if p.LockMode == LockModeTables && len(p.LockTables) == 0 && (p.SyncScope == nil || p.SyncScope.IsEmpty()) {
		return fmt.Errorf("lock_mode=tables 时 sync_scope 与 lock_tables 不能同时为空（禁止无清单加锁）")
	}

	p.LockWaitTimeoutSec = positiveOr(p.LockWaitTimeoutSec, defaultLockWaitTimeoutSec)
	p.LockRetry = positiveOr(p.LockRetry, defaultLockRetry)
	p.LongQuerySec = positiveOr(p.LongQuerySec, defaultLongQuerySec)
	p.PrecheckPollMax = positiveOr(p.PrecheckPollMax, dtsutil.DefaultPrecheckPollMax)
	p.CatchupPollMax = positiveOr(p.CatchupPollMax, dtsutil.DefaultCatchupPollMax)
	p.CatchupRecheck = positiveOr(p.CatchupRecheck, dtsutil.DefaultCatchupRecheck)
	if p.CatchupPollMax < p.CatchupRecheck {
		p.CatchupPollMax = p.CatchupRecheck
	}
	p.StopConvergeSec = positiveOr(p.StopConvergeSec, defaultStopConvergeSec)
	p.SessionWaitTimeout = positiveOr(p.SessionWaitTimeout, defaultSessionWaitTimeout)
	p.TickIntervalMs = positiveOr(p.TickIntervalMs, defaultTickIntervalMs)
	if p.SessionWaitTimeout <= maxLockHeldSec {
		return fmt.Errorf(
			"session_wait_timeout(%d) 必须大于持锁时长上限 %d 秒", p.SessionWaitTimeout, maxLockHeldSec,
		)
	}
	return nil
}

// Run 切换主路径（03 方案第 6 节）。
func (c *Comp) Run() error {
	if err := c.Init(); err != nil {
		return err
	}
	p := c.Params
	ctx := c.ctx()

	logger.Info(
		"spider dts-cutover 开始 task=%s spiders=%d shards=%d lock_mode=%s key=%s",
		p.TaskName, len(p.SpiderEndpoints), len(p.ShardSources), p.LockMode, p.PositionFileKey,
	)

	// 1. 幂等检查：全部 Stopped 且位点文件 stopped → 直接输出；其余按状态表
	decision, err := c.checkIdempotent(ctx)
	if err != nil {
		return err
	}
	if decision.action == idempotentDirectOutput {
		logger.Info("幂等检查：任务已全部停止且位点文件为 stopped，直接输出（不重新加锁）")
		return components.PrintOutputCtx(decision.output)
	}

	// 2. 预检（不加锁）
	if err = c.PreCheck(ctx); err != nil {
		return err
	}

	rt, err := newRuntime(ctx, p)
	if err != nil {
		return err
	}
	defer rt.close()

	// 3. 展开表清单（一次），并逐 Spider 校验存在性与 FLUSH 预算
	if err = rt.resolveLockTables(); err != nil {
		return err
	}

	// 4. 并行加锁（整轮全有或全无，最多 lock_retry 轮）
	if err = rt.lockAllSpiders(); err != nil {
		return err
	}
	rt.markLockAcquired()

	// 5. 各分片 remote master 快照 + 心跳写入
	snapshots, err := rt.snapshotShards()
	if err != nil {
		rt.unlockAllSpiders()
		return err
	}
	rt.ticker = startBinlogTicker(ctx, rt.shardWorkers, p.tickInterval())
	defer rt.ticker.stop()

	// 6. 持锁复核：syncer ≥ 快照 且 SBM=0 且无 blocking_ddls，连续 catchup_recheck 次
	finalItems, err := rt.pollCatchup(snapshots)
	rt.ticker.stop()
	if err != nil {
		rt.unlockAllSpiders()
		return err
	}

	// 7. 存活检查 → stopping → 停任务收敛 → 存活检查 → stopped
	if err = rt.checkLocksAlive(); err != nil {
		rt.unlockAllSpiders()
		return err
	}
	posFile := newPositionFile(p, snapshots, finalItems)
	if err = rt.writePosition(posFile, PositionStateStopping); err != nil {
		rt.unlockAllSpiders()
		return err
	}
	outcome, convergeItems, stopErr := stopAndConverge(p)
	if outcome == stopOutcomeDefiniteFail {
		_ = removePositionFile(positionFilePath(p.DeployPath, p.PositionFileKey))
		rt.unlockAllSpiders()
		return fmt.Errorf("停任务明确失败（任务仍 Running，可直接重试）: %w", stopErr)
	}
	if outcome == stopOutcomeUnknown {
		rt.unlockAllSpiders()
		return fmt.Errorf(
			"停任务结果未知（位点文件保留 stopping，重试时按状态表收敛）: %w", stopErr,
		)
	}

	finalPositions := convergeItems
	if len(finalPositions) == 0 {
		finalPositions = finalItems
	}
	posFile.Sources = dtsutil.BuildPositionOutput(p.TaskName, finalPositions).Sources
	overrideMasterBinlog(posFile.Sources, snapshots)

	aliveErr := rt.checkLocksAlive()
	targetState := PositionStateStopped
	if aliveErr != nil {
		targetState = PositionStateInconsistent
	}
	if werr := rt.writePosition(posFile, targetState); werr != nil {
		logger.Error("写位点文件 %s 失败: %s", targetState, werr.Error())
	}

	rt.unlockAllSpiders()
	rt.finalizePositionFile(posFile)

	if aliveErr != nil {
		return fmt.Errorf("停任务后锁连接存活检查失败，一致性不成立（位点文件已写 inconsistent）: %w", aliveErr)
	}

	out := buildOutput(p, snapshots, finalPositions, rt.lockHeldMs(), rt.nodeResults(), false)
	if err = components.PrintOutputCtx(out); err != nil {
		return fmt.Errorf("输出位点 JSON 失败: %w", err)
	}
	logger.Info(
		"spider dts-cutover 完成: task=%s shards=%d lock_held_ms=%d", p.TaskName, len(p.ShardSources), out.LockHeldMs,
	)
	return nil
}

// buildOutput 组装输出：sources[].master_binlog 用加锁快照覆盖 DTS 缓存值。
func buildOutput(
	p *Params,
	snapshots map[string]dtsutil.BinlogCoord,
	items []dtsutil.TaskStatusItem,
	lockHeldMs int64,
	nodes []NodeLockResult,
	fromFile bool,
) *SpiderCutoverOutput {
	base := dtsutil.BuildPositionOutput(p.TaskName, items)
	overrideMasterBinlog(base.Sources, snapshots)
	return &SpiderCutoverOutput{
		CutoverPositionOutput: base,
		LockMode:              p.LockMode,
		LockSnapshot:          snapshots,
		FromFile:              fromFile,
		LockHeldMs:            lockHeldMs,
		Nodes:                 nodes,
	}
}

// outputFromPositionFile 幂等重试直接输出：与首次成功输出同构（master_binlog 同样用加锁快照覆盖）。
func outputFromPositionFile(p *Params, pf *PositionFile) *SpiderCutoverOutput {
	sources := make([]dtsutil.SourcePositionSnapshot, len(pf.Sources))
	copy(sources, pf.Sources)
	overrideMasterBinlog(sources, pf.LockSnapshot)
	return &SpiderCutoverOutput{
		CutoverPositionOutput: dtsutil.CutoverPositionOutput{
			TaskName:  p.TaskName,
			Sources:   sources,
			StoppedAt: pf.StoppedConfirmedAt,
		},
		LockMode:     pf.LockMode,
		LockSnapshot: pf.LockSnapshot,
		FromFile:     true,
		Nodes:        pf.Nodes,
	}
}

func overrideMasterBinlog(sources []dtsutil.SourcePositionSnapshot, snapshots map[string]dtsutil.BinlogCoord) {
	for i := range sources {
		if coord, ok := snapshots[sources[i].SourceName]; ok {
			sources[i].MasterBinlog = fmt.Sprintf("(%s, %d)", coord.File, coord.Position)
		}
	}
}
