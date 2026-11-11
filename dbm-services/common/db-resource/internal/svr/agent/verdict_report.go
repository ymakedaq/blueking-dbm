/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package agent

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/apply"
	"dbm-services/common/go-pubpkg/logger"

	"gorm.io/gorm"
)

// BuildAnalysisResultFromVerdict 把申请时刻算出的确定性结论转成分析结果。
// 这里的 Reasons / Summary 由代码生成，不经过 LLM，因此不会被模型改写或编造。
func BuildAnalysisResultFromVerdict(evidence *apply.ApplyFailureEvidence) *AnalysisResult {
	if evidence == nil || evidence.Verdict == nil {
		return nil
	}
	v := evidence.Verdict
	result := &AnalysisResult{
		Summary: v.Summary,
		Reasons: []FailureReason{
			{
				Category:    v.Category,
				Description: v.Summary,
				Impact:      "high",
				Data:        v,
			},
		},
		Suggestions: suggestionForVerdict(v),
		Verification: &VerificationInfo{
			AllVerified: v.Confidence == apply.VerdictCertain,
			Confidence:  confidenceLabel(v.Confidence),
		},
	}
	formatAnalysisResult(result)
	result.MarkdownText = RenderVerdictMarkdown(v)
	return result
}

// verdictAsKnownFacts 把已确定的现场事实注入探索链路的提示词。
// 证据不足时模型仍可发散推导，但不得推翻申请时刻算出的结论。
func verdictAsKnownFacts(v *apply.RootCauseVerdict) string {
	return fmt.Sprintf(`

## 已确定的现场事实（不得推翻）

以下内容来自申请时刻的现场与资源池回放，属于已知事实。你可以继续探索其他可能性，
但不得推翻、改写或忽略这些事实，也不得用事后查询当前资源池的结果覆盖它们。

- 置信度：%s
- 已知结论：%s
- 依据：%s
- 观测边界：%s
`, v.Confidence, v.Summary, v.EvidenceText, v.Boundary)
}

func confidenceLabel(confidence string) string {
	switch confidence {
	case apply.VerdictCertain:
		return "high"
	case apply.VerdictPartial, apply.VerdictHypothesis:
		return "medium"
	default:
		return "low"
	}
}

func suggestionForVerdict(v *apply.RootCauseVerdict) []Suggestion {
	var st string
	var desc string
	switch v.Category {
	case apply.CategoryConflictingGroupCities, apply.CategoryConflictingSubZones:
		st, desc = string(SuggestionContactAdmin),
			"统一各分组的地域参数后重试，或在不需要同园区约束时关闭 groups_in_same_location"
	case apply.CategoryEmptyPool, apply.CategoryConditionBottleneck, apply.CategoryMultiCondition,
		apply.CategoryAffinityDistribution:
		st, desc = string(SuggestionAddResources), "按判定出的瓶颈条件补充对应资源，或调整该条件的取值范围"
	case apply.CategoryDiskNoMountPoint:
		st, desc = string(SuggestionAdjustDisk), "检查未指定挂载点的磁盘需求与实际机器磁盘分布是否匹配"
	case apply.CategoryMissingIPs, apply.CategoryConcurrentCAS, apply.CategoryAllocationGap,
		apply.CategorySubZonePriorityEmpty, apply.CategoryUndetermined:
		st, desc = string(SuggestionContactAdmin), "按上述判定结论核对申请参数或联系管理员排查"
	default:
		st, desc = string(SuggestionContactAdmin), "按上述判定结论核对申请参数或联系管理员排查"
	}
	suggestions := []Suggestion{{Type: st, Description: desc, Verified: false, Priority: 1}}
	return FilterSuggestions(suggestions)
}

