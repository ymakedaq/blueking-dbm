/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package apply

import (
	"fmt"
	"strings"
	"time"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/go-pubpkg/logger"
)

// 根因判定置信度。三值语义：证据不足时不得折叠成确定结论。
const (
	VerdictCertain    = "certain"
	VerdictPartial    = "partial"
	VerdictUnknown    = "unknown"
	VerdictHypothesis = "hypothesis"
)

// 根因分类
const (
	CategoryConflictingGroupCities = "conflicting_group_cities"
	CategoryConflictingSubZones    = "conflicting_subzones"
	CategoryMissingIPs             = "missing_ips"
	CategorySubZonePriorityEmpty   = "subzone_priority_empty"
	CategoryEmptyPool              = "empty_pool"
	CategoryConditionBottleneck    = "condition_bottleneck"
	CategoryMultiCondition         = "multi_condition_combined"
	CategoryDiskNoMountPoint       = "disk_no_mount_point"
	CategoryAffinityDistribution   = "affinity_distribution"
	CategoryConcurrentCAS          = "concurrent_cas"
	CategoryAllocationGap          = "allocation_gap"
	CategoryUndetermined           = "undetermined"
)

// GroupLocationSnapshot 记录每个分组原始地域参数，用于判定参数自相矛盾
type GroupLocationSnapshot struct {
	GroupMark    string   `json:"group_mark"`
	City         string   `json:"city,omitempty"`
	SubZoneIds   []string `json:"sub_zone_ids,omitempty"`
	RequestCount int      `json:"request_count"`
}

// CounterfactualItem 逐个剔除某个匹配条件后，申请时刻资源池的剩余台数
type CounterfactualItem struct {
	SkippedCondition string `json:"skipped_condition"`
	Description      string `json:"description"`
	RemainingCount   int64  `json:"remaining_count"`
}

// RootCauseVerdict 申请失败那一刻算出来的确定性根因结论。
// 这是分析报告的权威事实源，LLM 与事后查询都不允许推翻它。
type RootCauseVerdict struct {
	Stage                string                  `json:"stage"`
	Category             string                  `json:"category"`
	Confidence           string                  `json:"confidence"`
	DecisiveCondition    string                  `json:"decisive_condition,omitempty"`
	ConditionDescription string                  `json:"condition_description,omitempty"`
	BeforeCount          int64                   `json:"before_count,omitempty"`
	AfterCount           int64                   `json:"after_count,omitempty"`
	RequestCount         int                     `json:"request_count"`
	Shortage             int                     `json:"shortage,omitempty"`
	Counterfactual       []CounterfactualItem    `json:"counterfactual,omitempty"`
	EvidenceText         string                  `json:"evidence_text,omitempty"`
	Summary              string                  `json:"summary"`
	Boundary             string                  `json:"boundary,omitempty"`
	ObservedAt           time.Time               `json:"observed_at"`
	GroupsInSameLocation bool                    `json:"groups_in_same_location,omitempty"`
	GroupLocations       []GroupLocationSnapshot `json:"group_locations,omitempty"`
	BaselineGroupMark    string                  `json:"baseline_group_mark,omitempty"`
	EffectiveIdcCitys    []string                `json:"effective_idc_citys,omitempty"`
	OverriddenGroups     []string                `json:"overridden_groups,omitempty"`
}

