/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package agent

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"dbm-services/common/db-resource/internal/config"
	"dbm-services/common/go-pubpkg/logger"
)

// ValidationReport 验证报告
type ValidationReport struct {
	Passed          bool              `json:"passed"`
	ConfidenceScore int               `json:"confidence_score"` // 0-100
	Issues          []ValidationIssue `json:"issues"`
	Suggestions     []string          `json:"suggestions"`
}

// ValidationIssue 验证问题
type ValidationIssue struct {
	Category    string `json:"category"`    // reason_mismatch, quantity_logic, priority_error, forbidden
	Severity    string `json:"severity"`    // high, medium, low
	Description string `json:"description"`
	Field       string `json:"field"` // 具体问题字段
}

// ReasonSuggestionMapping 原因类别到建议类型的映射
var ReasonSuggestionMapping = map[string][]string{
	"disk":     {"adjust_disk"},
	"label":    {"add_labels"},
	"rstype":   {"change_rstype"},
	"spec":     {"adjust_spec", "add_resources"},
	"location": {"add_resources"},
	"affinity": {"add_resources"},
}

// ResourceAnalysisValidator 资源分析验证器
type ResourceAnalysisValidator struct {
	config config.ValidatorConfig
}

// NewResourceAnalysisValidator 创建验证器
func NewResourceAnalysisValidator(cfg config.ValidatorConfig) *ResourceAnalysisValidator {
	return &ResourceAnalysisValidator{
		config: cfg,
	}
}

// ValidateAnalysisResult 验证分析结果
func (v *ResourceAnalysisValidator) ValidateAnalysisResult(result *AnalysisResult) *ValidationReport {
	if result == nil {
		return &ValidationReport{
			Passed:          false,
			ConfidenceScore: 0,
			Issues: []ValidationIssue{
				{
					Category:    "invalid_input",
					Severity:    "high",
					Description: "分析结果为空",
					Field:       "result",
				},
			},
		}
	}

	report := &ValidationReport{
		Passed:          true,
		ConfidenceScore: 100, // 初始分数为满分
		Issues:          make([]ValidationIssue, 0),
		Suggestions:     make([]string, 0),
	}

	// 1. 检查原因-建议匹配
	v.checkReasonSuggestionMapping(result, report)

	// 2. 检查数量逻辑
	v.checkQuantityLogic(result, report)

	// 3. 检查优先级合理性
	v.checkPriorityReasoning(result, report)

	// 4. 检查禁止建议
	v.checkForbiddenSuggestions(result, report)

	// 5. 检查基本完整性
	v.checkCompleteness(result, report)

	// 计算最终置信度评分
	v.calculateConfidenceScore(report)

	// 判断是否通过验证
	report.Passed = report.ConfidenceScore >= v.config.MinConfidenceScore && len(getHighSeverityIssues(report)) == 0

	logger.Info("[Validator] Validation completed: passed=%v, score=%d, issues=%d",
		report.Passed, report.ConfidenceScore, len(report.Issues))

	return report
}

// checkReasonSuggestionMapping 检查原因-建议映射
func (v *ResourceAnalysisValidator) checkReasonSuggestionMapping(result *AnalysisResult, report *ValidationReport) {
	// 构建原因类别集合
	reasonCategories := make(map[string]bool)
	for _, reason := range result.Reasons {
		if reason.Category != "" {
			reasonCategories[reason.Category] = true
		}
	}

	// 检查每个建议是否与原因类别匹配
	for _, suggestion := range result.Suggestions {
		matched := false

		// 特殊类型：contact_admin 总是允许的
		if suggestion.Type == "contact_admin" {
			matched = true
		} else {
			// 检查建议类型是否与任一原因类别匹配
			for category := range reasonCategories {
				if expectedTypes, ok := ReasonSuggestionMapping[category]; ok {
					for _, expectedType := range expectedTypes {
						if suggestion.Type == expectedType {
							matched = true
							break
						}
					}
				}
				if matched {
					break
				}
			}
		}

		if !matched && len(reasonCategories) > 0 {
			// 找不到匹配的原因类别
			report.Issues = append(report.Issues, ValidationIssue{
				Category: "reason_mismatch",
				Severity: "high",
				Description: fmt.Sprintf("建议类型 '%s' 与失败原因类别不匹配。原因类别: %v",
					suggestion.Type, getKeys(reasonCategories)),
				Field: "suggestions",
			})
			report.Suggestions = append(report.Suggestions,
				"建议调整建议类型以匹配原因类别，或添加对应的失败原因说明")
		}
	}
}

