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

	// v2 参数校验（包含拒绝旧字段）
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
		c.SendResponse(r, errno.ErrResourceinsufficient.Add(param.BuildMessage()+"\n"+err.Error()), "")
		return
	}

	if param.DryRun {
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

	// v2 响应需要确保包含云字段
	responseData := buildV2Response(data)
	c.SendResponse(r, nil, responseData)
}

// buildV2Response 构建 v2 响应，按云可用区聚合 Summary（R06）
// 禁止把一组拆成多个 Item，保持一组一条 Item（原 group_mark），只重写 Summary
func buildV2Response(data []model.BatchGetTbDetailResult) []model.BatchGetTbDetailResult {
	// R06：保留一组一条 Item，重写 Summary（by_cloud_zone 等），明细映射主维

	for i := range data {
		// 按云可用区统计 Summary
		byCloudZone := make(map[string]int)
		byCloudZoneRack := make(map[string]map[string]int)

		for j := range data[i].Data {
			// 映射明细旧字段为主维值（避免下游误读）
			data[i].Data[j].SubZone = data[i].Data[j].BkCloudZone
			data[i].Data[j].SubZoneID = data[i].Data[j].BkCloudZone

			// 统计云可用区分布
			zone := data[i].Data[j].BkCloudZone
			rack := data[i].Data[j].RackID
			byCloudZone[zone]++

			if byCloudZoneRack[zone] == nil {
				byCloudZoneRack[zone] = make(map[string]int)
			}
			byCloudZoneRack[zone][rack]++
		}

		// 整表替换 Summary（清掉园区键，只保留云可用区维度）
		data[i].Summary = map[string]interface{}{
			"by_cloud_zone":      byCloudZone,
			"by_cloud_zone_rack": byCloudZoneRack,
		}

		logger.Info("Group %s Summary: by_cloud_zone=%v, by_cloud_zone_rack=%v",
			data[i].Item, byCloudZone, byCloudZoneRack)
	}

	return data
}

// CycleApplyV2 v2 循环申请逻辑
func CycleApplyV2(param RequestInputParamV2) (pickers []*apply.PickerObject, err error) {
	// 检查是否需要多组同位置
	affinitys := lo.Uniq(param.GetAllAffinitys())
	if param.GroupsInSameLocation && len(param.Details) > 1 && len(affinitys) == 1 {
		// v2 多组同位置逻辑
		return applyGroupsInSameLocationV2(param)
	}

	// 单组或多组不同位置逻辑
	for _, detail := range param.Details {
		var picker *apply.PickerObject

		// 如果数量 <= 1，重置亲和性为 NONE
		if detail.Affinity == "" || detail.Count <= 1 {
			detail.Affinity = apply.NONE
		}

		// v2 查询候选
		candidates, err := queryCandidatesV2(param.ForbizId, param.ResourceType, detail)
		if err != nil {
			return pickers, err
		}

		// 过滤空挂载点磁盘（复用 v1 导出方法）
		diskSpecs := meta.GetEmptyDiskSpec(detail.StorageSpecs)
		if len(diskSpecs) > 0 {
			candidates, err = apply.FilterEmptyMountPointStorage(candidates, detail.StorageSpecs, diskSpecs)
			if err != nil {
				logger.Error("filter empty mount point storage failed %s", err.Error())
				return pickers, err
			}
		}

		// 内存适配：将维度值映射到 v1 格式
		adaptedCandidates, err := adaptCandidatesToV1Format(candidates, detail.AffinityDimension)
		if err != nil {
			return pickers, fmt.Errorf("detail %s: %w", detail.GroupMark, err)
		}

		// 创建 picker 并调用现有选机逻辑
		picker = apply.NewPicker(detail.Count, detail.GroupMark)

		// 构建 v1 兼容的 SearchContext 并调用 PickInstanceBase
		err = pickInstanceBaseV2(picker, adaptedCandidates, detail)
		if err != nil {
			return pickers, err
		}

		// 检查是否选满
		if !picker.PickerDone() {
			return nil, fmt.Errorf("Picker for %s, 所有资源无法满足的参数需求\n%s", detail.GroupMark, detail.FailDetail())
		}

		// 预选（Unused → Preselected）- 按 R03 处理部分失败
		if err := preselectedWithRollbackOnPartialFailure(picker, detail.GroupMark); err != nil {
			return pickers, err
		}

		pickers = append(pickers, picker)
	}

	return pickers, nil
}