// AttachVerdict 在失败现场生成确定性根因结论。
// 只在申请请求内调用：param 提供参数自洽性证据，o 提供申请时刻资源池回放能力。
// 两者都可能为 nil（例如 CAS 抢占现场），此时按现场已有数据降级判定。
func AttachVerdict(evidence *ApplyFailureEvidence, param *RequestInputParam, o *SearchContext) {
	if evidence == nil || evidence.Verdict != nil {
		return
	}

	verdict := &RootCauseVerdict{
		Stage:        evidence.Stage,
		RequestCount: evidence.RequestCount,
		Category:     CategoryUndetermined,
		Confidence:   VerdictUnknown,
		ObservedAt:   time.Now(),
	}

	if param != nil {
		verdict.GroupsInSameLocation = param.GroupsInSameLocation
		verdict.GroupLocations = snapshotGroupLocations(*param)
		// 实现以 SortDetails 排序后的第一个分组为基准，不是原始 details[0]
		if sorted, sortErr := param.SortDetails(); sortErr == nil && len(sorted) > 0 {
			verdict.BaselineGroupMark = sorted[0].GroupMark
		}
	}
	if o != nil {
		verdict.EffectiveIdcCitys = append([]string(nil), o.IdcCitys...)
	}

	// L0：请求约束自洽性，纯参数判定，不查数据库。
	// 只有预筛阶段失败才把参数冲突作为主因；若已走到 affinity/cas，
	// 说明资源已挑出，参数冲突不是当时的直接原因。
	l0 := checkRequestConsistency(param, verdict.BaselineGroupMark)
	if l0 != nil && (evidence.Stage == "" || evidence.Stage == FailStagePickCheck) {
		mergeL0(verdict, l0)
		evidence.Verdict = verdict
		return
	}

	// L1：按失败层级分流，必要时对申请时刻资源池做反事实回放
	judgeByStage(verdict, evidence, o)
	if l0 != nil {
		verdict.Boundary = appendBoundary(verdict.Boundary,
			"参数层另发现约束冲突（非本次直接原因）："+l0.Summary)
	}
	evidence.Verdict = verdict
}

func appendBoundary(existing, extra string) string {
	if extra == "" {
		return existing
	}
	if existing == "" {
		return extra
	}
	return existing + "；" + extra
}

func snapshotGroupLocations(param RequestInputParam) []GroupLocationSnapshot {
	snapshots := make([]GroupLocationSnapshot, 0, len(param.Details))
	for _, d := range param.Details {
		snapshots = append(snapshots, GroupLocationSnapshot{
			GroupMark:    d.GroupMark,
			City:         d.LocationSpec.City,
			SubZoneIds:   append([]string(nil), d.LocationSpec.SubZoneIds...),
			RequestCount: d.Count,
		})
	}
	return snapshots
}

// checkRequestConsistency 判定 groups_in_same_location 模式下参数是否自相矛盾。
// 该模式只按第一个 detail 的城市计算候选园区（见 applyGroupsInSameLocation），
// 其余分组的 city 不生效，因此不同城市 + 同园区要求逻辑上不可能满足。
func checkRequestConsistency(param *RequestInputParam, baselineGroup string) *RootCauseVerdict {
	if param == nil || !param.GroupsInSameLocation || len(param.Details) < 2 {
		return nil
	}

	cities := make([]string, 0, len(param.Details))
	seenCity := map[string]bool{}
	for _, d := range param.Details {
		city := strings.TrimSpace(d.LocationSpec.City)
		if city == "" {
			continue
		}
		if !seenCity[city] {
			seenCity[city] = true
			cities = append(cities, city)
		}
	}

	if len(cities) > 1 {
		return &RootCauseVerdict{
			Category:   CategoryConflictingGroupCities,
			Confidence: VerdictCertain,
			Summary: fmt.Sprintf(
				"请求开启 groups_in_same_location=true，但 %d 个分组分别指定了不同城市（%s）。"+
					"该模式要求所有分组落在同一园区，且实现上只按排序后的第一个分组（group_mark=%s）的城市计算候选园区，"+
					"其余分组的 city 不生效。约束互斥，未进入资源筛选即失败。",
				len(param.Details), strings.Join(cities, " / "), baselineGroup),
			Boundary: "参数层判定，不依赖资源池数据；结论与申请时刻一致。",
		}
	}

	// 各分组指定了互不相交的园区，同样不可能落在同一园区
	if overlap := commonSubZones(param.Details); overlap != nil && len(overlap) == 0 {
		return &RootCauseVerdict{
			Category:   CategoryConflictingSubZones,
			Confidence: VerdictCertain,
			Summary: fmt.Sprintf(
				"请求开启 groups_in_same_location=true，但各分组指定的 sub_zone_ids 交集为空（分组数 %d），"+
					"无法找到同时满足所有分组的同一园区。", len(param.Details)),
			Boundary: "参数层判定，不依赖资源池数据；结论与申请时刻一致。",
		}
	}
	return nil
}