// checkQuantityLogic 检查数量逻辑
func (v *ResourceAnalysisValidator) checkQuantityLogic(result *AnalysisResult, report *ValidationReport) {
	// 从 Summary 或 Reasons 中提取申请数量和可用数量
	requestCount, availableCount := extractQuantityInfo(result)

	if requestCount > 0 && availableCount >= 0 {
		// 检查每个建议的预测数量
		for _, suggestion := range result.Suggestions {
			if suggestion.Type == "add_resources" && suggestion.PredictedCount > 0 {
				// 如果建议补充资源，预测数量应该 >= 申请数量
				if suggestion.PredictedCount < requestCount {
					report.Issues = append(report.Issues, ValidationIssue{
						Category: "quantity_logic",
						Severity: "medium",
						Description: fmt.Sprintf("建议的预测数量 %d 小于申请数量 %d，无法满足需求",
							suggestion.PredictedCount, requestCount),
						Field: "suggestions.predicted_count",
					})
					report.Suggestions = append(report.Suggestions,
						fmt.Sprintf("建议将预测数量调整为至少 %d 台", requestCount))
				}
			}
		}

		// 检查是否需要补充资源
		if availableCount < requestCount {
			hasAddResourcesSuggestion := false
			for _, suggestion := range result.Suggestions {
				if suggestion.Type == "add_resources" {
					hasAddResourcesSuggestion = true
					break
				}
			}

			if !hasAddResourcesSuggestion {
				report.Issues = append(report.Issues, ValidationIssue{
					Category:    "quantity_logic",
					Severity:    "medium",
					Description: fmt.Sprintf("可用资源 %d 台少于申请数量 %d 台，但未给出补充资源的建议", availableCount, requestCount),
					Field:       "suggestions",
				})
				report.Suggestions = append(report.Suggestions,
					"建议添加 add_resources 类型的建议")
			}
		}
	}
}

// checkPriorityReasoning 检查优先级合理性
func (v *ResourceAnalysisValidator) checkPriorityReasoning(result *AnalysisResult, report *ValidationReport) {
	// 检查高影响因素是否有对应的高优先级建议
	hasHighImpactReason := false
	for _, reason := range result.Reasons {
		if reason.Impact == "high" {
			hasHighImpactReason = true
			break
		}
	}

	if hasHighImpactReason {
		hasHighPrioritySuggestion := false
		for _, suggestion := range result.Suggestions {
			if suggestion.Priority <= 2 {
				hasHighPrioritySuggestion = true
				break
			}
		}

		if !hasHighPrioritySuggestion {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "priority_error",
				Severity:    "low",
				Description: "存在高影响因素，但没有对应的高优先级建议（优先级 1-2）",
				Field:       "suggestions.priority",
			})
			report.Suggestions = append(report.Suggestions,
				"建议为高影响因素提供优先级 1 或 2 的建议")
		}
	}

	// 检查优先级是否合理（1-5）
	for idx, suggestion := range result.Suggestions {
		if suggestion.Priority < 1 || suggestion.Priority > 5 {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "priority_error",
				Severity:    "low",
				Description: fmt.Sprintf("建议 #%d 的优先级 %d 超出合理范围（1-5）", idx+1, suggestion.Priority),
				Field:       fmt.Sprintf("suggestions[%d].priority", idx),
			})
		}
	}
}

