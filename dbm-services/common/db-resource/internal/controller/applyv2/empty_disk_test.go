package applyv2

import (
	"encoding/json"
	"testing"

	"dbm-services/common/db-resource/internal/model"
	"dbm-services/common/db-resource/internal/svr/apply"
	"dbm-services/common/db-resource/internal/svr/bk"
	"dbm-services/common/db-resource/internal/svr/meta"
)

// TestFilterEmptyMountPointStorageV2_AlignWithV1 测试对齐 v1 语义
func TestFilterEmptyMountPointStorageV2_AlignWithV1(t *testing.T) {
	// 场景：规格占用 /data1, /data2，空挂载点规格要求 1 个 SSD 500GB+
	// 机器A：有 /data1(已占), /data2(已占), /data3(SSD 600GB, 未占) -> 匹配
	// 机器B：有 /data1(已占), /data2(已占), /data4(HDD 700GB, 未占) -> 不匹配(类型错)
	// 机器C：有 /data1(已占), /data2(已占) -> 不匹配(无未占用盘)
	
	storageSpecs := []meta.DiskSpec{
		{MountPoint: "/data1", DiskType: "ALL", MinSize: 100},
		{MountPoint: "/data2", DiskType: "ALL", MinSize: 100},
		{MountPoint: "", DiskType: "SSD", MinSize: 500}, // 空挂载点规格
	}
	
	emptyDiskSpecs := meta.GetEmptyDiskSpec(storageSpecs)
	if len(emptyDiskSpecs) != 1 {
		t.Fatalf("expected 1 empty disk spec, got %d", len(emptyDiskSpecs))
	}
	
	// 构造测试数据（需要设置 StorageDevice JSON）
	storages1 := map[string]bk.DiskDetail{
		"/data1": {DiskType: "SSD", Size: 200},
		"/data2": {DiskType: "SSD", Size: 200},
		"/data3": {DiskType: "SSD", Size: 600}, // 未占用，匹配
	}
	storageJSON1, _ := json.Marshal(storages1)
	
	storages2 := map[string]bk.DiskDetail{
		"/data1": {DiskType: "HDD", Size: 200},
		"/data2": {DiskType: "HDD", Size: 200},
		"/data4": {DiskType: "HDD", Size: 700}, // 未占用，类型不匹配
	}
	storageJSON2, _ := json.Marshal(storages2)
	
	storages3 := map[string]bk.DiskDetail{
		"/data1": {DiskType: "SSD", Size: 200},
		"/data2": {DiskType: "SSD", Size: 200},
		// 无未占用盘
	}
	storageJSON3, _ := json.Marshal(storages3)
	
	items := []model.TbRpDetail{
		{
			BkHostID:      1,
			IP:            "1.1.1.1",
			StorageDevice: json.RawMessage(storageJSON1),
		},
		{
			BkHostID:      2,
			IP:            "2.2.2.2",
			StorageDevice: json.RawMessage(storageJSON2),
		},
		{
			BkHostID:      3,
			IP:            "3.3.3.3",
			StorageDevice: json.RawMessage(storageJSON3),
		},
	}
	
	// 调用旧包导出方法（参数顺序：items, storageSpecs, emptyDiskSpecs）
	result, err := apply.FilterEmptyMountPointStorage(items, storageSpecs, emptyDiskSpecs)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	
	if len(result) != 1 {
		t.Fatalf("expected 1 host, got %d", len(result))
	}
	
	if result[0].BkHostID != 1 {
		t.Errorf("expected host 1, got %d", result[0].BkHostID)
	}
}

// TestFilterEmptyMountPointStorageV2_NoMatch 测试 0 命中返回资源不足错误
func TestFilterEmptyMountPointStorageV2_NoMatch(t *testing.T) {
	storageSpecs := []meta.DiskSpec{
		{MountPoint: "/data1", DiskType: "SSD", MinSize: 100},
		{MountPoint: "", DiskType: "NVME", MinSize: 1000}, // 空挂载点，要求 NVME 1TB+
	}
	
	emptyDiskSpecs := meta.GetEmptyDiskSpec(storageSpecs)
	
	storages := map[string]bk.DiskDetail{
		"/data1": {DiskType: "SSD", Size: 200},
		"/data2": {DiskType: "SSD", Size: 500}, // 未占用，但类型不是 NVME
	}
	storageJSON, _ := json.Marshal(storages)
	
	items := []model.TbRpDetail{
		{
			BkHostID:      1,
			StorageDevice: json.RawMessage(storageJSON),
		},
	}
	
	// 调用旧包导出方法
	_, err := apply.FilterEmptyMountPointStorage(items, storageSpecs, emptyDiskSpecs)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	
	if !contains(err.Error(), "匹配空挂载点磁盘的资源为 0") {
		t.Errorf("expected resource insufficient error, got: %s", err.Error())
	}
}