// commonSubZones 返回各分组指定园区的交集；任一分组未指定园区时返回 nil（不做判定）。
func commonSubZones(details []ObjectDetail) []string {
	var intersection []string
	initialized := false
	for _, d := range details {
		if d.LocationSpec.IsExclude() || len(d.LocationSpec.SubZoneIds) == 0 {
			return nil
		}
		if !initialized {
			intersection = append([]string(nil), d.LocationSpec.SubZoneIds...)
			initialized = true
			continue
		}
		allowed := map[string]bool{}
		for _, id := range d.LocationSpec.SubZoneIds {
			allowed[id] = true
		}
		next := make([]string, 0, len(intersection))
		for _, id := range intersection {
			if allowed[id] {
				next = append(next, id)
			}
		}
		intersection = next
	}
	if !initialized {
		return nil
	}
	return intersection
}

func mergeL0(verdict, l0 *RootCauseVerdict) {
	verdict.Category = l0.Category
	verdict.Confidence = l0.Confidence
	verdict.Summary = l0.Summary
	verdict.Boundary = l0.Boundary
	verdict.EvidenceText = "依据：ApplyFailureEvidence 与 RequestInputParam 原始入参（申请时刻）"
}

// firstBreach 找漏斗中首次跌破申请数量的那一步。
// before < 0 表示跌破发生在漏斗首步，之前没有观测值，不得伪造「从 0 台降下来」。
func firstBreach(funnel []MatchStageCount, requestCount int) (before, after int64, name, desc string, found bool) {
	prev := int64(-1)
	for i, st := range funnel {
		if st.Count < int64(requestCount) && (i == 0 || prev >= int64(requestCount)) {
			return prev, st.Count, st.Name, st.Description, true
		}
		prev = st.Count
	}
	return prev, prev, "", "", false
}

func countChangeText(before, after int64) string {
	if before < 0 {
		return fmt.Sprintf("漏斗首步后仅剩 %d 台", after)
	}
	return fmt.Sprintf("%d → %d 台", before, after)
}

func breachText(desc string, before, after int64, requestCount, shortage int) string {
	return fmt.Sprintf("【%s】使候选变为 %s（申请 %d 台，缺 %d 台）",
		desc, countChangeText(before, after), requestCount, shortage)
}

func judgeByStage(verdict *RootCauseVerdict, evidence *ApplyFailureEvidence, o *SearchContext) {
	switch evidence.Stage {
	case FailStagePickCheck:
		judgePickCheck(verdict, evidence, o)
	case FailStageEmptyMountDisk:
		verdict.Category = CategoryDiskNoMountPoint
		verdict.Confidence = VerdictCertain
		verdict.Summary = fmt.Sprintf(
			"SQL 预筛阶段有 %d 台候选，但按「未指定挂载点的磁盘需求」在内存阶段过滤后只剩 %d 台，申请 %d 台。"+
				"漏斗的 storage 步骤只覆盖带 mount_point 的 SQL 磁盘条件，不能按 SQL 剩余台数判定资源充足。",
			evidence.CandidateCount, evidence.PickedCount, evidence.RequestCount)
		verdict.Boundary = funnelStorageNote
	case FailStageAffinity:
		verdict.Category = CategoryAffinityDistribution
		verdict.Confidence = VerdictCertain
		verdict.Summary = affinitySummary(evidence)
		verdict.Boundary = "这是分布约束导致的不足，不是资源池总量不足，也不是并发抢占或锁定。"
	case FailStageCAS:
		verdict.Category = CategoryConcurrentCAS
		verdict.Confidence = VerdictCertain
		verdict.Summary = fmt.Sprintf(
			"机器已按亲和性挑出 %d 台，但把 status 从 Unused 改成预占用时被其他申请抢占（CAS 失败），申请 %d 台。"+
				"这是并发竞争，不是资源池资源不足。", evidence.PickedCount, evidence.RequestCount)
		verdict.Boundary = "只有 cas 层级才表示并发抢占；不得从 consume_time 反推锁定。"
	default:
		verdict.Category = CategoryUndetermined
		verdict.Confidence = VerdictUnknown
		verdict.Summary = "现场未记录可判定的失败层级，无法给出确定性根因。"
		verdict.Boundary = "证据不足，需补充现场采集；不得由模型推测后当作结论。"
	}
}

