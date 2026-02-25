/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package dbmapi

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/url"

	"dbm-services/common/go-pubpkg/logger"
)

// DissolveCheckResponse dissolve check response
type DissolveCheckResponse struct {
	DissolvedHostIDs []int `json:"dissolved_host_ids"`
}

// CheckDissolveHosts request dbm api to check hosts in dissolve stage
func CheckDissolveHosts(bkHostIds []int) (dissolvedHostIds []int, err error) {
	cli := NewDbmClient()
	u, err := url.JoinPath(cli.EndPoint, DBMDissolveHostsCheckApi)
	if err != nil {
		return nil, err
	}
	p := map[string]interface{}{
		"bk_host_ids": bkHostIds,
	}
	body, err := json.Marshal(p)
	if err != nil {
		logger.Error("marshal CheckDissolveHosts body failed %s", err.Error())
		return nil, err
	}
	request, err := http.NewRequest(http.MethodPost, u, bytes.NewBuffer(body))
	if err != nil {
		return nil, err
	}
	resp, err := cli.Client.Do(request)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	content, err := io.ReadAll(resp.Body)
	if err != nil {
		logger.Error("read response body failed %s", err.Error())
		return nil, err
	}
	logger.Info("CheckDissolveHosts response %v", string(content))
	var d DissolveCheckResponse
	if err = json.Unmarshal(content, &d); err != nil {
		return nil, err
	}
	return d.DissolvedHostIDs, nil
}
