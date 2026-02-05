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
	"testing"

	"dbm-services/common/db-resource/internal/config"
)

// TestValidateAnalysisResult_Success 测试验证通过的情况
func TestValidateAnalysisResult_Success(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	result := &AnalysisResult{
		Summary: "磁盘类型不匹配导致资源不足",
		Reasons: []FailureReason{
			{
				Category:    "disk",
				Description: "磁盘类型不匹配",
				Impact:      "high",
			},
		},
		Suggestions: []Suggestion{
			{
				Type:           "adjust_disk",
				Description:    "调整磁盘类型要求",
				PredictedCount: 10,
				Verified:       true,
				Priority:       1,
			},
		},
	}

	report := validator.ValidateAnalysisResult(result)

	if !report.Passed {
		t.Errorf("Expected validation to pass, but it failed with score %d", report.ConfidenceScore)
	}

	if report.ConfidenceScore < 70 {
		t.Errorf("Expected confidence score >= 70, got %d", report.ConfidenceScore)
	}

	if len(report.Issues) > 0 {
		t.Logf("Unexpected issues found: %+v", report.Issues)
	}
}

// TestValidateAnalysisResult_ReasonMismatch 测试原因-建议不匹配
func TestValidateAnalysisResult_ReasonMismatch(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	result := &AnalysisResult{
		Summary: "磁盘类型不匹配",
		Reasons: []FailureReason{
			{
				Category:    "disk",
				Description: "磁盘类型不匹配",
				Impact:      "high",
			},
		},
		Suggestions: []Suggestion{
			{
				Type:        "add_resources", // 错误：应该是 adjust_disk
				Description: "补充资源",
				Priority:    1,
			},
		},
	}

	report := validator.ValidateAnalysisResult(result)

	if report.Passed {
		t.Error("Expected validation to fail due to reason mismatch")
	}

	foundMismatch := false
	for _, issue := range report.Issues {
		if issue.Category == "reason_mismatch" {
			foundMismatch = true
			break
		}
	}

	if !foundMismatch {
		t.Error("Expected to find reason_mismatch issue")
	}

	if report.ConfidenceScore >= 70 {
		t.Errorf("Expected confidence score < 70 due to mismatch, got %d", report.ConfidenceScore)
	}
}

// TestValidateAnalysisResult_QuantityLogic 测试数量逻辑验证
func TestValidateAnalysisResult_QuantityLogic(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	result := &AnalysisResult{
		Summary: "申请 10 台，可用 5 台",
		Reasons: []FailureReason{
			{
				Category:    "spec",
				Description: "申请 10 台，可用 5 台",
				Impact:      "high",
			},
		},
		Suggestions: []Suggestion{
			{
				Type:           "add_resources",
				Description:    "补充资源",
				PredictedCount: 7, // 错误：小于申请数量 10
				Priority:       1,
			},
		},
	}

	report := validator.ValidateAnalysisResult(result)

	foundQuantityIssue := false
	for _, issue := range report.Issues {
		if issue.Category == "quantity_logic" {
			foundQuantityIssue = true
			break
		}
	}

	if !foundQuantityIssue {
		t.Error("Expected to find quantity_logic issue")
	}
}

// TestValidateAnalysisResult_ForbiddenSuggestion 测试禁止建议检查
func TestValidateAnalysisResult_ForbiddenSuggestion(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	result := &AnalysisResult{
		Summary: "资源不足",
		Reasons: []FailureReason{
			{
				Category:    "spec",
				Description: "资源不足",
				Impact:      "high",
			},
		},
		Suggestions: []Suggestion{
			{
				Type:        "add_resources",
				Description: "建议降低申请数量", // 禁止的关键词
				Priority:    1,
			},
		},
	}

	report := validator.ValidateAnalysisResult(result)

	if report.Passed {
		t.Error("Expected validation to fail due to forbidden suggestion")
	}

	foundForbidden := false
	for _, issue := range report.Issues {
		if issue.Category == "forbidden" && issue.Severity == "high" {
			foundForbidden = true
			break
		}
	}

	if !foundForbidden {
		t.Error("Expected to find forbidden suggestion issue")
	}
}

// TestValidateAnalysisResult_Completeness 测试完整性检查
func TestValidateAnalysisResult_Completeness(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	result := &AnalysisResult{
		Summary:     "", // 缺少摘要
		Reasons:     []FailureReason{},
		Suggestions: []Suggestion{},
	}

	report := validator.ValidateAnalysisResult(result)

	if report.Passed {
		t.Error("Expected validation to fail due to incompleteness")
	}

	issues := map[string]bool{}
	for _, issue := range report.Issues {
		if issue.Category == "completeness" {
			issues[issue.Field] = true
		}
	}

	expectedFields := []string{"summary", "reasons", "suggestions"}
	for _, field := range expectedFields {
		if !issues[field] {
			t.Errorf("Expected completeness issue for field: %s", field)
		}
	}
}