// checkForbiddenSuggestions 检查禁止建议
func (v *ResourceAnalysisValidator) checkForbiddenSuggestions(result *AnalysisResult, report *ValidationReport) {
	forbiddenKeywords := map[string]string{
		"降低申请数量":   "reduce_count",
		"减少申请":     "reduce_request",
		"放宽亲和性":    "relax_affinity",
		"分批申请":     "split_request",
		"更换地域":     "change_location",
		"换到其他城市":   "change_location",
		"选择其他园区":   "change_location",
		"降低数量":     "reduce_count",
		"分两次申请":    "split_request",
		"先申请":      "split_request",
		"改为SAME":   "relax_affinity",
		"改为NONE":   "relax_affinity",
		"调整亲和性类型": "relax_affinity",
	}

	for idx, suggestion := range result.Suggestions {
		// 检查建议类型
		if IsForbiddenSuggestion(suggestion.Type) {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "forbidden",
				Severity:    "high",
				Description: fmt.Sprintf("建议 #%d 包含禁止的建议类型: %s", idx+1, suggestion.Type),
				Field:       fmt.Sprintf("suggestions[%d].type", idx),
			})
		}

		// 检查建议描述中的禁止关键词
		for keyword, forbiddenType := range forbiddenKeywords {
			if strings.Contains(suggestion.Description, keyword) {
				report.Issues = append(report.Issues, ValidationIssue{
					Category: "forbidden",
					Severity: "high",
					Description: fmt.Sprintf("建议 #%d 的描述包含禁止的关键词 '%s'（类型: %s）",
						idx+1, keyword, forbiddenType),
					Field: fmt.Sprintf("suggestions[%d].description", idx),
				})
			}
		}
	}

	// 检查 Summary 中的禁止关键词
	for keyword, forbiddenType := range forbiddenKeywords {
		if strings.Contains(result.Summary, keyword) {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "forbidden",
				Severity:    "medium",
				Description: fmt.Sprintf("摘要中包含禁止的关键词 '%s'（类型: %s）", keyword, forbiddenType),
				Field:       "summary",
			})
		}
	}
}

// checkCompleteness 检查基本完整性
func (v *ResourceAnalysisValidator) checkCompleteness(result *AnalysisResult, report *ValidationReport) {
	// 检查是否有摘要
	if result.Summary == "" {
		report.Issues = append(report.Issues, ValidationIssue{
			Category:    "completeness",
			Severity:    "medium",
			Description: "缺少分析摘要",
			Field:       "summary",
		})
	}

	// 检查是否有失败原因
	if len(result.Reasons) == 0 {
		report.Issues = append(report.Issues, ValidationIssue{
			Category:    "completeness",
			Severity:    "high",
			Description: "缺少失败原因分析",
			Field:       "reasons",
		})
	}

	// 检查是否有建议
	if len(result.Suggestions) == 0 {
		report.Issues = append(report.Issues, ValidationIssue{
			Category:    "completeness",
			Severity:    "high",
			Description: "缺少改进建议",
			Field:       "suggestions",
		})
	}

	// 检查原因是否有描述
	for idx, reason := range result.Reasons {
		if reason.Description == "" {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "completeness",
				Severity:    "medium",
				Description: fmt.Sprintf("原因 #%d 缺少描述", idx+1),
				Field:       fmt.Sprintf("reasons[%d].description", idx),
			})
		}
		if reason.Category == "" {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "completeness",
				Severity:    "low",
				Description: fmt.Sprintf("原因 #%d 缺少类别", idx+1),
				Field:       fmt.Sprintf("reasons[%d].category", idx),
			})
		}
	}

	// 检查建议是否有描述
	for idx, suggestion := range result.Suggestions {
		if suggestion.Description == "" {
			report.Issues = append(report.Issues, ValidationIssue{
				Category:    "completeness",
				Severity:    "medium",
				Description: fmt.Sprintf("建议 #%d 缺少描述", idx+1),
				Field:       fmt.Sprintf("suggestions[%d].description", idx),
			})
		}
	}
}

