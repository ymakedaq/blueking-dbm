package applyv2

import (
	"testing"

	"dbm-services/common/db-resource/internal/model"
)

// TestBuildV2Response_SummaryWritten 测试 Summary 键被真正写入且清除旧园区键
func TestBuildV2Response_SummaryWritten(t *testing.T) {
	input := []model.BatchGetTbDetailResult{
		{
			Item: "group1",
			Data: []model.TbRpDetail{
				{BkCloudZone: "zone-a", RackID: "rack1"},
				{BkCloudZone: "zone-a", RackID: "rack1"},
				{BkCloudZone: "zone-a", RackID: "rack2"},
				{BkCloudZone: "zone-b", RackID: "rack3"},
				{BkCloudZone: "zone-b", RackID: "rack3"},
			},
			Summary: map[string]interface{}{
				"by_city_subzone": "old_data_should_be_cleared",
				"by_subzone_rack": "old_data_should_be_cleared",
				"by_subzone":      "old_data_should_be_cleared",
			},
		},
	}
	
	result := buildV2Response(input)
	
	if len(result) != 1 {
		t.Fatalf("expected 1 item, got %d", len(result))
	}
	
	// 验证 Summary 存在
	if result[0].Summary == nil {
		t.Fatal("Summary should not be nil")
	}
	
	// 验证 by_cloud_zone 键已写入
	byCloudZone, ok := result[0].Summary["by_cloud_zone"].(map[string]int)
	if !ok {
		t.Fatalf("Summary[by_cloud_zone] must exist and be map[string]int, got %T", result[0].Summary["by_cloud_zone"])
	}
	
	if byCloudZone["zone-a"] != 3 {
		t.Errorf("expected zone-a count=3, got %d", byCloudZone["zone-a"])
	}
	if byCloudZone["zone-b"] != 2 {
		t.Errorf("expected zone-b count=2, got %d", byCloudZone["zone-b"])
	}
	
	// 验证 by_cloud_zone_rack 键已写入
	byCloudZoneRack, ok := result[0].Summary["by_cloud_zone_rack"].(map[string]map[string]int)
	if !ok {
		t.Fatalf("Summary[by_cloud_zone_rack] must exist and be map[string]map[string]int, got %T", result[0].Summary["by_cloud_zone_rack"])
	}
	
	if byCloudZoneRack["zone-a"]["rack1"] != 2 {
		t.Errorf("expected zone-a rack1 count=2, got %d", byCloudZoneRack["zone-a"]["rack1"])
	}
	if byCloudZoneRack["zone-a"]["rack2"] != 1 {
		t.Errorf("expected zone-a rack2 count=1, got %d", byCloudZoneRack["zone-a"]["rack2"])
	}
	if byCloudZoneRack["zone-b"]["rack3"] != 2 {
		t.Errorf("expected zone-b rack3 count=2, got %d", byCloudZoneRack["zone-b"]["rack3"])
	}
	
	// 断言旧园区键已清除
	if _, exists := result[0].Summary["by_city_subzone"]; exists {
		t.Error("old key by_city_subzone should be cleared")
	}
	if _, exists := result[0].Summary["by_subzone_rack"]; exists {
		t.Error("old key by_subzone_rack should be cleared")
	}
	if _, exists := result[0].Summary["by_subzone"]; exists {
		t.Error("old key by_subzone should be cleared")
	}
	
	// 断言只有云可用区键
	if len(result[0].Summary) != 2 {
		t.Errorf("Summary should only have 2 keys (by_cloud_zone, by_cloud_zone_rack), got %d keys: %v", 
			len(result[0].Summary), result[0].Summary)
	}
}

// TestBuildV2Response_DetailFieldMapping 测试明细字段映射
func TestBuildV2Response_DetailFieldMapping(t *testing.T) {
	input := []model.BatchGetTbDetailResult{
		{
			Item: "group1",
			Data: []model.TbRpDetail{
				{BkCloudZone: "cn-south-1a", SubZone: "old-subzone", SubZoneID: "old-id"},
			},
		},
	}
	
	result := buildV2Response(input)
	
	// 验证明细字段已映射为云可用区
	if result[0].Data[0].SubZone != "cn-south-1a" {
		t.Errorf("expected SubZone=cn-south-1a, got %s", result[0].Data[0].SubZone)
	}
	if result[0].Data[0].SubZoneID != "cn-south-1a" {
		t.Errorf("expected SubZoneID=cn-south-1a, got %s", result[0].Data[0].SubZoneID)
	}
}

// TestBuildV2Response_MultipleGroups 测试多组场景
func TestBuildV2Response_MultipleGroups(t *testing.T) {
	input := []model.BatchGetTbDetailResult{
		{
			Item: "group1",
			Data: []model.TbRpDetail{
				{BkCloudZone: "zone-a", RackID: "rack1"},
			},
		},
		{
			Item: "group2",
			Data: []model.TbRpDetail{
				{BkCloudZone: "zone-b", RackID: "rack2"},
				{BkCloudZone: "zone-b", RackID: "rack3"},
			},
		},
	}
	
	result := buildV2Response(input)
	
	if len(result) != 2 {
		t.Fatalf("expected 2 items, got %d", len(result))
	}
	
	// 验证每组都有独立 Summary 且键已写入
	for i, item := range result {
		if item.Summary == nil {
			t.Errorf("group %d Summary should not be nil", i)
		}
		if _, ok := item.Summary["by_cloud_zone"]; !ok {
			t.Errorf("group %d missing by_cloud_zone key", i)
		}
		if _, ok := item.Summary["by_cloud_zone_rack"]; !ok {
			t.Errorf("group %d missing by_cloud_zone_rack key", i)
		}
	}
}