// applyGroupsInSameLocationV2 v2 多组同位置申请（按云可用区）
func applyGroupsInSameLocationV2(param RequestInputParamV2) (pickers []*apply.PickerObject, err error) {
	// 获取第一个请求的位置规格
	firstDetail := param.Details[0]

	// 确定候选云可用区（根据 bk_cloud_zone_list）
	candidateZones := firstDetail.LocationSpec.BkCloudZoneList
	if len(candidateZones) == 0 {
		// 如果没有指定，查询所有可用区
		candidateZones, err = getAllAvailableZones(param.ForbizId, param.ResourceType, firstDetail)
		if err != nil {
			return pickers, err
		}
	}

	// 按优先级尝试每个 zone
	for _, zone := range candidateZones {
		pickers = []*apply.PickerObject{}
		success := true

		for _, detail := range param.Details {
			// 强制使用当前 zone
			detailCopy := detail
			detailCopy.LocationSpec.BkCloudZoneList = []string{zone}

			// 查询候选
			candidates, err := queryCandidatesV2(param.ForbizId, param.ResourceType, detailCopy)
			if err != nil {
				success = false
				break
			}

			// 过滤空挂载点磁盘（复用 v1 导出方法）
			diskSpecs := meta.GetEmptyDiskSpec(detailCopy.StorageSpecs)
			if len(diskSpecs) > 0 {
				candidates, err = apply.FilterEmptyMountPointStorage(candidates, detailCopy.StorageSpecs, diskSpecs)
				if err != nil {
					success = false
					break
				}
			}

			// 内存适配
			adaptedCandidates, err := adaptCandidatesToV1Format(candidates, detailCopy.AffinityDimension)
			if err != nil {
				success = false
				break
			}

			// 创建 picker
			picker := apply.NewPicker(detailCopy.Count, detailCopy.GroupMark)

			// 选机
			err = pickInstanceBaseV2(picker, adaptedCandidates, detailCopy)
			if err != nil || !picker.PickerDone() {
				success = false
				break
			}

			// 预选 - 按 R03 处理部分失败
			if err := preselectedWithRollbackOnPartialFailure(picker, detailCopy.GroupMark); err != nil {
				success = false
				break
			}

			pickers = append(pickers, picker)
		}

		if success {
			return pickers, nil
		}

		// 失败，回滚本批次
		apply.RollBackAllInstanceUnused(pickers)
	}

	var details strings.Builder
	for i := range param.Details {
		details.WriteString(param.Details[i].FailDetail())
	}
	return nil, fmt.Errorf("no zone can satisfy all groups in same location\n%s", details.String())
}

// toSearchContext v2 detail 转为 v1 SearchContext（去重字段赋值）
func toSearchContext(forbizId int, rsType string, detail ObjectDetailV2) *apply.SearchContext {
	return &apply.SearchContext{
		ObjectDetail: &apply.ObjectDetail{
			BkCloudId:     detail.BkCloudId,
			GroupMark:     detail.GroupMark,
			Affinity:      detail.Affinity,
			Count:         detail.Count,
			Tolerance:     detail.Tolerance,
			CurrentHosts:  convertCurrentHostsV2ToV1(detail.CurrentHosts, detail.AffinityDimension),
			Spec:          detail.Spec,
			StorageSpecs:  detail.StorageSpecs,
			DeviceClass:   detail.DeviceClass,
			Labels:        detail.Labels,
			OsType:        detail.OsType,
			OsNames:       detail.OsNames,
			ExcludeOsName: detail.ExcludeOsName,
		},
		RsType:                 model.NormalizeResourceType(rsType),
		IntentionBkBizId:       forbizId,
		AffinityDimension:      detail.AffinityDimension,
		BkCloudRegion:          detail.LocationSpec.BkCloudRegion,
		BkCloudZoneList:        detail.LocationSpec.BkCloudZoneList,
		ExcludeBkCloudZoneList: detail.LocationSpec.ExcludeBkCloudZoneList,
	}
}