// calculateConfidenceScore 计算置信度评分
func (v *ResourceAnalysisValidator) calculateConfidenceScore(report *ValidationReport) {
	score := 100

	// 根据问题严重程度扣分
	for _, issue := range report.Issues {
		switch issue.Severity {
		case "high":
			score -= 20
		case "medium":
			score -= 10
		case "low":
			score -= 5
		}
	}

	// 确保分数在 0-100 范围内
	if score < 0 {
		score = 0
	}

	report.ConfidenceScore = score
}

// extractQuantityInfo 从分析结果中提取数量信息
func extractQuantityInfo(result *AnalysisResult) (requestCount int, availableCount int) {
	requestCount = -1
	availableCount = -1

	// 从 Summary 中提取
	// 匹配模式: "申请 N 台"，"可用 M 台"，"需要 N 台"
	requestPatterns := []string{
		`申请\s*(\d+)\s*台`,
		`需要\s*(\d+)\s*台`,
		`request_count[:\s=]+(\d+)`,
	}
	availablePatterns := []string{
		`可用\s*(\d+)\s*台`,
		`仅有\s*(\d+)\s*台`,
		`available[:\s=]+(\d+)`,
		`final_count[:\s=]+(\d+)`,
	}

	text := result.Summary
	for _, reason := range result.Reasons {
		text += " " + reason.Description
	}

	for _, pattern := range requestPatterns {
		if re := regexp.MustCompile(pattern); re.MatchString(text) {
			if matches := re.FindStringSubmatch(text); len(matches) > 1 {
				if count, err := strconv.Atoi(matches[1]); err == nil {
					requestCount = count
					break
				}
			}
		}
	}

	for _, pattern := range availablePatterns {
		if re := regexp.MustCompile(pattern); re.MatchString(text) {
			if matches := re.FindStringSubmatch(text); len(matches) > 1 {
				if count, err := strconv.Atoi(matches[1]); err == nil {
					availableCount = count
					break
				}
			}
		}
	}

	return requestCount, availableCount
}

// getKeys 获取 map 的键集合
func getKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}

// getHighSeverityIssues 获取高严重性问题
func getHighSeverityIssues(report *ValidationReport) []ValidationIssue {
	highIssues := make([]ValidationIssue, 0)
	for _, issue := range report.Issues {
		if issue.Severity == "high" {
			highIssues = append(highIssues, issue)
		}
	}
	return highIssues
}

// FormatValidationReport 格式化验证报告为可读文本
func FormatValidationReport(report *ValidationReport) string {
	if report == nil {
		return "验证报告为空"
	}

	var sb strings.Builder

	// 验证结果
	if report.Passed {
		sb.WriteString("✅ 验证通过\n")
	} else {
		sb.WriteString("❌ 验证失败\n")
	}

	// 置信度评分
	sb.WriteString(fmt.Sprintf("📊 置信度评分: %d/100\n", report.ConfidenceScore))

	// 问题列表
	if len(report.Issues) > 0 {
		sb.WriteString("\n## 发现的问题\n\n")
		for idx, issue := range report.Issues {
			severityIcon := "⚪"
			switch issue.Severity {
			case "high":
				severityIcon = "🔴"
			case "medium":
				severityIcon = "🟡"
			case "low":
				severityIcon = "⚪"
			}
			sb.WriteString(fmt.Sprintf("%d. %s [%s] %s\n", idx+1, severityIcon, issue.Category, issue.Description))
			if issue.Field != "" {
				sb.WriteString(fmt.Sprintf("   字段: %s\n", issue.Field))
			}
		}
	}

	// 改进建议
	if len(report.Suggestions) > 0 {
		sb.WriteString("\n## 改进建议\n\n")
		for idx, suggestion := range report.Suggestions {
			sb.WriteString(fmt.Sprintf("%d. %s\n", idx+1, suggestion))
		}
	}

	return sb.String()
}
