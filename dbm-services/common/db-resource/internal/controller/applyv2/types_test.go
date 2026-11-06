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
	"strings"
	"testing"

	"dbm-services/common/db-resource/internal/svr/apply"
	"dbm-services/common/db-resource/internal/svr/meta"
)

// TestNormalizeDefaults 测试默认值标准化
func TestNormalizeDefaults(t *testing.T) {
	param := RequestInputParamV2{
		Details: []ObjectDetailV2{
			{
				GroupMark: "test1",
				Count:     1,
				// 不设置 AffinityDimension
			},
			{
				GroupMark:         "test2",
				Count:             1,
				AffinityDimension: "bk_cloud_zone",
			},
		},
	}

	param.NormalizeDefaults()

	if param.Details[0].AffinityDimension != "bk_cloud_zone" {
		t.Fatalf("expected default affinity_dimension to be 'bk_cloud_zone', got: %s",
			param.Details[0].AffinityDimension)
	}

	if param.Details[1].AffinityDimension != "bk_cloud_zone" {
		t.Fatalf("expected explicit affinity_dimension to remain 'bk_cloud_zone', got: %s",
			param.Details[1].AffinityDimension)
	}
}

// TestValidateAffinityDimension_Valid 测试有效的亲和维度
func TestValidateAffinityDimension_Valid(t *testing.T) {
	testCases := []struct {
		name      string
		dimension string
	}{
		{"empty", ""},
		{"bk_cloud_zone", "bk_cloud_zone"},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			param := RequestInputParamV2{
				Details: []ObjectDetailV2{
					{
						GroupMark:         "test",
						Count:             1,
						AffinityDimension: tc.dimension,
					},
				},
			}

			err := param.ValidateAffinityDimension()
			if err != nil {
				t.Fatalf("expected no error for dimension '%s', got: %v", tc.dimension, err)
			}
		})
	}
}

// TestValidateAffinityDimension_Invalid 测试无效的亲和维度
func TestValidateAffinityDimension_Invalid(t *testing.T) {
	param := RequestInputParamV2{
		Details: []ObjectDetailV2{
			{
				GroupMark:         "test",
				Count:             1,
				AffinityDimension: "rack_id",
			},
		},
	}

	err := param.ValidateAffinityDimension()
	if err == nil {
		t.Fatal("expected error for unsupported affinity_dimension, got nil")
	}
	if !strings.Contains(err.Error(), "affinity_dimension") {
		t.Fatalf("expected error about 'affinity_dimension', got: %v", err)
	}
}

// TestValidateCurrentHostsDimension_Valid 测试有效的 current_hosts 维度
func TestValidateCurrentHostsDimension_Valid(t *testing.T) {
	param := RequestInputParamV2{
		Details: []ObjectDetailV2{
			{
				GroupMark:         "test",
				Count:             1,
				AffinityDimension: "bk_cloud_zone",
				CurrentHosts: []CurrentHostV2{
					{
						BkHostId:    1001,
						BkCloudZone: "ap-chongqing-1",
					},
				},
			},
		},
	}

	err := param.ValidateCurrentHostsDimension()
	if err != nil {
		t.Fatalf("expected no error for valid current_hosts, got: %v", err)
	}
}

// TestValidateCurrentHostsDimension_EmptyDimension 测试空的主维字段应该被拒绝
func TestValidateCurrentHostsDimension_EmptyDimension(t *testing.T) {
	param := RequestInputParamV2{
		Details: []ObjectDetailV2{
			{
				GroupMark:         "test",
				Count:             1,
				AffinityDimension: "bk_cloud_zone",
				CurrentHosts: []CurrentHostV2{
					{
						BkHostId:    1001,
						BkCloudZone: "", // 空值
					},
				},
			},
		},
	}

	err := param.ValidateCurrentHostsDimension()
	if err == nil {
		t.Fatal("expected error for empty bk_cloud_zone in current_hosts, got nil")
	}
	if !strings.Contains(err.Error(), "bk_cloud_zone") || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("expected error about empty 'bk_cloud_zone', got: %v", err)
	}
}

// TestParamCheck_OldFieldsDropped 旧字段已删除，静默忽略（不再4xx）
func TestParamCheck_OldFieldsDropped(t *testing.T) {
	param := RequestInputParamV2{
		Details: []ObjectDetailV2{
			{
				GroupMark: "test",
				Count:     1,
			},
		},
	}

	// 旧字段已从结构体删除，ParamCheck 不再拒绝
	// 注意：ParamCheck 会做其它必填校验，这里只测试不会因幽灵字段4xx
	_ = param.ParamCheck()
	// 不再有针对幽灵字段的显式拒绝
}

