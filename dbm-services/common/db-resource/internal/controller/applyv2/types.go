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
	"fmt"
	"strings"
	"time"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/apply"
	"dbm-services/common/db-resource/internal/svr/meta"
	"dbm-services/common/go-pubpkg/cmutil"
	"dbm-services/common/go-pubpkg/logger"

	"github.com/samber/lo"
)

// RequestInputParamV2 v2 申请请求参数
type RequestInputParamV2 struct {
	ResourceType         string           `json:"resource_type"`
	DryRun               bool             `json:"dry_run"`
	ForbizId             int              `json:"for_biz_id"`
	Details              []ObjectDetailV2 `json:"details" binding:"required,gt=0,dive"`
	GroupsInSameLocation bool             `json:"groups_in_same_location"`
	apply.ActionInfo
}

// ObjectDetailV2 v2 资源申请对象详情
type ObjectDetailV2 struct {
	BkCloudId         int             `json:"bk_cloud_id"`
	CurrentHosts      []CurrentHostV2 `json:"current_hosts"`
	GroupMark         string          `json:"group_mark" binding:"required"`
	Labels            []string        `json:"labels"`
	DeviceClass       []string        `json:"device_class"`
	Spec              meta.Spec       `json:"spec"`
	StorageSpecs      []meta.DiskSpec `json:"storage_spec"`
	LocationSpec      LocationSpecV2  `json:"location_spec"`
	Affinity          string          `json:"affinity"`
	AffinityDimension string          `json:"affinity_dimension"`
	Tolerance         float64         `json:"tolerance"`
	OsType            string          `json:"os_type"`
	OsNames           []string        `json:"os_names"`
	ExcludeOsName     bool            `json:"exclude_os_name"`
	Count             int             `json:"count" binding:"required,min=1"`
}

// LocationSpecV2 v2 位置规格
type LocationSpecV2 struct {
	BkCloudRegion          string   `json:"bk_cloud_region" binding:"required"`
	BkCloudZoneList        []string `json:"bk_cloud_zone_list"`
	ExcludeBkCloudZoneList []string `json:"exclude_bk_cloud_zone_list,omitempty"`
}

// CurrentHostV2 v2 当前主机信息（用于亲和容忍度计算）
type CurrentHostV2 struct {
	BkHostId    int    `json:"bk_host_id" binding:"required"`
	BkCloudZone string `json:"bk_cloud_zone"`
	SubZone     string `json:"sub_zone"` // 园区维度时的主维字段
	RackId      string `json:"rack_id"`
}

// IsEmpty 判断位置规格是否为空
func (l LocationSpecV2) IsEmpty() bool {
	return cmutil.IsEmpty(l.BkCloudRegion)
}

// FailDetail 申请失败时附带的请求条件，对应旧接口 GetMessage 里的机型、标签和地域
func (d ObjectDetailV2) FailDetail() string {
	var b strings.Builder
	b.WriteString(fmt.Sprintf(" group: %s\n", d.GroupMark))
	if len(d.DeviceClass) > 0 {
		b.WriteString(fmt.Sprintf("CC机型: %v\n", d.DeviceClass))
	}
	if len(d.Labels) > 0 {
		b.WriteString(fmt.Sprintf("标签: %v\n", d.Labels))
	} else {
		b.WriteString("标签: 无\n")
	}
	if d.Spec.Cpu.IsNotEmpty() {
		b.WriteString(fmt.Sprintf("cpu: %d ~ %d 核\n", d.Spec.Cpu.Min, d.Spec.Cpu.Max))
	}
	if d.Spec.Mem.IsNotEmpty() {
		b.WriteString(fmt.Sprintf("mem: %d ~ %d M\n", d.Spec.Mem.Min, d.Spec.Mem.Max))
	}
	b.WriteString(d.diskMatchInfo())
	b.WriteString(fmt.Sprintf("云地域: %s\n", d.LocationSpec.BkCloudRegion))
	if len(d.LocationSpec.BkCloudZoneList) > 0 {
		b.WriteString(fmt.Sprintf("云可用区: %v\n", d.LocationSpec.BkCloudZoneList))
	}
	if len(d.LocationSpec.ExcludeBkCloudZoneList) > 0 {
		b.WriteString(fmt.Sprintf("排除云可用区: %v\n", d.LocationSpec.ExcludeBkCloudZoneList))
	}
	b.WriteString(fmt.Sprintf("申请总数: %d \n", d.Count))
	return b.String()
}