func judgePickCheck(verdict *RootCauseVerdict, evidence *ApplyFailureEvidence, o *SearchContext) {
	if len(evidence.MissingIps) > 0 {
		verdict.Category = CategoryMissingIPs
		verdict.Confidence = VerdictCertain
		verdict.Summary = fmt.Sprintf(
			"指定 bk_host_id 申请，其中 %d 个主机在资源池中不可用或不存在：%v。该路径未经按条件逐步筛选。",
			len(evidence.MissingIps), evidence.MissingIps)
		verdict.EvidenceText = "依据：PickCheckSpecialBkhostIds 返回的 MissingIps"
		return
	}

	if len(evidence.Funnel) == 0 {
		verdict.Category = CategorySubZonePriorityEmpty
		verdict.Confidence = VerdictCertain
		verdict.Summary = "合并各分组的园区优先级后候选园区为空，未执行按条件逐步筛选，因此没有漏斗数据。" +
			"失败发生在园区优先级合并阶段，而不是资源池总量不足。"
		verdict.Boundary = "该层级未采集逐步筛选漏斗；结论只覆盖「园区优先级为空」这一层。" +
			" 现场说明：" + evidence.Note
		return
	}

	before, after, name, desc, found := firstBreach(evidence.Funnel, evidence.RequestCount)
	if !found {
		verdict.Category = CategoryAllocationGap
		verdict.Confidence = VerdictPartial
		verdict.Summary = fmt.Sprintf(
			"漏斗各步剩余台数均未跌破申请数量 %d，但最终仍未分配满（候选 %d 台，实际 %d 台），"+
				"说明损失发生在 SQL 预筛之后的分配阶段。",
			evidence.RequestCount, evidence.CandidateCount, evidence.PickedCount)
		verdict.EvidenceText = "依据：ApplyFailureEvidence.Funnel / ProcessLogs"
		return
	}

	verdict.DecisiveCondition = name
	verdict.ConditionDescription = desc
	verdict.BeforeCount = before
	verdict.AfterCount = after
	if after < int64(evidence.RequestCount) {
		verdict.Shortage = evidence.RequestCount - int(after)
	}

	if after == 0 && before <= 0 {
		verdict.Category = CategoryEmptyPool
		verdict.Confidence = VerdictCertain
		verdict.Summary = fmt.Sprintf(
			"在【%s】处候选变为 0 台，云区域/状态/agent 状态等基础条件下没有可用资源，申请 %d 台。",
			desc, evidence.RequestCount)
		verdict.EvidenceText = "依据：ApplyFailureEvidence.Funnel 基础条件步"
		return
	}

	// 反事实回放：逐个剔除条件，确认是否存在单一决定性瓶颈
	counterfactual, err := collectCounterfactual(o)
	if err != nil {
		logger.Warn("collect counterfactual failed: %v", err)
		verdict.Category = CategoryMultiCondition
		verdict.Confidence = VerdictPartial
		verdict.Summary = fmt.Sprintf(
			"按申请 SQL 顺序叠加条件，%s，但反事实回放执行失败，无法确认它是否为唯一决定性条件。",
			breachText(desc, before, after, evidence.RequestCount, verdict.Shortage))
		verdict.Boundary = "count 下降依赖叠加顺序，单独一步下降不等于根因；反事实回放未完成。"
		return
	}
	verdict.Counterfactual = counterfactual
	verdict.EvidenceText = fmt.Sprintf(
		"依据：申请时刻按 matchSteps 逐步叠加 count(*)，首次跌破点 %s（%s）；"+
			"并逐个剔除条件回放 count(*) 共 %d 次。", name,
		countChangeText(before, after), len(counterfactual))

	for _, item := range counterfactual {
		if item.RemainingCount >= int64(evidence.RequestCount) {
			verdict.Category = CategoryConditionBottleneck
			verdict.Confidence = VerdictCertain
			verdict.DecisiveCondition = item.SkippedCondition
			verdict.ConditionDescription = item.Description
			verdict.Summary = fmt.Sprintf(
				"决定性瓶颈是【%s】：单独剔除该条件后，申请时刻资源池剩余 %d 台，可满足申请 %d 台。"+
					"（按原条件逐步筛选的首次跌破点是【%s】，%s）",
				item.Description, item.RemainingCount, evidence.RequestCount, desc,
				countChangeText(before, after))
			verdict.Boundary = "结论来自申请时刻资源池回放，可复跑；与事后查询当前资源池的结果无关。"
			return
		}
	}

	verdict.Category = CategoryMultiCondition
	verdict.Confidence = VerdictPartial
	verdict.Summary = fmt.Sprintf(
		"按申请 SQL 顺序叠加条件，%s；但逐个剔除任一条件后仍不足 %d 台，"+
			"属于多个条件叠加导致的资源不足，没有单一决定性条件。",
		breachText(desc, before, after, evidence.RequestCount, verdict.Shortage), evidence.RequestCount)
	verdict.Boundary = "count 下降依赖叠加顺序；已做逐条件剔除回放，未发现单一决定性条件。"
}

