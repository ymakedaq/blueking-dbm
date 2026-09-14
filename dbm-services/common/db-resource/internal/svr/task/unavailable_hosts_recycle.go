/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package task

import (
	"fmt"

	"github.com/samber/lo"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/dbmapi"
	"dbm-services/common/go-pubpkg/logger"
)

// UnavailableHostRecycle 扫描资源池 Unavailable 主机并转入待回收池。
// 导入时已将数据盘 ext3 主机标为 Unavailable，本任务不再做二次判定。
func UnavailableHostRecycle() (err error) {
	machines, err := listUnavailableMachinesFn()
	if err != nil {
		logger.Error("get unavailable machines failed %s", err.Error())
		return err
	}
	if len(machines) == 0 {
		logger.Info("no unavailable machines found for recycle")
		return nil
	}

	var failedBatches int
	var lastErr error
	for _, mgp := range lo.Chunk(machines, hostCheckBatchSize) {
		if batchErr := processUnavailableRecycleBatch(mgp); batchErr != nil {
			failedBatches++
			lastErr = batchErr
		}
	}
	if failedBatches > 0 {
		return fmt.Errorf("unavailable recycle failed for %d batches, last: %w", failedBatches, lastErr)
	}
	return nil
}

func processUnavailableRecycleBatch(mgp []model.TbRpDetail) error {
	hostIds := hostIdsOf(mgp)
	logger.Info("recycle unavailable hosts %v", hostIds)
	if err := resourceDeleteFn(buildDeleteHosts(mgp, hostIds), dbmapi.EventToRecycle, remarkExt3Recycle); err != nil {
		logger.Error("resource delete unavailable hosts failed, hosts=%v err=%s", hostIds, err.Error())
		return err
	}
	return nil
}
