/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package applyv2

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"dbm-services/common/db-resource/internal/controller"
	"dbm-services/common/db-resource/internal/lock"
	"dbm-services/common/db-resource/internal/middleware"
	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/apply"
	"dbm-services/common/db-resource/internal/svr/bk"
	"dbm-services/common/db-resource/internal/svr/meta"
	"dbm-services/common/db-resource/internal/svr/task"
	"dbm-services/common/go-pubpkg/cmutil"
	"dbm-services/common/go-pubpkg/errno"
	"dbm-services/common/go-pubpkg/logger"

	"github.com/gin-gonic/gin"
	"github.com/samber/lo"
	"gorm.io/gorm"
)

func init() {
	middleware.RequestBodyLoggerFilter.Add("/v2/resource/apply")
}

// ApplyHandlerV2 v2 申请处理器
type ApplyHandlerV2 struct {
	controller.BaseHandler
}

// RegisterRouter 注册路由
func (c *ApplyHandlerV2) RegisterRouter(engine *gin.Engine) {
	r := engine.Group("v2/resource")
	{
		r.POST("/apply", c.ApplyResourceV2)
	}
}

func newLocker(key string, requestId string) *lock.SpinLock {
	redisLock := lock.NewRedisLock(key, requestId, 120*time.Second)
	return lock.NewSpinLock(redisLock, 60, 350*time.Millisecond)
}

// ApplyResourceV2 v2 申请资源入口
func (c *ApplyHandlerV2) ApplyResourceV2(r *gin.Context) {
	logger.Info("start apply resource v2 ...")
	requestID := r.GetString("request_id")
	var param RequestInputParamV2
	var pickers []*apply.PickerObject
	var err error

	if c.Prepare(r, &param) != nil {
		return
	}

	// v2 参数校验；旧字段按 README 约定静默忽略
	if err = param.ParamCheck(); err != nil {
		c.SendResponse(r, errno.ErrApplyResourceParamCheck.AddErr(err), err.Error())
		return
	}

	// 获取资源锁
	if !param.DryRun {
		lock := newLocker(param.LockKey(), requestID)
		if err = lock.Lock(); err != nil {
			c.SendResponse(r, errno.ErrResourceLock.AddErr(err), err.Error())
			return
		}
		defer func() {
			if err = lock.Unlock(); err != nil {
				logger.Error(fmt.Sprintf("unlock failed %s", err.Error()))
				return
			}
		}()
	}

	// 失败时回滚
	defer func() {
		if err != nil {
			apply.RollBackAllInstanceUnused(pickers)
		}
	}()

	// v2 核心申请逻辑
	pickers, err = CycleApplyV2(param)
	if err != nil {
		// 与 v1 保持一致：失败时投递异步分析任务，带上参数与失败现场
		if paramsJSON, marshalErr := json.Marshal(param); marshalErr == nil {
			var evidenceJSON json.RawMessage
			var insufficient *apply.ResourceInsufficientError
			if errors.As(err, &insufficient) {
				if b, evErr := json.Marshal(insufficient.Evidence); evErr == nil {
					evidenceJSON = b
				} else {
					logger.Warn("marshal v2 apply failure evidence failed for bill %s: %v", param.BillId, evErr)
				}
			}
			select {
			case task.AnalysisTaskChan <- task.AnalysisTaskItem{
				BillID:      param.BillId,
				ApplyParams: paramsJSON,
				Evidence:    evidenceJSON,
			}:
				logger.Info("Async analysis task triggered for v2 bill: %s", param.BillId)
			default:
				logger.Warn("Analysis task channel is full, skip v2 analysis for bill: %s", param.BillId)
			}
		} else {
			logger.Error("Failed to marshal v2 apply params for bill %s: %s", param.BillId, marshalErr.Error())
		}

		var insufficient *apply.ResourceInsufficientError
		if errors.As(err, &insufficient) {
			c.SendResponse(r, errno.ErrResourceinsufficient.Add(param.BuildMessage()+"\n"+err.Error()), "")
		} else {
			c.SendResponse(r, err, "")
		}
		return
	}

	if param.DryRun {
		// dry-run 全程只读，CycleApplyV2 不写 Preselected，无需回滚
		c.SendResponse(r, nil, map[string]interface{}{"check_success": true})
		return
	}

	// 锁定并返回结果（复用 v1 的 LockReturnPickers）
	data, err := apply.LockReturnPickers(pickers, model.Used)
	if err != nil {
		c.SendResponse(r, errno.ErresourceLockReturn.AddErr(err), nil)
		return
	}

	logger.Info(fmt.Sprintf("The %s, will return %d machines", requestID, len(data)))

	task.ApplyResponseLogChan <- task.ApplyResponseLogItem{
		RequestId: requestID,
		Data:      data,
	}
	task.RecordRsOperatorInfoChan <- param.GetOperationInfo(requestID, model.Used, data)

	// v2 响应按全局 affinity_dimension 决定主维映射（整单一个维度，无需逐分组拼映射）
	responseData := buildV2ResponseByDimension(data, param.AffinityDimension)
	c.SendResponse(r, nil, responseData)
}

