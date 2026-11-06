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
	"testing"
)

// 这个测试文件不依赖数据库和配置，只测试纯函数逻辑

// TestNormalizeDefaultAffinityDimension 测试默认亲和维度标准化
func TestNormalizeDefaultAffinityDimension(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected string
	}{
		{
			name:     "empty string becomes default",
			input:    "",
			expected: "bk_cloud_zone",
		},
		{
			name:     "explicit bk_cloud_zone remains",
			input:    "bk_cloud_zone",
			expected: "bk_cloud_zone",
		},
		{
			name:     "explicit sub_zone remains",
			input:    "sub_zone",
			expected: "sub_zone",
		},
	}
	
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			param := RequestInputParamV2{
				Details: []ObjectDetailV2{
					{
						GroupMark:         "test",
						Count:             1,
						AffinityDimension: tt.input,
					},
				},
			}
			
			param.NormalizeDefaults()
			
			if param.Details[0].AffinityDimension != tt.expected {
				t.Errorf("NormalizeDefaults() dimension = %q, want %q",
					param.Details[0].AffinityDimension, tt.expected)
			}
		})
	}
}

// TestValidateAffinityDimensionWhitelist 测试亲和维度白名单
func TestValidateAffinityDimensionWhitelist(t *testing.T) {
	tests := []struct {
		name      string
		dimension string
		wantErr   bool
	}{
		{
			name:      "empty is valid",
			dimension: "",
			wantErr:   false,
		},
		{
			name:      "bk_cloud_zone is valid",
			dimension: "bk_cloud_zone",
			wantErr:   false,
		},
		{
			name:      "sub_zone is valid",
			dimension: "sub_zone",
			wantErr:   false,
		},
		{
			name:      "rack_id is invalid",
			dimension: "rack_id",
			wantErr:   true,
		},
		{
			name:      "arbitrary string is invalid",
			dimension: "custom_field",
			wantErr:   true,
		},
	}
	
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			param := RequestInputParamV2{
				Details: []ObjectDetailV2{
					{
						GroupMark:         "test",
						Count:             1,
						AffinityDimension: tt.dimension,
					},
				},
			}
			
			err := param.ValidateAffinityDimension()
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateAffinityDimension() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

// TestValidateCurrentHostsDimensionNotEmpty 测试 current_hosts 主维不能为空
func TestValidateCurrentHostsDimensionNotEmpty(t *testing.T) {
	tests := []struct {
		name             string
		affinityDimension string
		currentHost      CurrentHostV2
		wantErr          bool
	}{
		{
			name:             "non-empty bk_cloud_zone is valid",
			affinityDimension: "bk_cloud_zone",
			currentHost: CurrentHostV2{
				BkHostId:    1001,
				BkCloudZone: "ap-chongqing-1",
			},
			wantErr: false,
		},
		{
			name:             "empty bk_cloud_zone is invalid",
			affinityDimension: "bk_cloud_zone",
			currentHost: CurrentHostV2{
				BkHostId:    1001,
				BkCloudZone: "",
			},
			wantErr: true,
		},
		{
			name:             "non-empty sub_zone is valid",
			affinityDimension: "sub_zone",
			currentHost: CurrentHostV2{
				BkHostId: 1001,
				SubZone:  "sz1",
			},
			wantErr: false,
		},
		{
			name:             "empty sub_zone is invalid",
			affinityDimension: "sub_zone",
			currentHost: CurrentHostV2{
				BkHostId: 1001,
				SubZone:  "",
			},
			wantErr: true,
		},
	}
	
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			param := RequestInputParamV2{
				Details: []ObjectDetailV2{
					{
						GroupMark:         "test",
						Count:             1,
						AffinityDimension: tt.affinityDimension,
						CurrentHosts: []CurrentHostV2{tt.currentHost},
					},
				},
			}
			
			// NormalizeDefaults 会将空 affinity_dimension 转为 bk_cloud_zone
			param.NormalizeDefaults()
			
			err := param.ValidateCurrentHostsDimension()
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateCurrentHostsDimension() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

// 辅助函数
func ptrString(s string) *string {
	return &s
}

func ptrBool(b bool) *bool {
	return &b
}

func strPtr(s string) *string {
	return &s
}