// queryCandidatesV2 v2 查询候选机器（复用 v1 匹配步骤 + 云区 MatchBkCloudLocationSpec）
func queryCandidatesV2(forbizId int, rsType string, detail ObjectDetailV2) ([]model.TbRpDetail, error) {
	var items []model.TbRpDetail

	// 构造 SearchContext，复用 v1 匹配逻辑
	searchCtx := toSearchContext(forbizId, rsType, detail)

	db := model.DB.Self.Table(model.TbRpDetailName())

	// 基础过滤（状态 + GSE）
	db.Where("bk_cloud_id = ? and status = ? and gse_agent_status_code = ?",
		searchCtx.BkCloudId, model.Unused, bk.GseAlive)

	// 复用 v1 匹配步骤
	searchCtx.MatchIntentionBkBiz(db)
	searchCtx.MatchRsType(db)
	searchCtx.MatchOsType(db)
	searchCtx.MatchOsName(db)
	searchCtx.MatchLabels(db)

	// v2 显式调用云区匹配（新增能力）
	searchCtx.MatchBkCloudLocationSpec(db)

	// 磁盘匹配（带挂载点的）
	searchCtx.MatchStorage(db)

	// 规格匹配
	searchCtx.MatchSpec(db)

	// 亲和性相关字段检查
	switch detail.Affinity {
	case apply.SAME_SUBZONE_CROSS_SWTICH:
		searchCtx.UseNetDeviceIsNotEmpty(db)
	case apply.CROSS_RACK, apply.CROSS_SUBZONE_STRONG, apply.CROSS_SUBZONE_WEAK:
		searchCtx.RackIdIsNotEmpty(db)
	}

	// 执行查询
	if err := db.Scan(&items).Error; err != nil {
		logger.Error("query candidates v2 failed: %s", err.Error())
		return nil, errno.ErrDBQuery.AddErr(err)
	}

	// 踢掉主维为空的候选（按 affinity_dimension）
	items = filterEmptyDimensionCandidates(items, detail.AffinityDimension)

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

// pickInstanceBaseV2 调用 v1 的 PickInstanceBase 进行选机（真正复用，不自写）
func pickInstanceBaseV2(picker *apply.PickerObject, items []model.TbRpDetail, detail ObjectDetailV2) error {
	logger.Info("the anti-affinity is %s", detail.Affinity)

	// 构造 v1 兼容的 SearchContext（复用 toSearchContext 避免字段赋值重复）
	// forbizId 和 rsType 在此不影响 PickInstanceBase，传 0 和空串即可
	searchCtx := toSearchContext(0, "", detail)

	// 真正调用上游 PickInstanceBase（会自动调用 InitToleranceConfig 等）
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

// preselectedWithRollbackOnPartialFailure 预选并在部分失败时正确回滚（R03）
// 必须走 CAS：Unused → Preselected（禁止裸 UpdateTbRpDetail）
func preselectedWithRollbackOnPartialFailure(picker *apply.PickerObject, groupMark string) error {
	expectedCount := len(picker.SatisfiedHostIds)
	if expectedCount == 0 {
		return fmt.Errorf("no hosts satisfied for group %s", groupMark)
	}

	// 必须走 CAS：调用 apply.PreselectedSatisfiedInstance（Unused → Preselected）
	err := picker.PreselectedSatisfiedInstance()

	// 成功
	if err == nil {
		return nil
	}

	// 失败：查询本批实际被设置为 Preselected 的 host（R03）
	// 禁止假设「报错即无脏数据」或「用期望 ID 盲回滚」
	var actualPreselected []int
	queryErr := model.DB.Self.Table(model.TbRpDetailName()).
		Select("bk_host_id").
		Where("bk_host_id in (?) AND status = ?", picker.SatisfiedHostIds, model.Preselected).
		Scan(&actualPreselected).Error

	if queryErr != nil {
		// 查询失败：打错误日志且接口失败（禁止空过）
		logger.Error("preselect failed for %s, query actual preselected also failed: %s (original error: %s)",
			groupMark, queryErr.Error(), err.Error())
		// 兜底：StatusAtSelling 只动 Preselected，用期望 ID 尝试回滚
		actualPreselected = picker.SatisfiedHostIds
		// 接口必须失败
		if len(actualPreselected) > 0 {
			rollbackErr := model.UpdateTbRpDetailStatusAtSelling(actualPreselected, model.Unused)
			if rollbackErr != nil {
				logger.Error("rollback preselected hosts failed: %s", rollbackErr.Error())
			}
		}
		return fmt.Errorf("preselect failed for %s: %v (query actual preselected failed: %v, attempted rollback)",
			groupMark, err, queryErr)
	}

	// 查询成功：只回滚本批实际 Preselected 的 host（R03：禁止误伤他请求）
	if len(actualPreselected) > 0 {
		rollbackErr := model.UpdateTbRpDetailStatusAtSelling(actualPreselected, model.Unused)
		if rollbackErr != nil {
			logger.Error("rollback preselected hosts failed: %s", rollbackErr.Error())
		}
	}

	// 返回原始错误
	return fmt.Errorf("preselect failed for %s: %v (rolled back %d actual preselected hosts)",
		groupMark, err, len(actualPreselected))
}

// getAllAvailableZones 获取所有可用区（用于多组同位置，复用 v1 匹配逻辑）
func getAllAvailableZones(forbizId int, rsType string, detail ObjectDetailV2) ([]string, error) {
	var zones []string

	// 构造 SearchContext，复用 v1 匹配逻辑
	searchCtx := toSearchContext(forbizId, rsType, detail)

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
