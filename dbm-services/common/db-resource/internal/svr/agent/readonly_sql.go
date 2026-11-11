/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package agent

import (
	"fmt"
	"regexp"
	"strings"
	"time"

	"dbm-services/common/db-resource/internal/model"
)

const (
	maxReadonlyRows       = 100
	maxReadonlyCellLength = 2048
	readonlyQueryTimeout  = 30 * time.Second
)

// allowedReadonlyTables 发散探索链路允许查询的只读证据表。
// 除资源池明细外，其余表用于验证并发抢占、导入归档、状态变更、参数快照等长尾根因。
func allowedReadonlyTables() map[string]bool {
	return map[string]bool{
		strings.ToLower(model.TbRpDetailName()):             true,
		strings.ToLower(model.TbRpOperationInfoTableName()): true,
		strings.ToLower(model.TbRpApplyDetailLogName()):     true,
		strings.ToLower(model.TbRpStatusChangeLogName()):    true,
		strings.ToLower(model.TbRpDetailArchiveName()):      true,
		strings.ToLower(model.TbRequestLogName()):           true,
	}
}

var (
	// tableRefRe 提取 FROM / JOIN 引用的表（含子查询、schema 前缀与反引号）。
	// 用 \x60 表示反引号，以便整个正则使用 raw string。
	tableRefRe = regexp.MustCompile(`(?i)\b(?:from|join)\s+\x60?([a-zA-Z0-9_]+)\x60?(?:\.\x60?([a-zA-Z0-9_]+)\x60?)?`)
	// forbiddenSQLRe 用词边界避免误伤 update_time / create_time 这类合法列名
	forbiddenSQLRe = regexp.MustCompile(`(?i)\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|REPLACE|CALL|UNION|OUTFILE|LOAD_FILE|INFORMATION_SCHEMA|PERFORMANCE_SCHEMA)\b`)
	limitSQLRe = regexp.MustCompile(`(?i)\bLIMIT\s+\d+`)
)

// validateReadonlySQL 在执行前校验 LLM 生成的 SQL，并统一补齐行数上限。
// 不再依赖「SQL 文本包含某个表名字符串」这类可绕过的检查。
func validateReadonlySQL(sql string) (string, error) {
	trimmed := strings.TrimSpace(sql)
	if trimmed == "" {
		return "", fmt.Errorf("SQL 不能为空")
	}
	// 注释可用来藏表名或绕过关键字检查，直接拒绝
	if strings.Contains(trimmed, "--") || strings.Contains(trimmed, "/*") ||
		strings.Contains(trimmed, "*/") || strings.Contains(trimmed, "#") {
		return "", fmt.Errorf("只读查询不允许包含注释")
	}
	// 只允许单条语句
	trimmed = strings.TrimSuffix(trimmed, ";")
	if strings.Contains(trimmed, ";") {
		return "", fmt.Errorf("只读查询不允许执行多条语句")
	}
	upper := strings.ToUpper(trimmed)
	if !strings.HasPrefix(upper, "SELECT ") && upper != "SELECT" {
		return "", fmt.Errorf("只允许执行 SELECT 查询")
	}
	if forbiddenSQLRe.MatchString(upper) {
		return "", fmt.Errorf("查询包含不允许的关键字")
	}

	refs := tableRefRe.FindAllStringSubmatch(trimmed, -1)
	if len(refs) == 0 {
		return "", fmt.Errorf("未能解析出查询的表名，拒绝执行")
	}
	allowed := allowedReadonlyTables()
	for _, ref := range refs {
		name := strings.ToLower(ref[1])
		if ref[2] != "" {
			name = strings.ToLower(ref[2])
		}
		if !allowed[name] {
			return "", fmt.Errorf("表 %s 不在只读证据白名单中，允许的表：%v",
				name, readonlyTableNames())
		}
	}

	if !limitSQLRe.MatchString(upper) {
		trimmed += fmt.Sprintf(" LIMIT %d", maxReadonlyRows)
	}
	return trimmed, nil
}

func readonlyTableNames() []string {
	allowed := allowedReadonlyTables()
	names := make([]string, 0, len(allowed))
	for name := range allowed {
		names = append(names, name)
	}
	return names
}

// sanitizeReadonlyValue 截断大字段，避免把整包 request_body / response_body 等
// 敏感或超长内容原样塞进 LLM 上下文。
func sanitizeReadonlyValue(val interface{}) interface{} {
	s, ok := val.(string)
	if !ok || len(s) <= maxReadonlyCellLength {
		return val
	}
	return s[:maxReadonlyCellLength] + "…(已截断)"
}