// buildV2ResponseByDimension 按请求级 affinity_dimension 构建响应（整单一个维度）：
// - bk_cloud_zone（默认，cloud 模式）：明细的 SubZone/SubZoneID 映射为 bk_cloud_zone，Summary 只输出云区维度；
// - sub_zone（legacy 模式）：保留真实 SubZone/SubZoneID，Summary 同时输出 by_sub_zone 与 by_cloud_zone。
func buildV2ResponseByDimension(data []model.BatchGetTbDetailResult, dimension string) []model.BatchGetTbDetailResult {
	for i := range data {
		byCloudZone := make(map[string]int)
		byCloudZoneRack := make(map[string]map[string]int)
		bySubZone := make(map[string]int)
		bySubZoneRack := make(map[string]map[string]int)

		for j := range data[i].Data {
			zone := data[i].Data[j].BkCloudZone
			rack := data[i].Data[j].RackID

			if dimension == "sub_zone" {
				// sub_zone 维度：保留真实园区值，不覆盖为云区
				subZone := data[i].Data[j].SubZoneID
				if subZone == "" {
					subZone = data[i].Data[j].SubZone
				}
				bySubZone[subZone]++
				if bySubZoneRack[subZone] == nil {
					bySubZoneRack[subZone] = make(map[string]int)
				}
				bySubZoneRack[subZone][rack]++
			} else {
				// 默认 bk_cloud_zone 维度：映射明细旧字段为主维值（避免下游误读）
				data[i].Data[j].SubZone = zone
				data[i].Data[j].SubZoneID = zone
			}

			// 两种维度都统计云可用区分布，便于下游统一消费
			byCloudZone[zone]++
			if byCloudZoneRack[zone] == nil {
				byCloudZoneRack[zone] = make(map[string]int)
			}
			byCloudZoneRack[zone][rack]++
		}

		if dimension == "sub_zone" {
			data[i].Summary = map[string]interface{}{
				"by_sub_zone":        bySubZone,
				"by_sub_zone_rack":   bySubZoneRack,
				"by_cloud_zone":      byCloudZone,
				"by_cloud_zone_rack": byCloudZoneRack,
			}
		} else {
			data[i].Summary = map[string]interface{}{
				"by_cloud_zone":      byCloudZone,
				"by_cloud_zone_rack": byCloudZoneRack,
			}
		}

		logger.Info("Group %s Summary: dimension=%s by_cloud_zone=%v, by_cloud_zone_rack=%v",
			data[i].Item, dimension, byCloudZone, byCloudZoneRack)
	}

	return data
}

// CycleApplyV2 v2 循环申请逻辑。
// 位置模式由请求级 param.AffinityDimension 决定（cloud / legacy），两种模式都复用 v1 选机核心，
// 区别只在于：过滤用哪组位置字段、分桶主维是什么（见 toSearchContext / adaptCandidatesToV1Format 注释）。
func CycleApplyV2(param RequestInputParamV2) (pickers []*apply.PickerObject, err error) {
	if err = param.ValidateUniqueGroupMarks(); err != nil {
		return nil, err
	}

	// legacy 模式下预解析逻辑城市（每单一次远程调用，dev 模式跳过），失败整单返回；
	// 解析结果缓存在各 detail 的 LocationSpec.IdcCitys，后续 pickCheck/查询/枚举各环节复用
	if err = param.ResolveIdcCitys(); err != nil {
		return nil, err
	}

	// dry-run 全程只读：用内存集合避免同一台机器被多个分组重复选中
	dryRunUsedHosts := make(map[int]struct{})

	// 检查是否需要多组同位置（与 v1 一致：仅同园区/同园区跨交换机亲和才走同位置逻辑）
	affinitys := lo.Uniq(param.GetAllAffinitys())
	if param.GroupsInSameLocation && len(param.Details) > 1 && len(affinitys) == 1 &&
		lo.Contains([]string{apply.SAME_SUBZONE, apply.SAME_SUBZONE_CROSS_SWTICH}, affinitys[0]) {
		return applyGroupsInSameLocationV2(param)
	}

	// 单组或多组不同位置逻辑：约束更具体的组优先，避免宽松组抢占严格组资源
	for _, detail := range param.SortDetailsV2() {
		var picker *apply.PickerObject

		detail.Affinity = apply.NormalizeAffinity(detail.Affinity)

		// 前置计数与漏斗：与 v1 PickCheck 的可观察行为对齐
		if err = pickCheckV2(param.ForbizId, param.ResourceType, detail, param.AffinityDimension, dryRunUsedHosts); err != nil {
			return pickers, err
		}

		candidates, err := queryCandidatesV2(param.ForbizId, param.ResourceType, detail, param.AffinityDimension)
		if err != nil {
			return pickers, err
		}
		if param.DryRun {
			candidates = filterDryRunCandidates(candidates, dryRunUsedHosts)
		}

		diskSpecs := meta.GetEmptyDiskSpec(detail.StorageSpecs)
		if len(diskSpecs) > 0 {
			candidates, err = apply.FilterEmptyMountPointStorage(candidates, detail.StorageSpecs, diskSpecs)
			if err != nil {
				logger.Error("filter empty mount point storage failed %s", err.Error())
				return pickers, err
			}
		}

		adaptedCandidates, err := adaptCandidatesToV1Format(candidates, param.AffinityDimension)
		if err != nil {
			return pickers, fmt.Errorf("detail %s: %w", detail.GroupMark, err)
		}

		picker = apply.NewPicker(detail.Count, detail.GroupMark)
		if err = pickInstanceBaseV2(picker, adaptedCandidates, detail, param.AffinityDimension); err != nil {
			return pickers, err
		}

		if !picker.PickerDone() {
			return pickers, newV2ResourceInsufficientError(detail, adaptedCandidates, picker, nil, param.AffinityDimension)
		}

		if param.DryRun {
			markDryRunUsedHosts(picker, dryRunUsedHosts)
		} else if err := preselectedWithRollbackOnPartialFailure(picker, detail.GroupMark); err != nil {
			return pickers, err
		}

		pickers = append(pickers, picker)
	}

	return pickers, nil
}

