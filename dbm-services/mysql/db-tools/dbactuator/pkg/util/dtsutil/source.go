/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package dtsutil

import (
	"fmt"
	"net/url"
)

// sourceConfigTimeoutSec 查询 source 配置的 HTTP 超时
const sourceConfigTimeoutSec = 15

// SourceConfigResponse GET /api/v1/sources/{source-name} 响应（扁平结构，与 mysqldtsapi.GetSourceResponse 对齐）。
type SourceConfigResponse struct {
	SourceName string `json:"source_name"`
	Host       string `json:"host"`
	Port       int    `json:"port"`
	User       string `json:"user"`
}

// FetchSourceConfig 查询 DTS source 的实际连接地址。
// 任务状态里没有 source 地址；迁移期间上游若发生主从切换，source 仍连旧 master，快照与 syncer 位点不可比。
func FetchSourceConfig(dtsMasterAddr, sourceName string) (*SourceConfigResponse, error) {
	var out SourceConfigResponse
	apiPath := fmt.Sprintf("/api/v1/sources/%s", url.PathEscape(sourceName))
	what := fmt.Sprintf("source[%s]配置", sourceName)
	if err := getMasterAPIJSON(dtsMasterAddr, apiPath, sourceConfigTimeoutSec, what, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