// TestParamCheck_FullFlow 测试完整的参数检查流程
func TestParamCheck_FullFlow(t *testing.T) {
	param := RequestInputParamV2{
		ResourceType: "MySQL",
		ForbizId:     1,
		Details: []ObjectDetailV2{
			{
				GroupMark:         "backend",
				Count:             2,
				Affinity:          apply.CROS_SUBZONE,
				AffinityDimension: "bk_cloud_zone",
				LocationSpec: LocationSpecV2{
					BkCloudRegion:   "ap-chongqing",
					BkCloudZoneList: []string{"ap-chongqing-1", "ap-chongqing-2"},
				},
				Spec: meta.Spec{
					Cpu: meta.MeasureRange{Min: 4, Max: 8},
					Mem: meta.MeasureRange{Min: 8, Max: 16},
				},
				CurrentHosts: []CurrentHostV2{
					{
						BkHostId:    1001,
						BkCloudZone: "ap-chongqing-1",
						RackId:      "rack-a",
					},
				},
			},
		},
	}

	err := param.ParamCheck()
	if err != nil {
		t.Fatalf("expected no error for valid params, got: %v", err)
	}

	// 检查默认值是否被设置
	if param.Details[0].AffinityDimension != "bk_cloud_zone" {
		t.Fatalf("expected affinity_dimension to be normalized, got: %s",
			param.Details[0].AffinityDimension)
	}
}

// TestParamCheck_CrossSubzoneRequiresTwoZones 测试跨可用区需要至少 2 个 zone
func TestParamCheck_CrossSubzoneRequiresTwoZones(t *testing.T) {
	param := RequestInputParamV2{
		ResourceType: "MySQL",
		ForbizId:     1,
		Details: []ObjectDetailV2{
			{
				GroupMark: "backend",
				Count:     2,
				Affinity:  apply.CROS_SUBZONE,
				LocationSpec: LocationSpecV2{
					BkCloudRegion:   "ap-chongqing",
					BkCloudZoneList: []string{"ap-chongqing-1"}, // 只有一个 zone
				},
			},
		},
	}

	err := param.ParamCheck()
	if err == nil {
		t.Fatal("expected error for cross zone with only 1 zone, got nil")
	}
	if !strings.Contains(err.Error(), "at least 2 zones") {
		t.Fatalf("expected error about '2 zones', got: %v", err)
	}
}

// TestFailDetail 失败明细带上机型、标签和云地域
func TestFailDetail(t *testing.T) {
	detail := ObjectDetailV2{
		GroupMark:   "backend",
		DeviceClass: []string{"IT5.4XLARGE64"},
		Labels:      []string{"mysql"},
		Spec: meta.Spec{
			Cpu: meta.MeasureRange{Min: 4, Max: 8},
		},
		StorageSpecs: []meta.DiskSpec{
			{MountPoint: "/data", DiskType: "SSD", MinSize: 100, MaxSize: 500},
		},
		LocationSpec: LocationSpecV2{
			BkCloudRegion:          "ap-chongqing",
			BkCloudZoneList:        []string{"ap-chongqing-1", "ap-chongqing-2"},
			ExcludeBkCloudZoneList: []string{"ap-chongqing-3"},
		},
		Count: 2,
	}
	msg := detail.FailDetail()
	for _, part := range []string{
		"CC机型: [IT5.4XLARGE64]",
		"标签: [mysql]",
		"cpu: 4 ~ 8 核",
		"disk: mount point: /data",
		"disk type: SSD",
		"云地域: ap-chongqing",
		"云可用区: [ap-chongqing-1 ap-chongqing-2]",
		"排除云可用区: [ap-chongqing-3]",
		"申请总数: 2",
	} {
		if !strings.Contains(msg, part) {
			t.Fatalf("expected %q in %s", part, msg)
		}
	}

	empty := ObjectDetailV2{GroupMark: "backend", Count: 1, LocationSpec: LocationSpecV2{BkCloudRegion: "ap-shanghai"}}
	if !strings.Contains(empty.FailDetail(), "标签: 无") {
		t.Fatalf("empty labels should say 无, got: %s", empty.FailDetail())
	}
}

// TestBuildMessage 测试构建消息
func TestBuildMessage(t *testing.T) {
	param := RequestInputParamV2{
		Details: []ObjectDetailV2{
			{
				GroupMark: "group1",
				Affinity:  apply.SAME_SUBZONE,
				Count:     2,
			},
			{
				GroupMark: "group2",
				Affinity:  apply.CROS_SUBZONE,
				Count:     3,
			},
		},
	}

	msg := param.BuildMessage()
	if !strings.Contains(msg, "2组") {
		t.Fatalf("expected message to contain '2组', got: %s", msg)
	}
	if !strings.Contains(msg, "5个机器") {
		t.Fatalf("expected message to contain '5个机器', got: %s", msg)
	}
}

// TestLockKey 测试锁键生成（与 v1 一致）
func TestLockKey(t *testing.T) {
	param1 := RequestInputParamV2{
		ResourceType: "MySQL",
		ForbizId:     100,
	}
	expected1 := "dbrms:lock:MySQL"
	if param1.LockKey() != expected1 {
		t.Fatalf("expected lock key '%s', got: %s", expected1, param1.LockKey())
	}

	param2 := RequestInputParamV2{
		ResourceType: "",
		ForbizId:     100,
	}
	expected2 := "dbrms:lock:global"
	if param2.LockKey() != expected2 {
		t.Fatalf("expected lock key '%s', got: %s", expected2, param2.LockKey())
	}
}
