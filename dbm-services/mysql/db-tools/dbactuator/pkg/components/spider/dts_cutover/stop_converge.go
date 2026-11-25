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

// stagePollInterval 停任务收敛 / 幂等确认的轮询间隔
const stagePollInterval = 1 * time.Second

// stopOutcome 停任务结果三值判定（03 方案第 7.3 节）：未知不折叠成失败。
type stopOutcome int

const (
	// stopOutcomeSuccess 全部 subtask Stopped
	stopOutcomeSuccess stopOutcome = iota
	// stopOutcomeDefiniteFail 全部仍 Running 且 HTTP 返回明确错误（可直接重试）
	stopOutcomeDefiniteFail
	// stopOutcomeUnknown 部分 Stopped / 查询失败 / HTTP 超时且仍 Running
	stopOutcomeUnknown
)

// stageObservation 一次成功的任务状态观察。
type stageObservation struct {
	items      []dtsutil.TaskStatusItem
	allStopped bool
	allRunning bool
}

// observed 是否至少成功查到过一次状态。
func (o stageObservation) observed() bool {
	return o.items != nil
}

// stageWatchResult watchStages 的结果。
type stageWatchResult struct {
	// last 最后一次成功观察；从未查到时 items 为 nil
	last stageObservation
	// reached until 是否已满足
	reached bool
	// lastFetchErr 最后一次查询错误（之后又查询成功则清空）
	lastFetchErr error
}

// watchStages 每秒查询一次任务状态，最多 attempts 次，直到 until 满足。
// 查询失败只记日志继续（失败本身不是结论）；任务报错立即返回该错误（last 为报错那次的状态）；ctx 取消立即返回。
func watchStages(
	ctx context.Context, p *Params, attempts int, what string, until func(stageObservation) bool,
) (stageWatchResult, error) {
	var res stageWatchResult
	fetch := p.statusFetcher(ctx)
	for attempt := 0; attempt < attempts; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return res, fmt.Errorf("%s被取消: %w", what, ctx.Err())
			case <-time.After(stagePollInterval):
			}
		}
		items, err := fetch()
		if err != nil {
			res.lastFetchErr = err
			logger.Warn("%s查询任务状态失败 attempt=%d/%d: %s", what, attempt+1, attempts, err.Error())
			continue
		}
		res.lastFetchErr = nil
		res.last.items = items
		res.last.allStopped, res.last.allRunning = dtsutil.ClassifyStages(items)
		if rerr := dtsutil.TaskReportError(items); rerr != nil {
			return res, rerr
		}
		if until(res.last) {
			res.reached = true
			return res, nil
		}
		logger.Info("%s中 attempt=%d/%d stages=%s", what, attempt+1, attempts, dtsutil.SummarizeStages(items))
	}
	return res, nil
}

// stopAndConverge 停任务 + 持锁收敛。
//
// DTS 任务状态是权威来源，HTTP 响应只是线索：
// 不论 HTTP 返回什么，持锁轮询任务状态，最多 stopConvergeSec（默认 15 秒），间隔 1 秒：
//   - 全部 Stopped → 成功
//   - 全部仍 Running 且 HTTP 明确失败 → 明确失败（删位点文件，任务仍 Running，可直接重试）
//   - 其他 → 未知（位点文件保留 stopping，重试时由幂等状态表收敛）
//
// 收敛必须跑完，不响应退出信号（停止已发出，中途放弃只会制造更多未知）。
func stopAndConverge(p *Params) (stopOutcome, []dtsutil.TaskStatusItem, error) {
	stopHTTPErr := dtsutil.StopTaskWhileLocked(p.DtsMasterAddr, p.TaskName)
	// 只有服务端明确回了非 2xx 才算“明确失败”的线索；超时 / 连接中断属于结果未知，
	// 停止可能仍在服务端执行，不能据此判定为失败并删除位点文件。
	stopDefiniteErr := stopHTTPErr != nil && !dtsutil.IsTransportError(stopHTTPErr)
	if stopHTTPErr != nil {
		logger.Warn("停任务 HTTP 调用返回错误(definite=%v)，转入持锁收敛: %s", stopDefiniteErr, stopHTTPErr.Error())
	}

	res, err := watchStages(context.Background(), p, p.StopConvergeSec, "停任务收敛", func(o stageObservation) bool {
		return o.allStopped || (o.allRunning && stopDefiniteErr)
	})
	if err != nil {
		// 收敛期间任务报错（Paused 等）：停止已发出、任务也不再是 Running，不能判定为“可直接重试”，
		// 按未知处理（保留 stopping 位点文件，重试时由幂等状态表拦截并交人工）
		return stopOutcomeUnknown, res.last.items, err
	}
	if res.reached {
		if res.last.allStopped {
			logger.Info("停任务收敛完成：全部 Stopped")
			return stopOutcomeSuccess, res.last.items, nil
		}
		// 全部仍 Running 且 HTTP 明确失败：停止没有生效
		return stopOutcomeDefiniteFail, res.last.items, stopHTTPErr
	}
	if !res.last.observed() {
		if stopHTTPErr != nil {
			return stopOutcomeUnknown, nil, fmt.Errorf(
				"停任务后状态未知（状态查询失败: %v；stop 调用错误: %v）", res.lastFetchErr, stopHTTPErr,
			)
		}
		return stopOutcomeUnknown, nil, fmt.Errorf("停任务后状态查询持续失败: %w", res.lastFetchErr)
	}
	return stopOutcomeUnknown, res.last.items, fmt.Errorf(
		"持锁收敛超时 %d 秒，任务状态未全部 Stopped: stages=%s",
		p.StopConvergeSec, dtsutil.SummarizeStages(res.last.items),
	)
}
