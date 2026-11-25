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
	"sync"
	"sync/atomic"
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
)

// binlogTicker 复核期间的心跳写入（03 方案第 6.4 节）。
//
// 问题：锁住 Spider 后迁移范围内没有新写入，syncer_binlog（已刷新的 checkpoint）
// 最长要等 30 秒 binlog 心跳才推进，复核窗口会超时。
// 做法：每个分片 remote master 起一个 goroutine，每秒写一次
// REPLACE INTO infodba_schema.check_heartbeat(uid) VALUE(@@server_id)
// ——不设 sql_log_bin=0，产生行事件 + XID，推动 checkpoint 在约 5 秒内刷新。
type binlogTicker struct {
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	tickCount atomic.Int64
	failCount atomic.Int64
	stopOnce  sync.Once
}

// startBinlogTicker 为每个分片 remote master 启动心跳写入 goroutine。
// 写入失败只记日志、不中断——写入只是加速手段，复核超时按正常流程失败退出。
// 随 ctx 停止；defer 中再停一次防止泄漏。
func startBinlogTicker(
	parent context.Context, shardWorkers map[string]*native.DbWorker, interval time.Duration,
) *binlogTicker {
	if interval <= 0 {
		interval = time.Second
	}
	ctx, cancel := context.WithCancel(parent)
	t := &binlogTicker{cancel: cancel}
	for sourceName, worker := range shardWorkers {
		if worker == nil {
			continue
		}
		t.wg.Add(1)
		go func(sourceName string, worker *native.DbWorker) {
			defer t.wg.Done()
			ticker := time.NewTicker(interval)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					tctx, tcancel := context.WithTimeout(context.Background(), 5*time.Second)
					_, err := worker.Db.ExecContext(tctx, heartbeatWriteSQL)
					tcancel()
					t.tickCount.Add(1)
					if err != nil {
						t.failCount.Add(1)
						logger.Warn("分片 %s 心跳写入失败（继续复核）: %s", sourceName, err.Error())
					}
				}
			}
		}(sourceName, worker)
	}
	logger.Info("心跳写入已启动 shards=%d interval=%s", len(shardWorkers), interval)
	return t
}

// stop 停止心跳写入（幂等）。
func (t *binlogTicker) stop() {
	if t == nil {
		return
	}
	t.stopOnce.Do(func() {
		t.cancel()
		t.wg.Wait()
		logger.Info("心跳写入已停止 ticks=%d fails=%d", t.tickCount.Load(), t.failCount.Load())
	})
}