// applyGroupsInSameLocationV2 v2 多组同位置申请。
// 位置模式由请求级 affinity_dimension 决定（整单统一）：
//   - legacy 模式（sub_zone）：在同一 city 下枚举候选园区（getAllAvailableSubZones），
//     逐个园区用 ForcedSubZoneIds 锁定所有分组，保证所有分组落在同一园区（对齐 v1 语义）；
//   - cloud 模式：枚举候选云可用区（指定 zone 列表或 getAllAvailableZones），
//     逐个 zone 改写各分组 BkCloudZoneList，保证所有分组落在同一云可用区，分组内部可跨园区。
//
// 任一候选位置下所有分组都选机成功才算成功；全部候选失败时返回最后一个失败现场。
func applyGroupsInSameLocationV2(param RequestInputParamV2) (pickers []*apply.PickerObject, err error) {
	// 与顺序分支保持一致：约束更具体的组优先，避免宽松组先占用严格组资源
	sortedDetails := param.SortDetailsV2()
	if len(sortedDetails) == 0 {
		return nil, fmt.Errorf("no details in same location apply")
	}

	firstDetail := sortedDetails[0]
	firstDetail.Affinity = apply.NormalizeAffinity(firstDetail.Affinity)
	bySubZone := locationMode(param.AffinityDimension) == AffinityDimensionSubZone

	var candidateLocations []string
	if bySubZone {
		// legacy 模式必须锁定同一个 sub_zone，不能只锁云可用区
		candidateLocations, err = getAllAvailableSubZones(param.ForbizId, param.ResourceType, firstDetail, param.AffinityDimension)
	} else {
		candidateLocations = firstDetail.LocationSpec.BkCloudZoneList
		if len(candidateLocations) == 0 {
			candidateLocations, err = getAllAvailableZones(param.ForbizId, param.ResourceType, firstDetail, param.AffinityDimension)
		}
	}
	if err != nil {
		return nil, err
	}
	if len(candidateLocations) == 0 {
		return nil, newV2SameLocationError(sortedDetails, "no zone/subzone can satisfy all groups in same location",
			param.AffinityDimension)
	}

	var lastFailure error
	for _, candidate := range candidateLocations {
		pickers = nil
		dryRunUsedHosts := make(map[int]struct{})
		success := true

		for _, detail := range sortedDetails {
			detailCopy := detail
			detailCopy.Affinity = apply.NormalizeAffinity(detailCopy.Affinity)
			if bySubZone {
				detailCopy.LocationSpec.ForcedSubZoneIds = []string{candidate}
			} else {
				detailCopy.LocationSpec.BkCloudZoneList = []string{candidate}
			}

			if err = pickCheckV2(param.ForbizId, param.ResourceType, detailCopy, param.AffinityDimension, dryRunUsedHosts); err != nil {
				if isResourceInsufficientError(err) {
					lastFailure = err
					success = false
					break
				}
				apply.RollBackAllInstanceUnused(pickers)
				return nil, err
			}

			candidates, queryErr := queryCandidatesV2(param.ForbizId, param.ResourceType, detailCopy, param.AffinityDimension)
			if queryErr != nil {
				apply.RollBackAllInstanceUnused(pickers)
				return nil, queryErr
			}
			if param.DryRun {
				candidates = filterDryRunCandidates(candidates, dryRunUsedHosts)
			}

			diskSpecs := meta.GetEmptyDiskSpec(detailCopy.StorageSpecs)
			if len(diskSpecs) > 0 {
				candidates, err = apply.FilterEmptyMountPointStorage(candidates, detailCopy.StorageSpecs, diskSpecs)
				if err != nil {
					apply.RollBackAllInstanceUnused(pickers)
					return nil, err
				}
			}

			adaptedCandidates, adaptErr := adaptCandidatesToV1Format(candidates, param.AffinityDimension)
			if adaptErr != nil {
				apply.RollBackAllInstanceUnused(pickers)
				return nil, fmt.Errorf("detail %s: %w", detailCopy.GroupMark, adaptErr)
			}

			picker := apply.NewPicker(detailCopy.Count, detailCopy.GroupMark)
			if err = pickInstanceBaseV2(picker, adaptedCandidates, detailCopy, param.AffinityDimension); err != nil {
				if isResourceInsufficientError(err) {
					lastFailure = err
					success = false
					break
				}
				apply.RollBackAllInstanceUnused(pickers)
				return nil, err
			}

			if !picker.PickerDone() {
				lastFailure = newV2ResourceInsufficientError(detailCopy, adaptedCandidates, picker, nil, param.AffinityDimension)
				success = false
				break
			}

			if param.DryRun {
				markDryRunUsedHosts(picker, dryRunUsedHosts)
			} else if err := preselectedWithRollbackOnPartialFailure(picker, detailCopy.GroupMark); err != nil {
				apply.RollBackAllInstanceUnused(pickers)
				return nil, err
			}

			pickers = append(pickers, picker)
		}

		if success {
			return pickers, nil
		}
		apply.RollBackAllInstanceUnused(pickers)
	}

	if lastFailure != nil {
		return nil, lastFailure
	}
	return nil, newV2SameLocationError(sortedDetails, "no zone/subzone can satisfy all groups in same location",
		param.AffinityDimension)
}

