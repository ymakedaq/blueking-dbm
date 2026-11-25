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
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"dbm-services/common/go-pubpkg/logger"
)

// defaultAPITimeoutSec Master OpenAPI 查询默认超时
const defaultAPITimeoutSec = 30

// BuildMasterAPIURL 拼装 Master OpenAPI URL（本机直连 dts_master_addr）。
func BuildMasterAPIURL(dtsMasterAddr, apiPath string) (string, error) {
	addr := strings.TrimSpace(dtsMasterAddr)
	if addr == "" {
		return "", fmt.Errorf("dts_master_addr 为空")
	}
	if !strings.Contains(addr, "://") {
		addr = "http://" + addr
	}
	u, err := url.Parse(addr)
	if err != nil {
		return "", fmt.Errorf("解析 dts_master_addr 失败: %w", err)
	}
	decodedPath, err := url.PathUnescape(apiPath)
	if err != nil {
		return "", fmt.Errorf("非法 apiPath: %w", err)
	}
	u.Path = decodedPath
	u.RawPath = ""
	if decodedPath != apiPath {
		u.RawPath = apiPath
	}
	u.RawQuery = ""
	return u.String(), nil
}

// getMasterAPIJSON GET Master OpenAPI，HTTP 200 时把响应体解析到 out；what 用于日志与错误文案。
func getMasterAPIJSON(dtsMasterAddr, apiPath string, timeoutSec int, what string, out any) error {
	if timeoutSec <= 0 {
		timeoutSec = defaultAPITimeoutSec
	}
	u, err := BuildMasterAPIURL(dtsMasterAddr, apiPath)
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: time.Duration(timeoutSec) * time.Second}
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return fmt.Errorf("构造%s请求失败: %w", what, err)
	}
	logger.Info("查询 DTS %s: %s", what, req.URL.String())
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("查询%s失败: %w", what, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("查询%s HTTP %d", what, resp.StatusCode)
	}
	if err = json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("解析%s JSON 失败: %w", what, err)
	}
	return nil
}

func truncateOut(s string) string {
	const max = 2048
	if len(s) <= max {
		return s
	}
	return s[:max] + "...(truncated)"
}
