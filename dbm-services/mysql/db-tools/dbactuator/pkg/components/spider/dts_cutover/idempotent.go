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
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

const (
	idempotentStopConvergeSec    = 15
	idempotentPartialConvergeSec = 30
)

type idempotentAction int

const (
	// idempotentProceed 正常执行
	idempotentProceed idempotentAction = iota
	// idempotentDirectOutput 任务已全部停止且位点文件 stopped，直接输出
	idempotentDirectOutput
)

type idempotentDecision struct {
	action idempotentAction
	output *SpiderCutoverOutput
}

// manualHint 人工处理的出路，写进需要人工介入的错误文案。
const manualHint = "人工处理出路：核对源与目标数据后，可在 DTS 上 resume 任务并删除位点文件，再重试本命令重新切换"

// checkIdempotent 重试状态表（03 方案第 7.4 节）。
//
// 身份 = position_file_key（同一流程由上层 AcquireRunLock 串行化，同一 key 不会并发执行）；
// 权威事实源 = DTS 任务状态 + 本地位点文件状态。
func (c *Comp) checkIdempotent(ctx context.Context) (*idempotentDecision, error) {
	p := c.Params
	posPath := positionFilePath(p.DeployPath, p.PositionFileKey)
	posFile, err := readPositionFile(posPath)
	if err != nil {
		return nil, err
	}

	items, err := p.statusFetcher(ctx)()
	if err != nil {
		return nil, fmt.Errorf("幂等检查查询任务状态失败: %w", err)
	}

	// Paused（出错暂停）→ 失败，报出 DTS 错误
	if rerr := dtsutil.TaskReportError(items); rerr != nil {
		return nil, fmt.Errorf("DTS 任务处于出错暂停状态，拒绝切换: %w", rerr)
	}
	for _, item := range items {
		if item.Stage == dtsutil.TaskStagePaused {
			return nil, fmt.Errorf("DTS 任务处于人工暂停状态（source %s），请先 resume 或人工处理", dtsutil.StatusSourceKey(item))
		}
	}

	allStopped, allRunning := dtsutil.ClassifyStages(items)
	return c.classifyIdempotent(ctx, posPath, posFile, allStopped, allRunning, true)
}

// classifyIdempotent 按（任务阶段 × 位点文件状态）查表决策。
// waitPartial=false 时部分 Stopped 直接交人工（已等过一次收敛，不再重复等待）。
func (c *Comp) classifyIdempotent(
	ctx context.Context, posPath string, posFile *PositionFile, allStopped, allRunning, waitPartial bool,
) (*idempotentDecision, error) {
	switch {
	case allRunning:
		if posFile == nil {
			return &idempotentDecision{action: idempotentProceed}, nil
		}
		switch posFile.State {
		case PositionStateStopping:
			// 停止可能迟到：轮询 15 秒确认
			return c.pollStoppingLate(ctx, posPath)
		case PositionStateStopped, PositionStateInconsistent:
			// 状态矛盾：任务被人为重启
			return nil, fmt.Errorf(
				"状态矛盾：任务全部 Running 但位点文件为 %s（任务可能被人为重启），交人工处理。%s",
				posFile.State, manualHint,
			)
		default:
			return nil, fmt.Errorf("位点文件状态异常: %q", posFile.State)
		}

	case allStopped:
		if posFile == nil {
			return nil, fmt.Errorf("任务已全部停止但无位点文件，结果未知，需人工核对。%s", manualHint)
		}
		switch posFile.State {
		case PositionStateStopped:
			return &idempotentDecision{
				action: idempotentDirectOutput, output: outputFromPositionFile(c.Params, posFile),
			}, nil
		case PositionStateStopping:
			return nil, fmt.Errorf(
				"结果未知：任务已全部停止但位点文件为 stopping（无法确认停止发生在锁释放之前还是之后），交人工核对。%s",
				manualHint,
			)
		case PositionStateInconsistent:
			return nil, fmt.Errorf("锁中途丢失，一致性不成立，交人工处理。%s", manualHint)
		default:
			return nil, fmt.Errorf("位点文件状态异常: %q", posFile.State)
		}

	case !waitPartial:
		return nil, fmt.Errorf("任务部分 Stopped，交人工处理。%s", manualHint)

	default:
		return c.waitPartialConverge(ctx, posPath)
	}
}

// waitPartialConverge 部分 Stopped：上次停止正在收敛，最多等 30 秒再查表一次。
func (c *Comp) waitPartialConverge(ctx context.Context, posPath string) (*idempotentDecision, error) {
	logger.Warn("幂等检查：任务部分 Stopped（上次停止正在收敛），轮询等待")
	res, err := watchStages(ctx, c.Params, idempotentPartialConvergeSec, "等待收敛", func(o stageObservation) bool {
		return o.allStopped || o.allRunning
	})
	if err != nil {
		return nil, fmt.Errorf("等待收敛期间失败: %w", err)
	}
	if !res.reached {
		return nil, fmt.Errorf("任务部分 Stopped 且 %d 秒内未收敛，交人工处理。%s", idempotentPartialConvergeSec, manualHint)
	}
	return c.reclassify(ctx, posPath, res.last)
}

// pollStoppingLate Running + stopping：轮询 15 秒确认停止没有迟到。
func (c *Comp) pollStoppingLate(ctx context.Context, posPath string) (*idempotentDecision, error) {
	logger.Warn("幂等检查：任务 Running 且位点文件 stopping，确认停止是否迟到")
	res, err := watchStages(ctx, c.Params, idempotentStopConvergeSec, "确认停止", func(o stageObservation) bool {
		return !o.allRunning
	})
	if err != nil {
		return nil, fmt.Errorf("确认停止期间失败: %w", err)
	}
	if res.reached {
		return c.reclassify(ctx, posPath, res.last)
	}
	if !res.last.observed() {
		// 一次都没查到：停止是否迟到未知，不能当作“未停止”删文件重来
		return nil, fmt.Errorf(
			"确认停止期间任务状态查询持续失败（%v），停止是否生效未知，交人工处理。%s", res.lastFetchErr, manualHint,
		)
	}
	// 仍全部 Running：上次停任务没有生效，删除文件后正常执行
	logger.Info("确认停止未迟到：任务仍全部 Running，删除 stopping 位点文件后正常执行")
	if err = removePositionFile(posPath); err != nil {
		return nil, fmt.Errorf("删除 stopping 位点文件失败: %w", err)
	}
	return &idempotentDecision{action: idempotentProceed}, nil
}

// reclassify 等待结束后重读位点文件再查表一次（不再等待部分收敛，避免循环）。
func (c *Comp) reclassify(ctx context.Context, posPath string, obs stageObservation) (*idempotentDecision, error) {
	posFile, err := readPositionFile(posPath)
	if err != nil {
		return nil, err
	}
	return c.classifyIdempotent(ctx, posPath, posFile, obs.allStopped, obs.allRunning, false)
}