// toSearchContext v2 detail 转为 v1 SearchContext。
// dimension 为请求级 affinity_dimension（入口 NormalizeDefaults 已归一化），决定位置字段映射方式：
//   - cloud 模式：只填 SearchContext 的 BkCloudRegion/BkCloudZoneList/ExcludeBkCloudZoneList 槽位，
//     由 MatchBkCloudLocationSpec 消费；v1 ObjectDetail.LocationSpec 保持零值（v1 匹配函数不感知）；
//   - legacy 模式：把 LocationSpecV2 的 legacy 字段映射到 v1 ObjectDetail.LocationSpec（meta.LocationSpec），
//     由 v1 MatchLocationSpec 消费；同时带上 ResolveIdcCitys 预解析的 IdcCitys（逻辑城市转换结果）。
//
// 两种模式下 ForcedSubZoneIds（同位置锁定园区，仅内部使用）都走 v1 的 SpecialSubZoneIds 槽位。
// forbizId/rsType 仅供 SQL 过滤类调用方使用；pickInstanceBaseV2 场景传零值（不影响选机）。
func toSearchContext(forbizId int, rsType string, detail ObjectDetailV2, dimension string) *apply.SearchContext {
	objDetail := &apply.ObjectDetail{
		BkCloudId:     detail.BkCloudId,
		GroupMark:     detail.GroupMark,
		Affinity:      detail.Affinity,
		Count:         detail.Count,
		Tolerance:     detail.Tolerance,
		CurrentHosts:  convertCurrentHostsV2ToV1(detail.CurrentHosts, dimension),
		Spec:          detail.Spec,
		StorageSpecs:  detail.StorageSpecs,
		DeviceClass:   detail.DeviceClass,
		Labels:        detail.Labels,
		OsType:        detail.OsType,
		OsNames:       detail.OsNames,
		ExcludeOsName: detail.ExcludeOsName,
		Hosts:         detail.Hosts,
	}
	if locationMode(dimension) == AffinityDimensionSubZone {
		// legacy 模式：位置字段映射到 v1 槽位，复用 v1 MatchLocationSpec 的城市/园区过滤语义
		objDetail.LocationSpec = meta.LocationSpec{
			City:                detail.LocationSpec.City,
			SubZoneIds:          detail.LocationSpec.SubZoneIds,
			ExcludeSubZoneIds:   detail.LocationSpec.ExcludeSubZoneIds,
			ExcludeRackIds:      detail.LocationSpec.ExcludeRackIds,
			ExcludeNetDeviceIds: detail.LocationSpec.ExcludeNetDeviceIds,
		}
	}
	return &apply.SearchContext{
		ObjectDetail:           objDetail,
		RsType:                 model.NormalizeResourceType(rsType),
		IntentionBkBizId:       forbizId,
		SpecialHostIds:         detail.Hosts.GetBkHostIds(),
		SpecialSubZoneIds:      detail.LocationSpec.ForcedSubZoneIds,
		IdcCitys:               detail.LocationSpec.IdcCitys,
		AffinityDimension:      dimension,
		BkCloudRegion:          detail.LocationSpec.BkCloudRegion,
		BkCloudZoneList:        detail.LocationSpec.BkCloudZoneList,
		ExcludeBkCloudZoneList: detail.LocationSpec.ExcludeBkCloudZoneList,
	}
}