// TestFilterEmptyMountPointStorageV2_MultipleEmptySpecs 测试多个空挂载点规格
func TestFilterEmptyMountPointStorageV2_MultipleEmptySpecs(t *testing.T) {
	storageSpecs := []meta.DiskSpec{
		{MountPoint: "/data1", DiskType: "SSD", MinSize: 100},
		{MountPoint: "", DiskType: "SSD", MinSize: 200}, // 空挂载点 1
		{MountPoint: "", DiskType: "HDD", MinSize: 500}, // 空挂载点 2
	}
	
	emptyDiskSpecs := meta.GetEmptyDiskSpec(storageSpecs)
	if len(emptyDiskSpecs) != 2 {
		t.Fatalf("expected 2 empty disk specs, got %d", len(emptyDiskSpecs))
	}
	
	storages1 := map[string]bk.DiskDetail{
		"/data1": {DiskType: "SSD", Size: 200},
		"/data2": {DiskType: "SSD", Size: 300}, // 未占用，匹配第1个
		"/data3": {DiskType: "HDD", Size: 600}, // 未占用，匹配第2个
	}
	storageJSON1, _ := json.Marshal(storages1)
	
	storages2 := map[string]bk.DiskDetail{
		"/data1": {DiskType: "SSD", Size: 200},
		"/data2": {DiskType: "SSD", Size: 300}, // 未占用，匹配第1个
		// 缺少第2个规格的盘
	}
	storageJSON2, _ := json.Marshal(storages2)
	
	items := []model.TbRpDetail{
		{
			BkHostID:      1,
			StorageDevice: json.RawMessage(storageJSON1),
		},
		{
			BkHostID:      2,
			StorageDevice: json.RawMessage(storageJSON2),
		},
	}
	
	// 调用旧包导出方法
	result, err := apply.FilterEmptyMountPointStorage(items, storageSpecs, emptyDiskSpecs)
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	
	if len(result) != 1 {
		t.Fatalf("expected 1 host, got %d", len(result))
	}
	
	if result[0].BkHostID != 1 {
		t.Errorf("expected host 1, got %d", result[0].BkHostID)
	}
}

// TestDiskDetailMatchV2 测试磁盘匹配逻辑
func TestDiskDetailMatchV2(t *testing.T) {
	tests := []struct {
		name     string
		disk     bk.DiskDetail
		spec     meta.DiskSpec
		expected bool
	}{
		{
			name:     "type and size match",
			disk:     bk.DiskDetail{DiskType: "SSD", Size: 600},
			spec:     meta.DiskSpec{DiskType: "SSD", MinSize: 500, MaxSize: 1000},
			expected: true,
		},
		{
			name:     "type mismatch",
			disk:     bk.DiskDetail{DiskType: "HDD", Size: 600},
			spec:     meta.DiskSpec{DiskType: "SSD", MinSize: 500},
			expected: false,
		},
		{
			name:     "size too small",
			disk:     bk.DiskDetail{DiskType: "SSD", Size: 400},
			spec:     meta.DiskSpec{DiskType: "SSD", MinSize: 500},
			expected: false,
		},
		{
			name:     "size too large",
			disk:     bk.DiskDetail{DiskType: "SSD", Size: 1200},
			spec:     meta.DiskSpec{DiskType: "SSD", MinSize: 500, MaxSize: 1000},
			expected: false,
		},
		{
			name:     "empty disk type in spec matches any",
			disk:     bk.DiskDetail{DiskType: "NVME", Size: 600},
			spec:     meta.DiskSpec{DiskType: "", MinSize: 500},
			expected: true,
		},
	}
	
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// 调用旧包导出方法
			result := apply.DiskDetailMatch(tt.disk, tt.spec)
			if result != tt.expected {
				t.Errorf("expected %v, got %v", tt.expected, result)
			}
		})
	}
}

func contains(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