func (d ObjectDetailV2) diskMatchInfo() string {
	if len(d.StorageSpecs) == 0 {
		return ""
	}
	var b strings.Builder
	for _, disk := range d.StorageSpecs {
		if cmutil.IsNotEmpty(disk.MountPoint) {
			b.WriteString(fmt.Sprintf("disk: mount point: %s", disk.MountPoint))
		}
		if cmutil.IsNotEmpty(disk.DiskType) {
			b.WriteString(" disk type: " + disk.DiskType)
		}
		switch {
		case disk.MaxSize > 0 && disk.MinSize > 0:
			b.WriteString(fmt.Sprintf(" size: %d ~  %d G ", disk.MinSize, disk.MaxSize))
		case disk.MaxSize > 0:
			b.WriteString(fmt.Sprintf(" size <= %d G ", disk.MaxSize))
		case disk.MinSize > 0:
			b.WriteString(fmt.Sprintf(" size >= %d G ", disk.MinSize))
		}
	}
	b.WriteString("\n")
	return b.String()
}

// ValidateAffinityDimension 校验亲和维度，允许空、bk_cloud_zone 或 sub_zone
func (p *RequestInputParamV2) ValidateAffinityDimension() error {
	for idx, detail := range p.Details {
		// 空值、bk_cloud_zone、sub_zone 都是合法的
		if detail.AffinityDimension == "" || detail.AffinityDimension == "bk_cloud_zone" || detail.AffinityDimension == "sub_zone" {
			continue
		}
		return fmt.Errorf("detail[%d]: unsupported affinity_dimension '%s', only empty, 'bk_cloud_zone' or 'sub_zone' allowed",
			idx, detail.AffinityDimension)
	}
	return nil
}

// ValidateCurrentHostsDimension 校验 current_hosts 的主维字段不能为空
func (p *RequestInputParamV2) ValidateCurrentHostsDimension() error {
	for idx, detail := range p.Details {
		// 获取主维字段名（默认 bk_cloud_zone）
		dimension := detail.AffinityDimension
		if dimension == "" {
			dimension = "bk_cloud_zone"
		}

		// 检查对应维度的字段
		switch dimension {
		case "bk_cloud_zone":
			for hostIdx, host := range detail.CurrentHosts {
				if cmutil.IsEmpty(host.BkCloudZone) {
					return fmt.Errorf("detail[%d].current_hosts[%d]: primary dimension field 'bk_cloud_zone' is empty when affinity_dimension='bk_cloud_zone', whole request rejected",
						idx, hostIdx)
				}
			}
		case "sub_zone":
			for hostIdx, host := range detail.CurrentHosts {
				if cmutil.IsEmpty(host.SubZone) {
					return fmt.Errorf("detail[%d].current_hosts[%d]: primary dimension field 'sub_zone' is empty when affinity_dimension='sub_zone', whole request rejected",
						idx, hostIdx)
				}
			}
		}
	}
	return nil
}

// NormalizeDefaults 标准化默认值（affinity_dimension 默认为 bk_cloud_zone）
func (p *RequestInputParamV2) NormalizeDefaults() {
	for i := range p.Details {
		if p.Details[i].AffinityDimension == "" {
			p.Details[i].AffinityDimension = "bk_cloud_zone"
		}
	}
}