// queryCandidatesV2 v2 查询候选机器（复用 v1 匹配步骤）。
// 位置过滤按 dimension（请求级 affinity_dimension）选择：
//   - cloud 模式：MatchBkCloudLocationSpec（bk_cloud_region 等值 + zone 列表 IN / NOT IN）；
//   - legacy 模式：v1 MatchLocationSpec（IdcCitys/city + sub_zone_id in/not in +
//     排除园区/机架/网络设备；SpecialSubZoneIds 在未指定 sub_zone 时生效，即同位置锁定园区）。
//
// ForcedSubZoneIds 的显式 where 保留作为兜底：legacy 模式下与 MatchLocationSpec 的
// SpecialSubZoneIds 分支重复但等价（同一条件过滤两次，无语义差异）。
// 指定 bk_host_id 的场景与 v1 一致：只按 id 查，不再叠加规格/位置等过滤。
func queryCandidatesV2(forbizId int, rsType string, detail ObjectDetailV2, dimension string) ([]model.TbRpDetail, error) {
	var items []model.TbRpDetail

	// 构造 SearchContext，复用 v1 匹配逻辑
	searchCtx := toSearchContext(forbizId, rsType, detail, dimension)

	db := model.DB.Self.Table(model.TbRpDetailName())

	if len(searchCtx.SpecialHostIds) > 0 {
		// 指定 bk_host_id 与 v1 一致：只按 id 查，不再叠加规格/位置等过滤
		db.Where("bk_host_id in (?) and status = ? and gse_agent_status_code = ?",
			searchCtx.SpecialHostIds, model.Unused, bk.GseAlive)
	} else {
		// 基础过滤（状态 + GSE）
		db.Where("bk_cloud_id = ? and status = ? and gse_agent_status_code = ?",
			searchCtx.BkCloudId, model.Unused, bk.GseAlive)

		searchCtx.MatchIntentionBkBiz(db)
		searchCtx.MatchRsType(db)
		searchCtx.MatchOsType(db)
		searchCtx.MatchOsName(db)
		searchCtx.MatchLabels(db)

		// v2 位置匹配：cloud 模式走云区匹配，legacy 模式走 v1 城市/园区匹配
		if locationMode(dimension) == AffinityDimensionSubZone {
			searchCtx.MatchLocationSpec(db)
		} else {
			searchCtx.MatchBkCloudLocationSpec(db)
		}
		if len(detail.LocationSpec.ForcedSubZoneIds) > 0 {
			db.Where("sub_zone_id in (?)", detail.LocationSpec.ForcedSubZoneIds)
		}

		// 磁盘匹配（带挂载点的）
		searchCtx.MatchStorage(db)

		// 规格匹配
		searchCtx.MatchSpec(db)

		switch detail.Affinity {
		case apply.SAME_SUBZONE_CROSS_SWTICH:
			searchCtx.UseNetDeviceIsNotEmpty(db)
		case apply.CROSS_RACK, apply.CROSS_SUBZONE_STRONG, apply.CROSS_SUBZONE_WEAK:
			searchCtx.RackIdIsNotEmpty(db)
		}
	}

	if err := db.Scan(&items).Error; err != nil {
		logger.Error("query candidates v2 failed: %s", err.Error())
		return nil, errno.ErrDBQuery.AddErr(err)
	}

	// 指定主机不走主维空值过滤，与 v1 保持一致
	if len(searchCtx.SpecialHostIds) == 0 {
		items = filterEmptyDimensionCandidates(items, dimension)
	}

	return items, nil
}

// filterEmptyDimensionCandidates 踢掉主维为空的候选
func filterEmptyDimensionCandidates(items []model.TbRpDetail, dimension string) []model.TbRpDetail {
	// dimension 空则默认 bk_cloud_zone（入口 NormalizeDefaults 已保证）
	if dimension == "" {
		dimension = "bk_cloud_zone"
	}

	switch dimension {
	case "bk_cloud_zone":
		return lo.Filter(items, func(item model.TbRpDetail, _ int) bool {
			return !cmutil.IsEmpty(item.BkCloudZone)
		})
	case "sub_zone":
		return lo.Filter(items, func(item model.TbRpDetail, _ int) bool {
			return !cmutil.IsEmpty(item.SubZone) && !cmutil.IsEmpty(item.SubZoneID)
		})
	default:
		// 非法维度在入口校验阶段已拦截，不应走到此
		logger.Warn("filterEmptyDimensionCandidates: unsupported dimension %s, returning all items", dimension)
		return items
	}
}

// adaptCandidatesToV1Format 正向适配：按维度映射候选到 v1 格式
// bk_cloud_zone: 取 BkCloudZone 双写入 SubZone/SubZoneID
// sub_zone: 保持 SubZone/SubZoneID 原样（已是园区维度）
// dimension 空或非法由入口 NormalizeDefaults/校验保证，还空/非法则返回错误
func adaptCandidatesToV1Format(candidates []model.TbRpDetail, dimension string) ([]model.TbRpDetail, error) {
	// dimension 空则报错（入口 NormalizeDefaults 已做；还空属参数异常）
	if dimension == "" {
		return nil, fmt.Errorf("adaptCandidatesToV1Format: affinity_dimension is empty, cannot adapt")
	}

	// 创建副本，只在内存中修改
	adapted := make([]model.TbRpDetail, len(candidates))
	copy(adapted, candidates)

	switch dimension {
	case "bk_cloud_zone":
		// bk_cloud_zone 维度：正向取 BkCloudZone，反向双写 SubZone/SubZoneID
		for i := range adapted {
			primaryDimValue := adapted[i].BkCloudZone
			adapted[i].SubZone = primaryDimValue
			adapted[i].SubZoneID = primaryDimValue
		}
	case "sub_zone":
		// sub_zone 维度：SubZone/SubZoneID 已是园区维度，返回副本即可
		// 不需要 overwrite
	default:
		// 非法维度在入口校验阶段已拦截，不应走到此
		return nil, fmt.Errorf("adaptCandidatesToV1Format: unsupported dimension '%s'", dimension)
	}

	return adapted, nil
}

