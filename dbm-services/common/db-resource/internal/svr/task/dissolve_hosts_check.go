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
	"time"

	"github.com/samber/lo"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/dbmapi"
	"dbm-services/common/go-pubpkg/logger"
)

// DissolveHostCheck 检查资源池中是否有待裁撤的主机，若有则标记为 FaultHazard
// 仅当 HOST_DISSOLVED_SWITCH 为 false（或未设置）时执行
func DissolveHostCheck() (err error) {
	dbmConfig, err := dbmapi.GetDbmEnv()
	if err != nil {
		logger.Error("get dbm env failed %s", err.Error())
		return err
	}
	if dbmConfig.HOST_DISSOLVED_SWITCH {
		logger.Info("HOST_DISSOLVED_SWITCH is true, skip dissolve host check")
		return nil
	}
	var machines []model.TbRpDetail
	if err = model.DB.Self.Table(model.TbRpDetailName()).
		Where("status = ?", model.Unused).
		Find(&machines).Error; err != nil {
		logger.Error("get unused machines failed %s", err.Error())
		return err
	}
	if len(machines) == 0 {
		logger.Info("no unused machines found")
		return nil
	}
	for _, mgp := range lo.Chunk(machines, 50) {
		var bkHostIds []int
		for _, m := range mgp {
			bkHostIds = append(bkHostIds, m.BkHostID)
		}
		dissolvedHostIds, err := dbmapi.CheckDissolveHosts(bkHostIds)
		if err != nil {
			logger.Error("check dissolve hosts failed %s", err.Error())
			continue
		}
		if len(dissolvedHostIds) == 0 {
			logger.Info("no dissolve hosts found in this batch")
			continue
		}
		for _, hostId := range dissolvedHostIds {
			logger.Info("host %d is in dissolve stage, marking as FaultHazard", hostId)
			err = model.DB.Self.Table(model.TbRpDetailName()).
				Where("bk_host_id = ?", hostId).
				Updates(map[string]interface{}{"status": model.FaultHazard, "update_time": time.Now()}).Error
			if err != nil {
				logger.Error("update machine status failed %s", err.Error())
				return err
			}
		}
	}
	return
}
