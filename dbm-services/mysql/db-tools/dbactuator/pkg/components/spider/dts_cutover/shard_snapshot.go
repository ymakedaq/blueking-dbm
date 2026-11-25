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
	"strings"
	"sync"
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

const snapshotTimeoutSec = 10

// snapshotShards 并行在各分片 remote master 上执行 SHOW MASTER STATUS（03 方案第 6.1 节）。
//
// 为什么直接查 remote master：DTS 返回的 master_binlog 是缓存值，可能偏旧；
// 拿旧值当基准会把“没追平”判成“已追平”。锁住 Spider 后 remote master 仍会因
// 非迁移表写入继续前进，比较基准必须是加锁后那一刻的快照。
func (rt *runtime) snapshotShards() (map[string]dtsutil.BinlogCoord, error) {
	type shardResult struct {
		sourceName string
		coord      dtsutil.BinlogCoord
		err        error
	}
	results := make([]shardResult, len(rt.p.ShardSources))
	var wg sync.WaitGroup
	for i, s := range rt.p.ShardSources {
		results[i].sourceName = s.SourceName
		worker, ok := rt.shardWorkers[s.SourceName]
		if !ok {
			results[i].err = fmt.Errorf("分片 %s 未建立连接", s.SourceName)
			continue
		}
		wg.Add(1)
		go func(idx int, worker *native.DbWorker) {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), snapshotTimeoutSec*time.Second)
			defer cancel()
			// 复用 native：兼容 8.4+ SHOW BINARY LOG STATUS；无结果（binlog 未开启）时 File 为空，下面统一报错
			st, err := worker.ShowMasterStatusContext(ctx)
			if err != nil {
				results[idx].err = fmt.Errorf("SHOW MASTER STATUS 失败: %w", err)
				return
			}
			results[idx].coord = dtsutil.BinlogCoord{File: st.File, Position: int64(st.Position)}
		}(i, worker)
	}
	wg.Wait()

	snapshots := make(map[string]dtsutil.BinlogCoord, len(results))
	var errs []string
	for _, r := range results {
		if r.err != nil {
			errs = append(errs, fmt.Sprintf("%s: %s", r.sourceName, r.err.Error()))
			continue
		}
		if r.coord.File == "" {
			errs = append(errs, fmt.Sprintf("%s: SHOW MASTER STATUS 未返回位点（binlog 可能未开启）", r.sourceName))
			continue
		}
		snapshots[r.sourceName] = r.coord
		logger.Info("分片快照 source=%s binlog=(%s, %d)", r.sourceName, r.coord.File, r.coord.Position)
	}
	if len(errs) > 0 {
		return nil, fmt.Errorf("取分片快照失败（不执行 stop）: %s", strings.Join(errs, "; "))
	}
	return snapshots, nil
}