func isResourceInsufficientError(err error) bool {
	var insufficient *apply.ResourceInsufficientError
	return errors.As(err, &insufficient)
}

func filterDryRunCandidates(candidates []model.TbRpDetail, used map[int]struct{}) []model.TbRpDetail {
	if len(used) == 0 {
		return candidates
	}
	return lo.Filter(candidates, func(item model.TbRpDetail, _ int) bool {
		_, ok := used[item.BkHostID]
		return !ok
	})
}

func markDryRunUsedHosts(picker *apply.PickerObject, used map[int]struct{}) {
	for _, id := range picker.SatisfiedHostIds {
		used[id] = struct{}{}
	}
}

// funnelStep pickCheckV2 漏斗中的一个计数步骤：叠加一个匹配条件后统计剩余候选数
type funnelStep struct {
	name string
	desc string
	fn   func(*gorm.DB)
}

// pickCheckV2 做前置计数并产出漏斗，行为对齐 v1 PickCheck 的可观测输出。
// dimension 为请求级 affinity_dimension，决定漏斗中的位置过滤步骤：
//   - cloud 模式：cloud_location 步骤（MatchBkCloudLocationSpec，叠加云地域/云可用区）；
//   - legacy 模式：location 步骤（v1 MatchLocationSpec，叠加城市/园区/排除条件）。
//
// excludeHostIDs 为 dry-run 场景下已选中的主机集合（同一批 dry-run 内避免重复选机）。
// 计数不足时返回 ResourceInsufficientError，带完整漏斗现场。
func pickCheckV2(forbizId int, rsType string, detail ObjectDetailV2, dimension string, excludeHostIDs map[int]struct{}) error {
	searchCtx := toSearchContext(forbizId, rsType, detail, dimension)
	if len(excludeHostIDs) > 0 {
		filtered := make([]int, 0, len(searchCtx.SpecialHostIds))
		for _, id := range searchCtx.SpecialHostIds {
			if _, used := excludeHostIDs[id]; !used {
				filtered = append(filtered, id)
			}
		}
		searchCtx.SpecialHostIds = filtered
	}
	if len(searchCtx.SpecialHostIds) > 0 {
		return searchCtx.PickCheckSpecialBkhostIds()
	}

	db := model.DB.Self.Table(model.TbRpDetailName()).Select("count(*)")
	db.Where("bk_cloud_id = ? and status = ? and gse_agent_status_code = ?",
		searchCtx.BkCloudId, model.Unused, bk.GseAlive)
	if len(excludeHostIDs) > 0 {
		excluded := make([]int, 0, len(excludeHostIDs))
		for id := range excludeHostIDs {
			excluded = append(excluded, id)
		}
		db.Where("bk_host_id not in (?)", excluded)
	}

	var funnel []apply.MatchStageCount
	countAfter := func(name, desc string, fn func(*gorm.DB)) error {
		fn(db)
		var count int64
		if err := db.Scan(&count).Error; err != nil {
			logger.Error("v2 pick check step %s failed: %s", name, err.Error())
			return errno.ErrDBQuery.AddErr(err)
		}
		funnel = append(funnel, apply.MatchStageCount{
			Name:        name,
			Count:       count,
			Requested:   detail.Count,
			Description: desc,
		})
		return nil
	}

	steps := []funnelStep{
		{name: "biz", desc: "叠加专用业务/公共业务后", fn: searchCtx.MatchIntentionBkBiz},
		{name: "rsType", desc: "叠加资源类型后", fn: searchCtx.MatchRsType},
		{name: "osType", desc: "叠加操作系统类型后", fn: searchCtx.MatchOsType},
		{name: "osName", desc: "叠加操作系统名称后", fn: searchCtx.MatchOsName},
		{name: "labels", desc: "叠加标签后", fn: searchCtx.MatchLabels},
	}
	// 位置过滤步骤按模式选择：cloud 用云区匹配，legacy 用 v1 城市/园区匹配
	if locationMode(dimension) == AffinityDimensionSubZone {
		steps = append(steps, funnelStep{
			name: "location", desc: "叠加城市/园区条件后", fn: searchCtx.MatchLocationSpec,
		})
	} else {
		steps = append(steps, funnelStep{
			name: "cloud_location", desc: "叠加云地域/云可用区后", fn: searchCtx.MatchBkCloudLocationSpec,
		})
	}
	steps = append(steps,
		funnelStep{name: "storage", desc: "叠加磁盘条件(仅带挂载点)后", fn: searchCtx.MatchStorage},
		funnelStep{name: "spec", desc: "叠加规格[cpu/mem或机型]后", fn: searchCtx.MatchSpec},
	)
	for _, step := range steps {
		if err := countAfter(step.name, step.desc, step.fn); err != nil {
			return err
		}
	}

	if len(detail.LocationSpec.ForcedSubZoneIds) > 0 {
		if err := countAfter("same_location_subzone", "叠加同位置 sub_zone 后", func(tx *gorm.DB) {
			tx.Where("sub_zone_id in (?)", detail.LocationSpec.ForcedSubZoneIds)
		}); err != nil {
			return err
		}
	}

	switch detail.Affinity {
	case apply.SAME_SUBZONE_CROSS_SWTICH:
		if err := countAfter("netDevice", "叠加网卡id非空后", searchCtx.UseNetDeviceIsNotEmpty); err != nil {
			return err
		}
	case apply.CROSS_RACK, apply.CROSS_SUBZONE_STRONG, apply.CROSS_SUBZONE_WEAK:
		if err := countAfter("rackId", "叠加机架id非空后", searchCtx.RackIdIsNotEmpty); err != nil {
			return err
		}
	}

	finalCount := funnel[len(funnel)-1].Count
	if finalCount < int64(detail.Count) {
		msg := fmt.Sprintf("申请需求:\n%s 资源池符合条件的资源总数%d 小于申请的数量", detail.FailDetail(dimension), finalCount)
		if funnelText := apply.FormatFunnel(funnel); funnelText != "" {
			msg += "\n\n" + funnelText
		}
		return apply.NewResourceInsufficientError(apply.ApplyFailureEvidence{
			Stage:        apply.FailStagePickCheck,
			GroupMark:    detail.GroupMark,
			Affinity:     detail.Affinity,
			RequestCount: detail.Count,
			Funnel:       funnel,
			Note:         "漏斗的 storage 步骤只覆盖带挂载点的 SQL 磁盘条件",
		}, errno.ErrResourceinsufficient.Add(msg), msg)
	}
	return nil
}

