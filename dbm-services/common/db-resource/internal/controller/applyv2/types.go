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
	"sort"
	"strings"
	"time"

	"dbm-services/common/db-resource/internal/config"
	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/apply"
	"dbm-services/common/db-resource/internal/svr/dbmapi"
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
	// AffinityDimension 全局亲和维度，决定整单的位置模式（过滤字段集 + 选机分桶 key）：
	//   - 空 / bk_cloud_zone（cloud 模式）：details[].location_spec 只允许
	//     bk_cloud_region / bk_cloud_zone_list / exclude_bk_cloud_zone_list，选机按 bk_cloud_zone 分桶；
	//   - sub_zone（legacy 模式）：details[].location_spec 只允许
	//     city / sub_zone_ids / exclude_sub_zone_ids / exclude_rack_ids / exclude_net_device_ids，
	//     选机按原生 SubZone/SubZoneID 分桶，行为对齐 v1。
	// 维度是整单语义，放在请求层而非 detail 层，结构上保证同一请求内不会混用两种模式。
	AffinityDimension string `json:"affinity_dimension"`
	apply.ActionInfo
}

// ObjectDetailV2 v2 资源申请对象详情
type ObjectDetailV2 struct {
	BkCloudId     int             `json:"bk_cloud_id"`
	Hosts         apply.Hosts     `json:"hosts"`
	CurrentHosts  []CurrentHostV2 `json:"current_hosts"`
	GroupMark     string          `json:"group_mark" binding:"required"`
	Labels        []string        `json:"labels"`
	DeviceClass   []string        `json:"device_class"`
	Spec          meta.Spec       `json:"spec"`
	StorageSpecs  []meta.DiskSpec `json:"storage_spec"`
	LocationSpec  LocationSpecV2  `json:"location_spec"`
	Affinity      string          `json:"affinity"`
	Tolerance     float64         `json:"tolerance"`
	OsType        string          `json:"os_type"`
	OsNames       []string        `json:"os_names"`
	ExcludeOsName bool            `json:"exclude_os_name"`
	Count         int             `json:"count" binding:"required,min=1"`
}

// LocationSpecV2 v2 位置规格。
// 位置模式由请求级 affinity_dimension 决定，两种模式的字段互斥，混传在 ParamCheck 阶段整单拒绝（4xx），
// 不做静默忽略——避免调用方误以为未生效的过滤条件已经生效：
//   - cloud 模式（affinity_dimension 为空或 bk_cloud_zone）：BkCloudRegion / BkCloudZoneList / ExcludeBkCloudZoneList；
//   - legacy 模式（affinity_dimension=sub_zone）：City / SubZoneIds / ExcludeSubZoneIds / ExcludeRackIds /
//     ExcludeNetDeviceIds，字段含义与 v1 meta.LocationSpec 相同；
//     不含 v1 中即将废弃的 include_or_exclue，SubZoneIds 语义固定为「包含」。
type LocationSpecV2 struct {
	BkCloudRegion          string   `json:"bk_cloud_region"`
	BkCloudZoneList        []string `json:"bk_cloud_zone_list"`
	ExcludeBkCloudZoneList []string `json:"exclude_bk_cloud_zone_list,omitempty"`
	// legacy 模式字段（affinity_dimension=sub_zone 时生效，字段含义与 v1 meta.LocationSpec 一致）
	City                string   `json:"city"`
	SubZoneIds          []string `json:"sub_zone_ids"`
	ExcludeSubZoneIds   []string `json:"exclude_sub_zone_ids,omitempty"`
	ExcludeRackIds      []string `json:"exclude_rack_ids,omitempty"`
	ExcludeNetDeviceIds []string `json:"exclude_net_device_ids,omitempty"`
	// ForcedSubZoneIds 仅用于 groups_in_same_location + legacy 模式内部锁定同一园区，不对外暴露
	ForcedSubZoneIds []string `json:"-"`
	// IdcCitys city 经 dbmapi.GetIdcCityByLogicCity 转换出的真实城市列表，内部字段不对外暴露。
	// legacy 模式下由 RequestInputParamV2.ResolveIdcCitys 在进入选机前统一解析（dev 模式跳过），
	// 传递给 v1 MatchLocationSpec 做 city in (...) 过滤；转换失败时整单失败，与 v1 行为一致。
	IdcCitys []string `json:"-"`
}

