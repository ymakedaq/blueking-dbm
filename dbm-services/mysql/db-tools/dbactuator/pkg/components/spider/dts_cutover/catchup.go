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
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

// pollCatchup 持锁复核（03 方案第 6 节步骤 7），判定复用 dtsutil.PollUntilCaughtUp：
// 每个分片 syncer ≥ 快照 且 SBM=0 且无 blocking_ddls，连续 catchupRecheck 次；
// 附加：返回的 source 必须恰好覆盖全部分片（禁止子集追平假成功）；收到 SIGTERM/SIGINT 尽快退出。
// 失败时解锁退出，不停任务。
func (rt *runtime) pollCatchup(snapshots map[string]dtsutil.BinlogCoord) ([]dtsutil.TaskStatusItem, error) {
	p := rt.p
	items, err := dtsutil.PollUntilCaughtUp(
		p.statusFetcher(rt.ctx),
		snapshots,
		p.CatchupRecheck,
		p.CatchupPollMax,
		ctxSleeper(rt.ctx, dtsutil.CatchupPollInterval),
		p.shardSourceCheck(),
	)
	if err != nil {
		return nil, fmt.Errorf("持锁复核失败（不执行 stop）: %w", err)
	}
	return items, nil
}

// statusFetcher 查询本次任务状态；ctx 已取消时直接返回取消错误，让轮询立即终止。
func (p *Params) statusFetcher(ctx context.Context) func() ([]dtsutil.TaskStatusItem, error) {
	return func() ([]dtsutil.TaskStatusItem, error) {
		if err := ctx.Err(); err != nil {
			return nil, fmt.Errorf("收到退出信号: %w", err)
		}
		return dtsutil.FetchTaskStatusItems(p.DtsMasterAddr, p.TaskName, dtsutil.StatusTimeoutSec)
	}
}

// shardSourceCheck 任务 status 的 source 集合必须与 shard_sources 完全一致。
func (p *Params) shardSourceCheck() dtsutil.StatusCheck {
	want := make(map[string]struct{}, len(p.ShardSources))
	for _, s := range p.ShardSources {
		want[s.SourceName] = struct{}{}
	}
	return func(items []dtsutil.TaskStatusItem) error {
		return dtsutil.CheckSourceSet(items, want)
	}
}

// ctxSleeper 可被 ctx 打断的 sleep：取消后立即返回，由下一次 fetch 报出取消错误。
func ctxSleeper(ctx context.Context, interval time.Duration) func() {
	return func() {
		select {
		case <-ctx.Done():
		case <-time.After(interval):
		}
	}
}

// writePosition 按状态写位点文件并补齐时间线。
func (rt *runtime) writePosition(pf *PositionFile, state PositionState) error {
	pf.State = state
	if !rt.lockAcquired.IsZero() {
		pf.LockAcquiredAt = rt.lockAcquired.UTC().Format(time.RFC3339)
	}
	switch state {
	case PositionStateStopping:
		pf.StopIssuedAt = nowUTC()
	case PositionStateStopped, PositionStateInconsistent:
		pf.StoppedConfirmedAt = nowUTC()
	}
	pf.Nodes = rt.nodeResults()
	return writePositionFileAtomic(positionFilePath(rt.p.DeployPath, rt.p.PositionFileKey), pf)
}

// finalizePositionFile 解锁后补写 lock_released_at 与最终节点结果（失败只记日志）。
func (rt *runtime) finalizePositionFile(pf *PositionFile) {
	pf.LockReleasedAt = nowUTC()
	pf.Nodes = rt.nodeResults()
	if err := writePositionFileAtomic(positionFilePath(rt.p.DeployPath, rt.p.PositionFileKey), pf); err != nil {
		logger.Warn("解锁后补写位点文件失败（不影响结果）: %s", err.Error())
	}
}