// getAllAvailableSubZones 获取同位置候选园区（legacy 模式）。
// dimension 为请求级 affinity_dimension；复用 v2 的 SearchContext 与 v1 匹配条件：
// 位置过滤按模式选择——legacy 模式走 v1 MatchLocationSpec（city/IdcCitys + 园区/排除条件），
// 保证枚举出的园区已经满足该分组的全部位置约束。
func getAllAvailableSubZones(forbizId int, rsType string, detail ObjectDetailV2, dimension string) ([]string, error) {
	var subZones []string
	searchCtx := toSearchContext(forbizId, rsType, detail, dimension)

	db := model.DB.Self.Table(model.TbRpDetailName()).Select("DISTINCT sub_zone_id")
	if len(searchCtx.SpecialHostIds) > 0 {
		db.Where("bk_host_id in (?) and status = ?", searchCtx.SpecialHostIds, model.Unused)
	} else {
		db.Where("bk_cloud_id = ? and status = ? and sub_zone_id <> ?",
			searchCtx.BkCloudId, model.Unused, "")
		searchCtx.MatchIntentionBkBiz(db)
		searchCtx.MatchRsType(db)
		// 位置过滤按模式选择：legacy 用 v1 城市/园区匹配，cloud 用云区匹配
		if locationMode(dimension) == AffinityDimensionSubZone {
			searchCtx.MatchLocationSpec(db)
		} else {
			searchCtx.MatchBkCloudLocationSpec(db)
		}
	}

	if err := db.Scan(&subZones).Error; err != nil {
		return nil, errno.ErrDBQuery.AddErr(err)
	}
	return lo.Filter(subZones, func(zone string, _ int) bool { return !cmutil.IsEmpty(zone) }), nil
}

// newV2ResourceInsufficientError 构建选机阶段的资源不足错误（带失败现场证据）。
// dimension 为请求级 affinity_dimension，决定 FailDetail 按哪种模式输出位置条件文案。
func newV2ResourceInsufficientError(detail ObjectDetailV2, candidates []model.TbRpDetail,
	picker *apply.PickerObject, cause error, dimension string) error {
	msg := fmt.Sprintf("Picker for %s, 所有资源无法满足的参数需求\n%s", detail.GroupMark, detail.FailDetail(dimension))
	return apply.NewResourceInsufficientError(apply.ApplyFailureEvidence{
		Stage:          apply.FailStageAffinity,
		GroupMark:      detail.GroupMark,
		Affinity:       detail.Affinity,
		RequestCount:   detail.Count,
		CandidateCount: len(candidates),
		PickedCount:    len(picker.SatisfiedHostIds),
		ProcessLogs:    picker.ProcessLogs,
		Distribution:   apply.BuildAffinitySnapshot(detail.Affinity, detail.Count, candidates),
		Note:           "process_logs 为分配过程日志,不是结论",
	}, cause, msg)
}

// newV2SameLocationError 构建同位置申请找不到可用位置的错误（带各分组失败条件）。
// dimension 为请求级 affinity_dimension，决定 FailDetail 按哪种模式输出位置条件文案。
func newV2SameLocationError(details []ObjectDetailV2, message string, dimension string) error {
	var builder strings.Builder
	builder.WriteString(message)
	builder.WriteString("\n")
	for i := range details {
		builder.WriteString(details[i].FailDetail(dimension))
	}
	msg := builder.String()
	if len(details) == 0 {
		return fmt.Errorf("%s", msg)
	}
	first := details[0]
	return apply.NewResourceInsufficientError(apply.ApplyFailureEvidence{
		Stage:        apply.FailStagePickCheck,
		GroupMark:    first.GroupMark,
		Affinity:     first.Affinity,
		RequestCount: first.Count,
		Note:         "多分组同位置未找到可用 zone/subzone,未执行按条件逐步筛选",
	}, errno.ErrResourceinsufficient.Add(msg), msg)
}

