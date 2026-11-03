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
	middleware.RequestLoggerFilter.Add("/v2/resource/apply")
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
	return lock.NewSpinLock(&lock.RedisLock{Name: key, RandKey: requestId, Expiry: 120 * time.Second}, 60,
		350*time.Millisecond)
}

// ApplyResourceV2 v2 申请资源入口
func (c *ApplyHandlerV2) ApplyResourceV2(r *gin.Context) {
	logger.Info("start apply resource v2 ...")
	
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
		lock := newLocker(param.LockKey(), c.RequestId)
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
	
	logger.Info(fmt.Sprintf("The %s, will return %d machines", c.RequestId, len(data)))
	
	task.ApplyResponseLogChan <- task.ApplyResponseLogItem{
		RequestId: c.RequestId,
		Data:      data,
	}
	task.RecordRsOperatorInfoChan <- param.GetOperationInfo(c.RequestId, model.Used, data)
	
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
		
		// 过滤空挂载点磁盘（v2 无指定机入口，CurrentHosts 是亲和存量机，不应作门闸）
		diskSpecs := meta.GetEmptyDiskSpec(detail.StorageSpecs)
		if len(diskSpecs) > 0 {
			candidates, err = filterEmptyMountPointStorageV2(candidates, diskSpecs, detail.StorageSpecs)
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
			return nil, fmt.Errorf("picker for %s: failed to satisfy requirements", detail.GroupMark)
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
			
			// 过滤空挂载点磁盘（v2 无指定机入口，CurrentHosts 是亲和存量机，不应作门闸）
			diskSpecs := meta.GetEmptyDiskSpec(detailCopy.StorageSpecs)
			if len(diskSpecs) > 0 {
				candidates, err = filterEmptyMountPointStorageV2(candidates, diskSpecs, detailCopy.StorageSpecs)
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
	
	return nil, fmt.Errorf("no zone can satisfy all groups in same location")
}

// queryCandidatesV2 v2 查询候选机器（直接按云字段等值匹配）
func queryCandidatesV2(forbizId int, rsType string, detail ObjectDetailV2) ([]model.TbRpDetail, error) {
	var items []model.TbRpDetail
	
	db := model.DB.Self.Table(model.TbRpDetailName())
	
	// 基础状态过滤
	db = db.Where("bk_cloud_id = ? AND status = ? AND gse_agent_status_code = ?",
		detail.BkCloudId, model.Unused, bk.GseAlive)
	
	// 业务匹配
	if forbizId <= 0 {
		db = db.Where("dedicated_biz = 0")
	} else {
		db = db.Where("dedicated_biz in (?)", []int{0, forbizId})
	}
	
	// 资源类型匹配
	if cmutil.IsEmpty(rsType) {
		db = db.Where("rs_type = 'PUBLIC'")
	} else {
		db = db.Where("rs_type in (?)", []string{"PUBLIC", rsType})
	}
	
	// OS 类型
	osType := detail.OsType
	if cmutil.IsEmpty(osType) {
		osType = model.LinuxOs
	}
	db = db.Where("os_type = ?", osType)
	
	// OS 名称
	if len(detail.OsNames) > 0 {
		if detail.ExcludeOsName {
			db = db.Where("os_name not in (?)", detail.OsNames)
		} else {
			db = db.Where("os_name in (?)", detail.OsNames)
		}
	}
	
	// 标签匹配
	if len(detail.Labels) > 0 {
		db = db.Where(model.JSONQuery("labels").JointOrContains(detail.Labels))
	} else {
		db = db.Where("JSON_TYPE(labels) is null OR JSON_LENGTH(labels) < 1")
	}
	
	// v2 核心：云字段直接等值匹配（不走逻辑城转换）
	if !detail.LocationSpec.IsEmpty() {
		// bk_cloud_region 等值匹配
		db = db.Where("bk_cloud_region = ?", detail.LocationSpec.BkCloudRegion)
	
	// bk_cloud_zone_list 匹配
	if len(detail.LocationSpec.BkCloudZoneList) > 0 {
		db = db.Where("bk_cloud_zone in (?)", detail.LocationSpec.BkCloudZoneList)
	}
	
	// exclude zones
	if len(detail.LocationSpec.ExcludeBkCloudZoneList) > 0 {
		db = db.Where("bk_cloud_zone not in (?)", detail.LocationSpec.ExcludeBkCloudZoneList)
	}
	}
	
	// 规格匹配
	if detail.Spec.NotEmpty() {
		if detail.Spec.Cpu.IsNotEmpty() {
			db = db.Where("cpu_num >= ? AND cpu_num <= ?", detail.Spec.Cpu.Min, detail.Spec.Cpu.Max)
		}
		if detail.Spec.Mem.IsNotEmpty() {
			db = db.Where("dram_cap >= ? AND dram_cap <= ?", detail.Spec.Mem.Min, detail.Spec.Mem.Max)
		}
	}
	
	// 机型
	if len(detail.DeviceClass) > 0 {
		db = db.Where("device_class in (?)", detail.DeviceClass)
	}
	
	// 磁盘规格（简化：只过滤有存储需求的基本匹配）
	// 详细磁盘匹配在后续 filterEmptyMountPointStorageV2 中处理
	
	// 亲和性相关字段检查
	switch detail.Affinity {
	case apply.SAME_SUBZONE_CROSS_SWTICH:
		db = db.Where("net_device_id != ''")
	case apply.CROSS_RACK:
		db = db.Where("rack_id != ''")
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
	
	// 构造 v1 兼容的 SearchContext，补齐所有 PickInstanceBase 实际用到的字段
	searchCtx := &apply.SearchContext{
		ObjectDetail: &apply.ObjectDetail{
			BkCloudId:    detail.BkCloudId,
			GroupMark:    detail.GroupMark,
		Affinity:     detail.Affinity,
		Count:        detail.Count,
		Tolerance:    detail.Tolerance,
		CurrentHosts: convertCurrentHostsV2ToV1(detail.CurrentHosts, detail.AffinityDimension),
			// 补齐规格相关字段（用于优先级评分）
			Spec:         detail.Spec,
			StorageSpecs: detail.StorageSpecs,
			DeviceClass:  detail.DeviceClass,
			Labels:       detail.Labels,
			OsType:       detail.OsType,
			OsNames:      detail.OsNames,
			ExcludeOsName: detail.ExcludeOsName,
		},
	}
	
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

// filterEmptyMountPointStorageV2 v2 过滤空挂载点磁盘（对齐 v1 语义）
// 逻辑：对每台机，找出「未在规格里占用的挂载点」（noUseStorages），
// 看这些未占用盘能否匹配空挂载点规格（diskSpecs 是 GetEmptyDiskSpec 返回的）
func filterEmptyMountPointStorageV2(items []model.TbRpDetail, diskSpecs []meta.DiskSpec, storageSpecs []meta.DiskSpec) ([]model.TbRpDetail, error) {
	var result []model.TbRpDetail
	
	for _, item := range items {
		if err := item.UnmarshalDiskInfo(); err != nil {
			logger.Error("unmarshal disk info failed for host %d: %s", item.BkHostID, err.Error())
			return nil, err
		}
		
		logger.Info("host %d storages: %v", item.BkHostID, item.Storages)
		
		// 找出未在规格里占用的挂载点（对齐 v1）
		noUseStorages := make(map[string]bk.DiskDetail)
		occupiedMountPoints := meta.GetDiskSpecMountPoints(storageSpecs)
		for mp, disk := range item.Storages {
			if !lo.Contains(occupiedMountPoints, mp) {
				noUseStorages[mp] = disk
			}
		}
		
		logger.Info("host %d no-use storages: %v", item.BkHostID, noUseStorages)
		
		// 检查空挂载点规格能否在未占用盘里全部匹配上
		if matchNoMountPointStorageV2(diskSpecs, noUseStorages) {
			result = append(result, item)
		}
	}
	
	if len(result) == 0 {
		return nil, errno.ErrResourceinsufficient.Add("匹配空挂载点磁盘的资源为 0")
	}
	
	return result, nil
}

// matchNoMountPointStorageV2 对齐 v1 matchNoMountPointStorage 逻辑
func matchNoMountPointStorageV2(specs []meta.DiskSpec, noUseStorages map[string]bk.DiskDetail) bool {
	matchedCount := 0
	// 复制一份避免修改原 map
	availableStorages := make(map[string]bk.DiskDetail)
	for k, v := range noUseStorages {
		availableStorages[k] = v
	}
	
	for _, spec := range specs {
		matched := false
		for mp, disk := range availableStorages {
			if diskDetailMatchV2(disk, spec) {
				delete(availableStorages, mp) // 匹配上就删除，避免重复使用
				matchedCount++
				matched = true
				break
			}
		}
		if !matched {
			return false // 有规格没匹配上
		}
	}
	
	return matchedCount == len(specs)
}

// diskDetailMatchV2 对齐 v1 diskDetailMatch 逻辑
func diskDetailMatchV2(disk bk.DiskDetail, spec meta.DiskSpec) bool {
	// 检查磁盘类型（对齐 v1）
	if strings.TrimSpace(disk.DiskType) != strings.TrimSpace(spec.DiskType) && !cmutil.IsEmpty(spec.DiskType) {
		logger.Info("disk type not match: disk=%s, spec=%s", disk.DiskType, spec.DiskType)
		return false
	}
	
	// 检查最大容量
	if disk.Size > spec.MaxSize && spec.MaxSize > 0 {
		logger.Info("max size not match: disk=%d, spec.MaxSize=%d", disk.Size, spec.MaxSize)
		return false
	}
	
	// 检查最小容量
	if disk.Size < spec.MinSize {
		logger.Info("min size not match: disk=%d, spec.MinSize=%d", disk.Size, spec.MinSize)
		return false
	}
	
	return true
}

// getAllAvailableZones 获取所有可用区（用于多组同位置）
func getAllAvailableZones(forbizId int, rsType string, detail ObjectDetailV2) ([]string, error) {
	var zones []string
	
	db := model.DB.Self.Table(model.TbRpDetailName()).Select("DISTINCT bk_cloud_zone")
	db = db.Where("bk_cloud_id = ? AND status = ?", detail.BkCloudId, model.Unused)
	
	// 如果有 bk_cloud_region，加上过滤
	if !detail.LocationSpec.IsEmpty() {
		db = db.Where("bk_cloud_region = ?", detail.LocationSpec.BkCloudRegion)
	}
	
	if forbizId <= 0 {
		db = db.Where("dedicated_biz = 0")
	} else {
		db = db.Where("dedicated_biz in (?)", []int{0, forbizId})
	}
	
	if !cmutil.IsEmpty(rsType) {
		db = db.Where("rs_type in (?)", []string{"PUBLIC", rsType})
	}
	
	if err := db.Scan(&zones).Error; err != nil {
		return nil, err
	}
	
	// 过滤空值
	zones = lo.Filter(zones, func(z string, _ int) bool {
		return !cmutil.IsEmpty(z)
	})
	
	return zones, nil
}