// collectCounterfactual 逐个跳过 matchSteps 中的一个条件，用干净的 db 重新叠加并 count。
// 必须每次新建 db：matchStep.fn 是在同一个 *gorm.DB 上累积 Where 的。
func collectCounterfactual(o *SearchContext) ([]CounterfactualItem, error) {
	if o == nil || model.DB == nil || model.DB.Self == nil || len(o.SpecialHostIds) > 0 {
		return nil, fmt.Errorf("counterfactual unavailable: missing search context or special host ids")
	}
	steps := o.matchSteps()
	items := make([]CounterfactualItem, 0, len(steps))
	for _, skip := range steps {
		db := model.DB.Self.Table(model.TbRpDetailName()).Select("count(*)")
		for _, step := range steps {
			if step.name == skip.name {
				continue
			}
			step.fn(db)
		}
		var count int64
		if err := db.Scan(&count).Error; err != nil {
			return nil, err
		}
		items = append(items, CounterfactualItem{
			SkippedCondition: skip.name,
			Description:      skip.desc,
			RemainingCount:   count,
		})
	}
	return items, nil
}

func affinitySummary(evidence *ApplyFailureEvidence) string {
	base := fmt.Sprintf(
		"SQL 预筛得到 %d 台候选（申请 %d 台，实际挑出 %d 台），资源总量足够，"+
			"但按亲和性约束在园区/机架/交换机上打散后无法凑满。",
		evidence.CandidateCount, evidence.RequestCount, evidence.PickedCount)
	if evidence.Distribution == nil {
		return base
	}
	d := evidence.Distribution
	return fmt.Sprintf(
		"%s候选分布：不同园区 %d 个、机架 %d 个、交换机 %d 个，亲和性类型 %s。",
		base, d.UniqueSubZones, d.UniqueRacks, d.UniqueNetDevices, d.AffinityType)
}