// ParamCheck v2 参数检查
func (p *RequestInputParamV2) ParamCheck() error {
	// 1. 标准化默认值
	p.NormalizeDefaults()

	// 2. 校验亲和维度白名单
	if err := p.ValidateAffinityDimension(); err != nil {
		return err
	}

	// 3. 校验 current_hosts 主维不能为空
	if err := p.ValidateCurrentHostsDimension(); err != nil {
		return err
	}

	// 5. 基本参数检查（复用 v1 逻辑）
	for _, detail := range p.Details {
		for _, d := range detail.StorageSpecs {
			if d.MaxSize > 0 && d.MinSize > d.MaxSize {
				return fmt.Errorf("min %d greater than max %d", d.MinSize, d.MaxSize)
			}
		}
		if !detail.Spec.Cpu.Legal() {
			return fmt.Errorf("cpu参数不合法: min:%d,max:%d", detail.Spec.Cpu.Min, detail.Spec.Cpu.Max)
		}
		if !detail.Spec.Mem.Legal() {
			return fmt.Errorf("mem参数不合法: min:%d,max:%d", detail.Spec.Mem.Min, detail.Spec.Mem.Max)
		}

		// 亲和性检查
		if detail.Count <= 1 {
			continue
		}
		switch detail.Affinity {
		case apply.SAME_SUBZONE, apply.SAME_SUBZONE_CROSS_SWTICH:
			if detail.LocationSpec.IsEmpty() {
				return fmt.Errorf("you need choose a cloud region")
			}
		case apply.CROS_SUBZONE:
			if detail.LocationSpec.IsEmpty() {
				return fmt.Errorf("you need choose a cloud region")
			}
			if len(detail.LocationSpec.BkCloudZoneList) < 2 {
				return fmt.Errorf("cross cloud zone affinity requires at least 2 zones")
			}
		case apply.NONE:
			// NONE 无需额外检查
		}
	}

	return nil
}

// LockKey 获取锁键（与 v1 完全一致，确保共池互斥）
func (p *RequestInputParamV2) LockKey() string {
	if cmutil.IsEmpty(p.ResourceType) {
		return fmt.Sprintf("dbrms:lock:global")
	}
	// 使用 model.NormalizeResourceType 确保与 v1 锁键完全一致
	return fmt.Sprintf("dbrms:lock:%s", model.NormalizeResourceType(p.ResourceType))
}

// GetOperationInfo 构建操作信息（用于记录）
func (p *RequestInputParamV2) GetOperationInfo(requestId, mode string, data []model.BatchGetTbDetailResult) model.TbRpOperationInfo {
	var count int
	var bkHostIds []int
	var ipList []string

	for _, v := range p.Details {
		count += v.Count
	}

	for _, group := range data {
		for _, detail := range group.Data {
			bkHostIds = append(bkHostIds, detail.BkHostID)
			ipList = append(ipList, detail.IP)
		}
	}

	var desc string
	bkHostIdsBytes, err := json.Marshal(bkHostIds)
	if err != nil {
		desc += "failed to serialize bkhost ids"
		logger.Error("json marshal failed %s", err.Error())
	}
	ipListBytes, err := json.Marshal(ipList)
	if err != nil {
		desc += "failed to serialize ipList"
		logger.Error("json marshal failed %s", err.Error())
	}

	return model.TbRpOperationInfo{
		RequestID:     requestId,
		TotalCount:    count,
		OperationType: model.Consumed,
		BkHostIds:     bkHostIdsBytes,
		IpList:        ipListBytes,
		BillId:        p.BillId,
		BillType:      p.BillType,
		TaskId:        p.TaskId,
		Operator:      p.Operator,
		Status:        mode,
		CreateTime:    time.Now(),
		UpdateTime:    time.Now(),
		Description:   desc,
	}
}

// BuildMessage 构建申请消息
func (p *RequestInputParamV2) BuildMessage() string {
	var count int
	groupMap := make(map[string]int)
	groupCountMap := make(map[string]int)
	for _, d := range p.Details {
		groupMap[d.Affinity]++
		groupCountMap[d.Affinity] += d.Count
		count += d.Count
	}
	msg := fmt.Sprintf("此次申请分%d组申请%d个机器\n", len(p.Details), count)
	for affinity, groupCount := range groupMap {
		msg += fmt.Sprintf("按照亲和性%s申请的资源分组%d,总共包含机器数量%d\n", affinity, groupCount, groupCountMap[affinity])
	}
	return msg
}

// GetAllAffinitys 获取所有亲和性
func (p *RequestInputParamV2) GetAllAffinitys() []string {
	var affinitys []string
	for _, d := range p.Details {
		if !lo.Contains(affinitys, d.Affinity) {
			affinitys = append(affinitys, d.Affinity)
		}
	}
	return affinitys
}