// pickInstanceBaseV2 调用 v1 的 PickInstanceBase 进行选机。
// dimension 为请求级 affinity_dimension，经由 toSearchContext 决定
// current_hosts 主维映射（见 convertCurrentHostsV2ToV1）。
func pickInstanceBaseV2(picker *apply.PickerObject, items []model.TbRpDetail, detail ObjectDetailV2, dimension string) error {
	logger.Info("the anti-affinity is %s", detail.Affinity)

	// forbizId/rsType 不影响 PickInstanceBase；hosts 已通过 detail 进入 SpecialHostIds
	searchCtx := toSearchContext(0, "", detail, dimension)

	return searchCtx.PickInstanceBase(picker, items)
}

// convertCurrentHostsV2ToV1 正向转换：取 v2 亲和存量机的 BkCloudZone 填入 v1 CurrentResource.SubZone
func convertCurrentHostsV2ToV1(hostsV2 []CurrentHostV2, dimension string) []apply.CurrentResource {
	var hostsV1 []apply.CurrentResource
	for _, h := range hostsV2 {
		var subZoneValue string
		// 按维度选择主维字段填入 v1 SubZone 旧槽
		switch dimension {
		case "bk_cloud_zone":
			subZoneValue = h.BkCloudZone
		case "sub_zone":
			subZoneValue = h.SubZone
		default:
			// 空值或非法维度在入口已拦截，不应走到此
			subZoneValue = h.BkCloudZone // 兜底使用云可用区
		}

		hostsV1 = append(hostsV1, apply.CurrentResource{
			BkHostId: h.BkHostId,
			SubZone:  subZoneValue,
			RackId:   h.RackId,
		})
	}
	return hostsV1
}

// preselectedWithRollbackOnPartialFailure 预选并在部分失败时精确回滚（R03）
// 走 CAS：Unused → Preselected，并让 CAS 直接返回本请求真正成功的 bk_host_id；
// 这样回滚只针对本请求写成功的机器，不会把其他请求预选的机器误改回 Unused。
func preselectedWithRollbackOnPartialFailure(picker *apply.PickerObject, groupMark string) error {
	expectedCount := len(picker.SatisfiedHostIds)
	if expectedCount == 0 {
		return fmt.Errorf("no hosts satisfied for group %s", groupMark)
	}

	succeeded, err := model.PreselectFromUnusedWithIDs(picker.SatisfiedHostIds, model.Preselected)
	if err != nil {
		// CAS 事务报错：无法确认是否部分写成功，回滚本次可能已写成功的 id
		if len(succeeded) > 0 {
			if rollbackErr := model.UpdateTbRpDetailStatusAtSelling(succeeded, model.Unused); rollbackErr != nil {
				logger.Error("rollback preselected hosts failed for %s: %s", groupMark, rollbackErr.Error())
			}
		}
		return fmt.Errorf("preselect failed for %s: %w (rolled back %d hosts)", groupMark, err, len(succeeded))
	}

	if len(succeeded) != expectedCount {
		// 部分成功：只回滚本请求真正写成功的那批，禁止按期望 id 盲回滚（会误伤他请求）
		if len(succeeded) > 0 {
			if rollbackErr := model.UpdateTbRpDetailStatusAtSelling(succeeded, model.Unused); rollbackErr != nil {
				logger.Error("rollback preselected hosts failed for %s: %s", groupMark, rollbackErr.Error())
			}
		}
		return fmt.Errorf("preselect failed for %s: expected %d hosts but only %d were preselected (rolled back)",
			groupMark, expectedCount, len(succeeded))
	}

	return nil
}

// getAllAvailableZones 获取所有可用云可用区（用于多组同位置，cloud 模式），复用 v1 匹配逻辑。
// dimension 为请求级 affinity_dimension（此函数只在 cloud 模式下被调用）。
func getAllAvailableZones(forbizId int, rsType string, detail ObjectDetailV2, dimension string) ([]string, error) {
	var zones []string

	// 构造 SearchContext，复用 v1 匹配逻辑
	searchCtx := toSearchContext(forbizId, rsType, detail, dimension)

	db := model.DB.Self.Table(model.TbRpDetailName()).Select("DISTINCT bk_cloud_zone")

	// 基础过滤
	db = db.Where("bk_cloud_id = ? AND status = ?", searchCtx.BkCloudId, model.Unused)

	// 复用 v1 匹配步骤
	searchCtx.MatchIntentionBkBiz(db)
	searchCtx.MatchRsType(db)

	// 云区匹配（只需要 region 过滤）
	searchCtx.MatchBkCloudLocationSpec(db)

	if err := db.Scan(&zones).Error; err != nil {
		return nil, err
	}

	// 过滤空值
	zones = lo.Filter(zones, func(z string, _ int) bool {
		return !cmutil.IsEmpty(z)
	})

	return zones, nil
}