// 请求级亲和维度的合法取值（AffinityDimension 字段）
const (
	// AffinityDimensionCloud 云可用区维度（cloud 模式，也是默认值）
	AffinityDimensionCloud = "bk_cloud_zone"
	// AffinityDimensionSubZone 园区维度（legacy 模式）
	AffinityDimensionSubZone = "sub_zone"
)

// locationMode 归一化亲和维度：sub_zone 返回 sub_zone，其余（含空值、空白）返回 bk_cloud_zone。
// 入参为请求级 affinity_dimension；返回值决定位置过滤字段集与选机分桶 key。
func locationMode(dimension string) string {
	if strings.TrimSpace(dimension) == AffinityDimensionSubZone {
		return AffinityDimensionSubZone
	}
	return AffinityDimensionCloud
}

// IsEmpty 判断当前模式下位置规格是否为空。
// dimension 为请求级 affinity_dimension（空值按 bk_cloud_zone 处理）：
// cloud 模式看 BkCloudRegion；legacy 模式看 City（与 v1 meta.LocationSpec.IsEmpty 行为一致）。
func (l LocationSpecV2) IsEmpty(dimension string) bool {
	if locationMode(dimension) == AffinityDimensionSubZone {
		return cmutil.IsEmpty(l.City)
	}
	return cmutil.IsEmpty(l.BkCloudRegion)
}

// CurrentHostV2 v2 当前主机信息（用于亲和容忍度计算）。
// 主维字段由请求级 affinity_dimension 决定：bk_cloud_zone 用 BkCloudZone，sub_zone 用 SubZone。
type CurrentHostV2 struct {
	BkHostId    int    `json:"bk_host_id" binding:"required,gt=0,number"`
	BkCloudZone string `json:"bk_cloud_zone"`
	SubZone     string `json:"sub_zone"` // 园区维度时的主维字段
	RackId      string `json:"rack_id" binding:"required,min=1,max=40"`
}

// hasCloudFields 是否填写了 cloud 模式字段，仅供混传校验使用
func (l LocationSpecV2) hasCloudFields() bool {
	return cmutil.IsNotEmpty(l.BkCloudRegion) || len(l.BkCloudZoneList) > 0 || len(l.ExcludeBkCloudZoneList) > 0
}

// hasLegacyFields 是否填写了 legacy 模式字段，仅供混传校验使用
func (l LocationSpecV2) hasLegacyFields() bool {
	return cmutil.IsNotEmpty(l.City) || len(l.SubZoneIds) > 0 || len(l.ExcludeSubZoneIds) > 0 ||
		len(l.ExcludeRackIds) > 0 || len(l.ExcludeNetDeviceIds) > 0
}

