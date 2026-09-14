/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package manage

import (
	"testing"

	"dbm-services/common/db-resource/internal/config"
	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/bk"
)

func ext3DataDiskMap() map[string]*bk.ShellResCollection {
	return map[string]*bk.ShellResCollection{
		"127.0.0.1": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/data", DiskDetail: bk.DiskDetail{FileType: "ext3"}},
			},
		},
	}
}

func TestMaybeCollectExt3DataDiskIps_DisabledSkip(t *testing.T) {
	orig := config.AppConfig.CheckExt3DataDisk
	config.AppConfig.CheckExt3DataDisk = false
	t.Cleanup(func() { config.AppConfig.CheckExt3DataDisk = orig })

	if hits := maybeCollectExt3DataDiskIps(ext3DataDiskMap()); len(hits) != 0 {
		t.Fatalf("disabled check should skip, got: %v", hits)
	}
}

func TestMaybeCollectExt3DataDiskIps_EnabledHit(t *testing.T) {
	orig := config.AppConfig.CheckExt3DataDisk
	config.AppConfig.CheckExt3DataDisk = true
	t.Cleanup(func() { config.AppConfig.CheckExt3DataDisk = orig })

	hits := maybeCollectExt3DataDiskIps(ext3DataDiskMap())
	if _, ok := hits["127.0.0.1"]; !ok {
		t.Fatalf("enabled check should collect ext3 ip, got: %v", hits)
	}
}

func TestCollectExt3DataDiskIps_RootExt3Ignored(t *testing.T) {
	diskMap := map[string]*bk.ShellResCollection{
		"127.0.0.1": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/", DiskDetail: bk.DiskDetail{FileType: "ext3"}},
				{MountPoint: "/data", DiskDetail: bk.DiskDetail{FileType: "ext4"}},
			},
		},
	}
	if hits := collectExt3DataDiskIps(diskMap); len(hits) != 0 {
		t.Fatalf("root ext3 should be ignored, got: %v", hits)
	}
}

func TestCollectExt3DataDiskIps_DataExt3Hit(t *testing.T) {
	diskMap := map[string]*bk.ShellResCollection{
		"127.0.0.1": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/", DiskDetail: bk.DiskDetail{FileType: "ext4"}},
				{MountPoint: "/data", DiskDetail: bk.DiskDetail{FileType: "ext3"}},
			},
		},
	}
	hits := collectExt3DataDiskIps(diskMap)
	if _, ok := hits["127.0.0.1"]; !ok {
		t.Fatalf("expected 127.0.0.1 when /data is ext3, got: %v", hits)
	}
}

func TestCollectExt3DataDiskIps_Data1Data2Ext3Hit(t *testing.T) {
	diskMap := map[string]*bk.ShellResCollection{
		"127.0.0.1": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/data1", DiskDetail: bk.DiskDetail{FileType: "ext3"}},
			},
		},
		"127.0.0.2": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/data2", DiskDetail: bk.DiskDetail{FileType: "EXT3"}},
			},
		},
	}
	hits := collectExt3DataDiskIps(diskMap)
	if _, ok := hits["127.0.0.1"]; !ok {
		t.Fatalf("expected 127.0.0.1 for /data1, got: %v", hits)
	}
	if _, ok := hits["127.0.0.2"]; !ok {
		t.Fatalf("expected 127.0.0.2 for /data2, got: %v", hits)
	}
}

func TestCollectExt3DataDiskIps_CaseInsensitive(t *testing.T) {
	diskMap := map[string]*bk.ShellResCollection{
		"127.0.0.2": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/data", DiskDetail: bk.DiskDetail{FileType: "EXT3"}},
			},
		},
	}
	if hits := collectExt3DataDiskIps(diskMap); len(hits) != 1 {
		t.Fatalf("expected hit when file_type is EXT3, got: %v", hits)
	}
}

func TestCollectExt3DataDiskIps_MixedBatchOnlyViolatingIP(t *testing.T) {
	diskMap := map[string]*bk.ShellResCollection{
		"127.0.0.1": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/data", DiskDetail: bk.DiskDetail{FileType: "xfs"}},
			},
		},
		"127.0.0.2": {
			Disk: []bk.DiskInfo{
				{MountPoint: "/data1", DiskDetail: bk.DiskDetail{FileType: "ext3"}},
			},
		},
	}
	hits := collectExt3DataDiskIps(diskMap)
	if _, ok := hits["127.0.0.2"]; !ok {
		t.Fatalf("expected violating ip 127.0.0.2, got: %v", hits)
	}
	if _, ok := hits["127.0.0.1"]; ok {
		t.Fatalf("non-ext3 host should not be collected, got: %v", hits)
	}
}

func TestCollectExt3DataDiskIps_EmptyOrNilHostSkipped(t *testing.T) {
	if hits := collectExt3DataDiskIps(nil); len(hits) != 0 {
		t.Fatalf("nil map should be empty, got: %v", hits)
	}
	if hits := collectExt3DataDiskIps(map[string]*bk.ShellResCollection{}); len(hits) != 0 {
		t.Fatalf("empty map should be empty, got: %v", hits)
	}
	diskMap := map[string]*bk.ShellResCollection{
		"127.0.0.1": nil,
	}
	if hits := collectExt3DataDiskIps(diskMap); len(hits) != 0 {
		t.Fatalf("nil host shell result should be empty, got: %v", hits)
	}
}

func TestApplyExt3UnavailableStatus(t *testing.T) {
	el := model.TbRpDetail{IP: "127.0.0.1", Status: model.Unused}
	applyExt3UnavailableStatus(&el, map[string]struct{}{"127.0.0.1": {}})
	if el.Status != model.Unavailable {
		t.Fatalf("expected Unavailable, got %s", el.Status)
	}

	keep := model.TbRpDetail{IP: "127.0.0.2", Status: model.Unused}
	applyExt3UnavailableStatus(&keep, map[string]struct{}{"127.0.0.1": {}})
	if keep.Status != model.Unused {
		t.Fatalf("non-hit host should stay Unused, got %s", keep.Status)
	}
	applyExt3UnavailableStatus(nil, map[string]struct{}{"127.0.0.1": {}})
}

func TestImportedExt3Ips(t *testing.T) {
	elems := []model.TbRpDetail{
		{IP: "127.0.0.2", Status: model.Unavailable},
		{IP: "127.0.0.1", Status: model.Unused},
		{IP: "127.0.0.3", Status: model.Unavailable},
	}
	got := importedExt3Ips(elems)
	if len(got) != 2 || got[0] != "127.0.0.2" || got[1] != "127.0.0.3" {
		t.Fatalf("expected sorted unavailable ips, got %v", got)
	}
}