// TestValidateAnalysisResult_PriorityReasoning 测试优先级合理性
func TestValidateAnalysisResult_PriorityReasoning(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	result := &AnalysisResult{
		Summary: "资源不足",
		Reasons: []FailureReason{
			{
				Category:    "spec",
				Description: "资源不足",
				Impact:      "high", // 高影响
			},
		},
		Suggestions: []Suggestion{
			{
				Type:        "add_resources",
				Description: "补充资源",
				Priority:    5, // 低优先级，与高影响不匹配
			},
		},
	}

	report := validator.ValidateAnalysisResult(result)

	foundPriorityIssue := false
	for _, issue := range report.Issues {
		if issue.Category == "priority_error" {
			foundPriorityIssue = true
			break
		}
	}

	if !foundPriorityIssue {
		t.Error("Expected to find priority_error issue")
	}
}

// TestExtractQuantityInfo 测试数量信息提取
func TestExtractQuantityInfo(t *testing.T) {
	tests := []struct {
		name            string
		result          *AnalysisResult
		wantRequest     int
		wantAvailable   int
	}{
		{
			name: "从摘要中提取",
			result: &AnalysisResult{
				Summary: "申请 10 台，可用 5 台",
			},
			wantRequest:   10,
			wantAvailable: 5,
		},
		{
			name: "从原因中提取",
			result: &AnalysisResult{
				Summary: "资源不足",
				Reasons: []FailureReason{
					{Description: "需要 15 台资源，但仅有 8 台"},
				},
			},
			wantRequest:   15,
			wantAvailable: 8,
		},
		{
			name: "无数量信息",
			result: &AnalysisResult{
				Summary: "配置错误",
			},
			wantRequest:   -1,
			wantAvailable: -1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotRequest, gotAvailable := extractQuantityInfo(tt.result)
			if gotRequest != tt.wantRequest {
				t.Errorf("extractQuantityInfo() request = %v, want %v", gotRequest, tt.wantRequest)
			}
			if gotAvailable != tt.wantAvailable {
				t.Errorf("extractQuantityInfo() available = %v, want %v", gotAvailable, tt.wantAvailable)
			}
		})
	}
}

// TestCalculateConfidenceScore 测试置信度评分计算
func TestCalculateConfidenceScore(t *testing.T) {
	validator := NewResourceAnalysisValidator(config.ValidatorConfig{
		Enabled:            true,
		MaxRefinements:     2,
		MinConfidenceScore: 70,
	})

	tests := []struct {
		name      string
		issues    []ValidationIssue
		wantScore int
	}{
		{
			name:      "无问题",
			issues:    []ValidationIssue{},
			wantScore: 100,
		},
		{
			name: "一个高严重性问题",
			issues: []ValidationIssue{
				{Severity: "high", Description: "测试问题"},
			},
			wantScore: 80,
		},
		{
			name: "两个中等严重性问题",
			issues: []ValidationIssue{
				{Severity: "medium", Description: "问题1"},
				{Severity: "medium", Description: "问题2"},
			},
			wantScore: 80,
		},
		{
			name: "混合严重性问题",
			issues: []ValidationIssue{
				{Severity: "high", Description: "高"},
				{Severity: "medium", Description: "中"},
				{Severity: "low", Description: "低"},
			},
			wantScore: 65, // 100 - 20 - 10 - 5 = 65
		},
		{
			name: "多个高严重性问题导致零分",
			issues: []ValidationIssue{
				{Severity: "high", Description: "问题1"},
				{Severity: "high", Description: "问题2"},
				{Severity: "high", Description: "问题3"},
				{Severity: "high", Description: "问题4"},
				{Severity: "high", Description: "问题5"},
				{Severity: "high", Description: "问题6"},
			},
			wantScore: 0, // 最低为 0
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			report := &ValidationReport{
				Issues: tt.issues,
			}
			validator.calculateConfidenceScore(report)
			if report.ConfidenceScore != tt.wantScore {
				t.Errorf("calculateConfidenceScore() = %v, want %v", report.ConfidenceScore, tt.wantScore)
			}
		})
	}
}

// TestFormatValidationReport 测试验证报告格式化
func TestFormatValidationReport(t *testing.T) {
	report := &ValidationReport{
		Passed:          false,
		ConfidenceScore: 75,
		Issues: []ValidationIssue{
			{
				Category:    "reason_mismatch",
				Severity:    "high",
				Description: "原因与建议不匹配",
				Field:       "suggestions",
			},
			{
				Category:    "quantity_logic",
				Severity:    "medium",
				Description: "数量逻辑错误",
				Field:       "suggestions.predicted_count",
			},
		},
		Suggestions: []string{
			"调整建议类型以匹配原因",
			"修正预测数量",
		},
	}

	formatted := FormatValidationReport(report)

	if formatted == "" {
		t.Error("Expected non-empty formatted report")
	}

	// 检查关键内容是否存在
	expectedContents := []string{
		"验证失败",
		"75/100",
		"原因与建议不匹配",
		"数量逻辑错误",
		"调整建议类型以匹配原因",
	}

	for _, expected := range expectedContents {
		if !contains(formatted, expected) {
			t.Errorf("Expected formatted report to contain '%s'", expected)
		}
	}
}

// 辅助函数：检查字符串是否包含子串
func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > len(substr) && containsHelper(s, substr))
}

func containsHelper(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