// FailDetail 申请失败时附带的请求条件，对应旧接口 GetMessage 里的机型、标签和地域。
// dimension 为请求级 affinity_dimension（空值按 cloud 模式处理）：
// cloud 模式输出云地域/云可用区/排除云可用区；legacy 模式输出城市/园区/排除园区/排除机架/排除网络设备。
func (d ObjectDetailV2) FailDetail(dimension string) string {
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
	if locationMode(dimension) == AffinityDimensionSubZone {
		// legacy 模式位置文案，字段与 v1 meta.LocationSpec 一致
		b.WriteString(fmt.Sprintf("城市: %s\n", d.LocationSpec.City))
		if len(d.LocationSpec.SubZoneIds) > 0 {
			b.WriteString(fmt.Sprintf("园区: %v\n", d.LocationSpec.SubZoneIds))
		}
		if len(d.LocationSpec.ExcludeSubZoneIds) > 0 {
			b.WriteString(fmt.Sprintf("排除园区: %v\n", d.LocationSpec.ExcludeSubZoneIds))
		}
		if len(d.LocationSpec.ExcludeRackIds) > 0 {
			b.WriteString(fmt.Sprintf("排除机架: %v\n", d.LocationSpec.ExcludeRackIds))
		}
		if len(d.LocationSpec.ExcludeNetDeviceIds) > 0 {
			b.WriteString(fmt.Sprintf("排除网络设备: %v\n", d.LocationSpec.ExcludeNetDeviceIds))
		}
	} else {
		b.WriteString(fmt.Sprintf("云地域: %s\n", d.LocationSpec.BkCloudRegion))
		if len(d.LocationSpec.BkCloudZoneList) > 0 {
			b.WriteString(fmt.Sprintf("云可用区: %v\n", d.LocationSpec.BkCloudZoneList))
		}
		if len(d.LocationSpec.ExcludeBkCloudZoneList) > 0 {
			b.WriteString(fmt.Sprintf("排除云可用区: %v\n", d.LocationSpec.ExcludeBkCloudZoneList))
		}
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

// ValidateAffinityDimension 校验请求级亲和维度白名单：允许空（默认 bk_cloud_zone）、bk_cloud_zone、sub_zone。
// 维度是整单唯一的位置模式，非法取值直接拒绝整个请求。
func (p *RequestInputParamV2) ValidateAffinityDimension() error {
	if p.AffinityDimension == "" || p.AffinityDimension == AffinityDimensionCloud ||
		p.AffinityDimension == AffinityDimensionSubZone {
		return nil
	}
	return fmt.Errorf("unsupported affinity_dimension '%s', only empty, 'bk_cloud_zone' or 'sub_zone' allowed",
		p.AffinityDimension)
}

// ValidateLocationFields 校验位置字段与模式的匹配（混传拒绝）：
// legacy 模式（sub_zone）下任何 detail 填了 cloud 字段、或 cloud 模式（bk_cloud_zone/空）下
// 任何 detail 填了 legacy 字段，均视为参数错误，整单返回 4xx。
// 拒绝而非静默忽略，避免调用方误以为未生效的过滤条件已经生效。
func (p *RequestInputParamV2) ValidateLocationFields() error {
	legacy := locationMode(p.AffinityDimension) == AffinityDimensionSubZone
	for idx, detail := range p.Details {
		if legacy && detail.LocationSpec.hasCloudFields() {
			return fmt.Errorf("detail[%d]: affinity_dimension='sub_zone' 不允许使用 "+
				"bk_cloud_region/bk_cloud_zone_list/exclude_bk_cloud_zone_list", idx)
		}
		if !legacy && detail.LocationSpec.hasLegacyFields() {
			return fmt.Errorf("detail[%d]: affinity_dimension='bk_cloud_zone' 不允许使用 "+
				"city/sub_zone_ids/exclude_sub_zone_ids/exclude_rack_ids/exclude_net_device_ids", idx)
		}
	}
	return nil
}

// ValidateCurrentHostsDimension 校验 current_hosts 的主维字段不能为空。
// 主维字段由请求级 affinity_dimension 决定：bk_cloud_zone 检查 bk_cloud_zone，sub_zone 检查 sub_zone。
func (p *RequestInputParamV2) ValidateCurrentHostsDimension() error {
	// 归一化主维（默认 bk_cloud_zone）
	dimension := locationMode(p.AffinityDimension)
	for idx, detail := range p.Details {
		// 检查对应维度的字段
		switch dimension {
		case AffinityDimensionCloud:
			for hostIdx, host := range detail.CurrentHosts {
				if cmutil.IsEmpty(host.BkCloudZone) {
					return fmt.Errorf("detail[%d].current_hosts[%d]: primary dimension field 'bk_cloud_zone' is empty when affinity_dimension='bk_cloud_zone', whole request rejected",
						idx, hostIdx)
				}
			}
		case AffinityDimensionSubZone:
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

// ValidateUniqueGroupMarks 校验 group_mark 唯一，避免响应维度映射按组标记串组。
func (p *RequestInputParamV2) ValidateUniqueGroupMarks() error {
	seen := make(map[string]struct{}, len(p.Details))
	for idx, detail := range p.Details {
		if _, ok := seen[detail.GroupMark]; ok {
			return fmt.Errorf("detail[%d]: duplicate group_mark '%s'", idx, detail.GroupMark)
		}
		seen[detail.GroupMark] = struct{}{}
	}
	return nil
}

// NormalizeDefaults 标准化默认值：
// 请求级 affinity_dimension 默认归一化为 bk_cloud_zone（cloud 模式）；
// 各 detail 的 affinity 与 v1 NormalizeAffinities 一致归一化。
func (p *RequestInputParamV2) NormalizeDefaults() {
	if p.AffinityDimension == "" {
		p.AffinityDimension = AffinityDimensionCloud
	}
	for i := range p.Details {
		p.Details[i].Affinity = apply.NormalizeAffinity(p.Details[i].Affinity)
	}
}

// ParamCheck v2 参数检查
func (p *RequestInputParamV2) ParamCheck() error {
	// 1. 标准化默认值（含 affinity 归一化，与 v1 NormalizeAffinities 对齐）
	p.NormalizeDefaults()

	// 2. 校验 group_mark 唯一
	if err := p.ValidateUniqueGroupMarks(); err != nil {
		return err
	}

	// 3. 校验亲和维度白名单
	if err := p.ValidateAffinityDimension(); err != nil {
		return err
	}

	// 3.5 校验位置字段与模式匹配（混传拒绝）
	if err := p.ValidateLocationFields(); err != nil {
		return err
	}

	// 4. 校验 current_hosts 主维不能为空
	if err := p.ValidateCurrentHostsDimension(); err != nil {
		return err
	}

	// 5. 校验同地点分组的位置一致性
	if err := p.ValidateGroupsInSameLocationV2(); err != nil {
		return err
	}

	// 6. 基本参数检查（复用 v1 逻辑）
	for idx := range p.Details {
		detail := &p.Details[idx]
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
		if detail.Tolerance < 0 || detail.Tolerance > 1 {
			return fmt.Errorf("detail[%d]: tolerance must be between 0 and 1, got %f", idx, detail.Tolerance)
		}
		if err := validateZoneListExclusive(detail.LocationSpec); err != nil {
			return fmt.Errorf("detail[%d]: %s", idx, err.Error())
		}

		// 亲和性合法性：以 v1 支持的全部 affinity 为准，未知值才拒绝
		if !lo.Contains(supportedAffinities(), detail.Affinity) {
			return fmt.Errorf("detail[%d]: unsupported affinity '%s'", idx, detail.Affinity)
		}

		// 亲和性检查（数量下限按模式取对应字段集：cloud 按 zone 列表，legacy 按园区列表且规则对齐 v1）
		if detail.Count <= 1 {
			continue
		}
		legacyMode := locationMode(p.AffinityDimension) == AffinityDimensionSubZone
		switch detail.Affinity {
		case apply.SAME_SUBZONE, apply.SAME_SUBZONE_CROSS_SWTICH:
			if detail.LocationSpec.IsEmpty(p.AffinityDimension) {
				if legacyMode {
					return fmt.Errorf("you need choose a city !!! ")
				}
				return fmt.Errorf("you need choose a cloud region")
			}
		case apply.CROS_SUBZONE:
			if detail.LocationSpec.IsEmpty(p.AffinityDimension) {
				if legacyMode {
					return fmt.Errorf("you need choose a city !!! ")
				}
				return fmt.Errorf("you need choose a cloud region")
			}
			if legacyMode {
				// 对齐 v1：指定园区列表时，跨园区至少需要 2 个园区
				if len(detail.LocationSpec.SubZoneIds) > 0 && len(detail.LocationSpec.SubZoneIds) < 2 {
					return fmt.Errorf("because need cross sub-zone,you special subzones need more than 2 subzones")
				}
				// 对齐 v1：tolerance=0 表示所有机器落在不同园区，指定园区列表时数量必须够分
				if detail.Tolerance == 0 && len(detail.LocationSpec.SubZoneIds) > 0 &&
					len(detail.LocationSpec.SubZoneIds) < detail.Count+len(detail.CurrentHosts) {
					return fmt.Errorf("tolerance=0 requires all machines in different subzones, "+
						"need at least %d subzones but only specified %d",
						detail.Count+len(detail.CurrentHosts), len(detail.LocationSpec.SubZoneIds))
				}
			} else {
				if len(detail.LocationSpec.BkCloudZoneList) > 0 && len(detail.LocationSpec.BkCloudZoneList) < 2 {
					return fmt.Errorf("cross cloud zone affinity requires at least 2 zones")
				}
				if detail.Tolerance == 0 && len(detail.LocationSpec.BkCloudZoneList) > 0 &&
					len(detail.LocationSpec.BkCloudZoneList) < detail.Count+len(detail.CurrentHosts) {
					return fmt.Errorf("tolerance=0 requires at least %d zones, but only specified %d",
						detail.Count+len(detail.CurrentHosts), len(detail.LocationSpec.BkCloudZoneList))
				}
			}
		case apply.CROSS_SUBZONE_STRONG:
			if detail.LocationSpec.IsEmpty(p.AffinityDimension) {
				if legacyMode {
					return fmt.Errorf("you need choose a city !!! ")
				}
				return fmt.Errorf("you need choose a cloud region")
			}
			if legacyMode {
				// 对齐 v1：跨园区(强)指定园区列表时至少 3 个园区
				if len(detail.LocationSpec.SubZoneIds) > 0 && len(detail.LocationSpec.SubZoneIds) < 3 {
					return fmt.Errorf("CROSS_SUBZONE_STRONG requires at least 3 subzones, but only specified %d",
						len(detail.LocationSpec.SubZoneIds))
				}
			} else {
				if len(detail.LocationSpec.BkCloudZoneList) > 0 && len(detail.LocationSpec.BkCloudZoneList) < 3 {
					return fmt.Errorf("CROSS_SUBZONE_STRONG requires at least 3 zones, but only specified %d",
						len(detail.LocationSpec.BkCloudZoneList))
				}
			}
		case apply.CROSS_SUBZONE_WEAK:
			if detail.LocationSpec.IsEmpty(p.AffinityDimension) {
				if legacyMode {
					return fmt.Errorf("you need choose a city !!! ")
				}
				return fmt.Errorf("you need choose a cloud region")
			}
			if legacyMode {
				// 对齐 v1：跨园区(弱)指定园区列表时至少 2 个园区
				if len(detail.LocationSpec.SubZoneIds) > 0 && len(detail.LocationSpec.SubZoneIds) < 2 {
					return fmt.Errorf("CROSS_SUBZONE_WEAK requires at least 2 subzones, but only specified %d",
						len(detail.LocationSpec.SubZoneIds))
				}
			} else {
				if len(detail.LocationSpec.BkCloudZoneList) > 0 && len(detail.LocationSpec.BkCloudZoneList) < 2 {
					return fmt.Errorf("CROSS_SUBZONE_WEAK requires at least 2 zones, but only specified %d",
						len(detail.LocationSpec.BkCloudZoneList))
				}
			}
		}
	}

	return nil
}

// supportedAffinities v2 支持的亲和性全集，与 v1 保持一致（PickInstanceBase 的 case 全集）
func supportedAffinities() []string {
	return []string{
		apply.NONE,
		apply.CROS_SUBZONE,
		apply.CROSS_SUBZONE_STRONG,
		apply.CROSS_SUBZONE_WEAK,
		apply.SAME_SUBZONE,
		apply.SAME_SUBZONE_CROSS_SWTICH,
		apply.CROSS_RACK,
		apply.MAX_EACH_ZONE_EQUAL,
		apply.MAJORITY_ELECTION_DISTRI,
	}
}

// validateZoneListExclusive 校验包含列表与排除列表无交集
func validateZoneListExclusive(spec LocationSpecV2) error {
	if len(spec.BkCloudZoneList) == 0 || len(spec.ExcludeBkCloudZoneList) == 0 {
		return nil
	}
	zoneSet := make(map[string]struct{}, len(spec.BkCloudZoneList))
	for _, zone := range spec.BkCloudZoneList {
		zoneSet[strings.TrimSpace(zone)] = struct{}{}
	}
	for _, zone := range spec.ExcludeBkCloudZoneList {
		if _, ok := zoneSet[strings.TrimSpace(zone)]; ok {
			return fmt.Errorf("zone '%s' appears in both bk_cloud_zone_list and exclude_bk_cloud_zone_list", zone)
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

// SortDetailsV2 与 v1 SortDetails 同策略：约束越具体（磁盘/位置/机型/数量）越优先处理，
// 避免宽松组先预选后导致严格组无资源可用。
// 位置条件的具体度按模式取对应字段：cloud 看 zone 列表，legacy 看园区/排除园区列表。
func (p *RequestInputParamV2) SortDetailsV2() []ObjectDetailV2 {
	if len(p.Details) <= 1 {
		return p.Details
	}
	sorted := make([]ObjectDetailV2, len(p.Details))
	copy(sorted, p.Details)
	dimension := p.AffinityDimension
	legacyMode := locationMode(dimension) == AffinityDimensionSubZone
	priority := func(d ObjectDetailV2) int64 {
		var score int64
		score += int64(len(d.StorageSpecs))
		if !d.LocationSpec.IsEmpty(dimension) {
			score++
			if legacyMode {
				if len(d.LocationSpec.SubZoneIds) > 0 || len(d.LocationSpec.ExcludeSubZoneIds) > 0 {
					score++
				}
			} else {
				if len(d.LocationSpec.BkCloudZoneList) > 0 || len(d.LocationSpec.ExcludeBkCloudZoneList) > 0 {
					score++
				}
			}
		}
		if len(d.DeviceClass) > 0 {
			score++
		}
		if d.Count > 1 {
			score += int64(d.Count)
		}
		return score
	}
	sort.SliceStable(sorted, func(i, j int) bool {
		return priority(sorted[i]) > priority(sorted[j])
	})
	return sorted
}

// ValidateGroupsInSameLocationV2 校验同地点申请的跨分组位置一致性（对齐 v1 ValidateGroupsInSameLocation）。
// 按请求级 affinity_dimension 分两种模式（模式为全局字段，同位置各组天然同模式）：
//   - legacy 模式（sub_zone）：所有分组 city 必须相同且非空；归一化后的园区条件
//     （sub_zone_ids + exclude_sub_zone_ids，排序/去空白/去重）必须一致；
//   - cloud 模式：所有分组 bk_cloud_region 必须相同且非空，归一化后的 zone 条件必须一致。
func (p *RequestInputParamV2) ValidateGroupsInSameLocationV2() error {
	if !p.GroupsInSameLocation || len(p.Details) <= 1 {
		return nil
	}
	first := p.Details[0]
	if locationMode(p.AffinityDimension) == AffinityDimensionSubZone {
		// legacy 模式：city 一致 + 园区条件一致（对齐 v1 locationSubZoneKey 语义，不含 include_or_exclue）
		firstCity := strings.TrimSpace(first.LocationSpec.City)
		if firstCity == "" {
			return fmt.Errorf("groups_in_same_location=true 时各分组必须指定相同的 city, 分组 %s 的 city 为空",
				first.GroupMark)
		}
		firstKey := locationV2SubZoneKey(first.LocationSpec)
		for _, detail := range p.Details[1:] {
			city := strings.TrimSpace(detail.LocationSpec.City)
			if city != firstCity {
				return fmt.Errorf("groups_in_same_location=true 时各分组 city 必须相同: 分组 %s=%s, 分组 %s=%s",
					first.GroupMark, firstCity, detail.GroupMark, city)
			}
			if key := locationV2SubZoneKey(detail.LocationSpec); key != firstKey {
				return fmt.Errorf("groups_in_same_location=true 时各分组 sub_zone 必须相同: 分组 %s=%q, 分组 %s=%q",
					first.GroupMark, firstKey, detail.GroupMark, key)
			}
		}
		return nil
	}
	// cloud 模式：region 一致 + zone 条件一致
	firstRegion := strings.TrimSpace(first.LocationSpec.BkCloudRegion)
	if firstRegion == "" {
		return fmt.Errorf("groups_in_same_location=true 时各分组必须指定相同的 bk_cloud_region, 分组 %s 的 bk_cloud_region 为空",
			first.GroupMark)
	}
	firstZones := locationV2ZoneKey(first.LocationSpec)
	for _, detail := range p.Details[1:] {
		region := strings.TrimSpace(detail.LocationSpec.BkCloudRegion)
		if region != firstRegion {
			return fmt.Errorf("groups_in_same_location=true 时各分组 bk_cloud_region 必须相同: 分组 %s=%s, 分组 %s=%s",
				first.GroupMark, firstRegion, detail.GroupMark, region)
		}
		if zones := locationV2ZoneKey(detail.LocationSpec); zones != firstZones {
			return fmt.Errorf("groups_in_same_location=true 时各分组 bk_cloud_zone 必须相同: 分组 %s=%q, 分组 %s=%q",
				first.GroupMark, firstZones, detail.GroupMark, zones)
		}
	}
	return nil
}

// locationV2SubZoneKey 把 legacy 模式的园区条件归一化成可比字符串：
// 去掉空白与重复项并排序，顺序差异不影响判定；未指定任何园区条件时返回空串。
// v2 不支持 include_or_exclue，SubZoneIds 语义固定为「包含」。
func locationV2SubZoneKey(spec LocationSpecV2) string {
	subZoneIds := normalizeV2Zones(spec.SubZoneIds)
	excludeSubZoneIds := normalizeV2Zones(spec.ExcludeSubZoneIds)
	if len(subZoneIds) == 0 && len(excludeSubZoneIds) == 0 {
		return ""
	}
	return fmt.Sprintf("sub_zone_ids=%v,exclude_sub_zone_ids=%v", subZoneIds, excludeSubZoneIds)
}

// ResolveIdcCitys 解析逻辑城市（仅 legacy 模式生效）。
// 行为对齐 v1：city 非空的 detail 调用 dbmapi.GetIdcCityByLogicCity 把逻辑城市换成真实城市列表，
// 结果写入 detail.LocationSpec.IdcCitys，供 toSearchContext 传递给 v1 MatchLocationSpec 做 city in (...) 过滤；
// dev 模式（config.AppConfig.RunMode == "dev"）跳过转换，IdcCitys 保持为空；
// 转换失败则整单报错，与 v1 cycleApplySequential 的失败路径一致。
// cloud 模式不做逻辑城市转换，直接返回。
// 前置条件：必须在 NormalizeDefaults 之后调用（依赖 affinity_dimension 已归一化），
// 且必须在选机循环之前调用一次（每次申请只做一次远程调用，选机各环节复用结果）。
func (p *RequestInputParamV2) ResolveIdcCitys() error {
	if locationMode(p.AffinityDimension) != AffinityDimensionSubZone {
		return nil
	}
	// dev 模式跳过逻辑城市转换，IdcCitys 保持为空，
	// v1 MatchLocationSpec 会退化为 city 等值匹配（与 v1 getLogicIdcCitys 的 dev 行为一致）
	if config.AppConfig.RunMode == "dev" {
		return nil
	}
	for i := range p.Details {
		city := strings.TrimSpace(p.Details[i].LocationSpec.City)
		if city == "" {
			continue
		}
		idcCitys, err := dbmapi.GetIdcCityByLogicCity(city)
		if err != nil {
			logger.Error("request real cites by logic city %s from bk-dbm api failed:%v", city, err)
			return fmt.Errorf("get logic city %s failed: %w", city, err)
		}
		p.Details[i].LocationSpec.IdcCitys = idcCitys
	}
	return nil
}

// locationV2ZoneKey 把 v2 云可用区配置归一化成可比字符串
func locationV2ZoneKey(spec LocationSpecV2) string {
	zones := normalizeV2Zones(spec.BkCloudZoneList)
	excludeZones := normalizeV2Zones(spec.ExcludeBkCloudZoneList)
	if len(zones) == 0 && len(excludeZones) == 0 {
		return ""
	}
	return fmt.Sprintf("zones=%v,exclude_zones=%v", zones, excludeZones)
}

func normalizeV2Zones(zones []string) []string {
	result := make([]string, 0, len(zones))
	for _, zone := range zones {
		if zone = strings.TrimSpace(zone); zone != "" {
			result = append(result, zone)
		}
	}
	sort.Strings(result)
	return lo.Uniq(result)
}