// RenderVerdictMarkdown 用确定性结论直接渲染报告。
// certain 与 partial/unknown 分区呈现，未确定的内容不会被包装成结论。
func RenderVerdictMarkdown(v *apply.RootCauseVerdict) string {
	var b strings.Builder
	b.WriteString("# 资源申请分析报告\n\n")

	if v.Confidence == apply.VerdictCertain {
		b.WriteString("## 已确认根因\n\n")
	} else {
		b.WriteString("## 待验证线索（未形成确定结论）\n\n")
		b.WriteString("> 证据不足，以下内容不是确定根因，请勿据此直接调整申请参数。\n\n")
	}
	b.WriteString(v.Summary)
	b.WriteString("\n\n")

	b.WriteString("## 判定信息\n\n")
	b.WriteString(fmt.Sprintf("- 分类：`%s`\n", v.Category))
	b.WriteString(fmt.Sprintf("- 置信度：`%s`\n", v.Confidence))
	b.WriteString(fmt.Sprintf("- 失败层级：`%s`\n", v.Stage))
	b.WriteString(fmt.Sprintf("- 申请数量：%d 台\n", v.RequestCount))
	if v.Shortage > 0 {
		b.WriteString(fmt.Sprintf("- 缺口：%d 台\n", v.Shortage))
	}
	if v.DecisiveCondition != "" {
		b.WriteString(fmt.Sprintf("- 决定性条件：`%s`（%s）\n",
			v.DecisiveCondition, v.ConditionDescription))
	}
	if v.BeforeCount < 0 {
		b.WriteString(fmt.Sprintf("- 首次跌破点：漏斗首步后仅剩 %d 台\n", v.AfterCount))
	} else if v.BeforeCount != 0 || v.AfterCount != 0 {
		b.WriteString(fmt.Sprintf("- 首次跌破点：%d → %d 台\n", v.BeforeCount, v.AfterCount))
	}
	b.WriteString(fmt.Sprintf("- 判定时刻：%s\n\n", v.ObservedAt.Format(time.RFC3339)))

	if v.EvidenceText != "" {
		b.WriteString("## 证据\n\n")
		b.WriteString(v.EvidenceText)
		b.WriteString("\n\n")
	}

	if len(v.GroupLocations) > 0 {
		b.WriteString("### 各分组原始地域参数\n\n")
		for _, g := range v.GroupLocations {
			b.WriteString(fmt.Sprintf("- `%s`：city=%q，sub_zone_ids=%v，申请 %d 台\n",
				g.GroupMark, g.City, g.SubZoneIds, g.RequestCount))
		}
		b.WriteString("\n")
		if v.GroupsInSameLocation {
			b.WriteString(fmt.Sprintf("> groups_in_same_location=true，实现上以第一个分组（`%s`）的城市为基准，生效城市=%v。\n\n",
				v.BaselineGroupMark, v.EffectiveIdcCitys))
		}
	}

	if len(v.Counterfactual) > 0 {
		b.WriteString("### 逐条件剔除回放（申请时刻）\n\n")
		b.WriteString("| 剔除的条件 | 剩余台数 | 是否满足申请 |\n|---|---|---|\n")
		for _, item := range v.Counterfactual {
			satisfied := "否"
			if item.RemainingCount >= int64(v.RequestCount) {
				satisfied = "是"
			}
			b.WriteString(fmt.Sprintf("| %s | %d | %s |\n",
				item.SkippedCondition, item.RemainingCount, satisfied))
		}
		b.WriteString("\n")
	}

	if v.Boundary != "" {
		b.WriteString("## 观测边界\n\n")
		b.WriteString(v.Boundary)
		b.WriteString("\n\n")
	}

	b.WriteString("## 处理建议\n\n")
	for _, s := range suggestionForVerdict(v) {
		b.WriteString(fmt.Sprintf("- %s\n", s.Description))
	}
	b.WriteString("\n")
	return b.String()
}

// PersistVerdictReport 在申请请求内把确定性结论同步落库。
// 不依赖 AnalysisTaskChan：即使 channel 满被丢弃、LLM 未启用或分析超时，
// 前端轮询 /resource/analysis/result 仍能看到真实根因。
func PersistVerdictReport(billID string, applyParams json.RawMessage,
	evidence *apply.ApplyFailureEvidence) error {
	if billID == "" || evidence == nil || evidence.Verdict == nil ||
		model.DB == nil || model.DB.Self == nil {
		return nil
	}
	result := BuildAnalysisResultFromVerdict(evidence)
	if result == nil {
		return nil
	}
	resultJSON, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("marshal verdict report failed: %w", err)
	}
	if len(applyParams) == 0 {
		applyParams = json.RawMessage(`{}`)
	}

	db := model.DB.Self
	var existing model.TbRpAnalysisResult
	findErr := db.Where("bill_id = ?", billID).First(&existing).Error
	now := time.Now()

	if findErr == gorm.ErrRecordNotFound {
		record := model.TbRpAnalysisResult{
			BillId:         billID,
			ApplyParams:    applyParams,
			AnalysisResult: resultJSON,
			MarkdownText:   result.MarkdownText,
			Status:         model.AnalysisStatusCompleted,
			CreateTime:     now,
			UpdateTime:     now,
		}
		if err := db.Create(&record).Error; err != nil {
			return fmt.Errorf("create verdict report failed: %w", err)
		}
		return nil
	}
	if findErr != nil {
		return fmt.Errorf("query analysis record failed: %w", findErr)
	}

	// 已有完成的分析结果时保持幂等，不覆盖、不重跑
	if existing.Status == model.AnalysisStatusCompleted && len(existing.AnalysisResult) > 0 {
		return nil
	}
	if err := db.Model(&model.TbRpAnalysisResult{}).
		Where("bill_id = ?", billID).
		Updates(map[string]interface{}{
			"apply_params":    applyParams,
			"analysis_result": resultJSON,
			"markdown_text":   result.MarkdownText,
			"status":          model.AnalysisStatusCompleted,
			"error_msg":       "",
			"update_time":     now,
		}).Error; err != nil {
		return fmt.Errorf("update verdict report failed: %w", err)
	}
	logger.Info("verdict report persisted for bill %s, category=%s, confidence=%s",
		billID, evidence.Verdict.Category, evidence.Verdict.Confidence)
	return nil
}
