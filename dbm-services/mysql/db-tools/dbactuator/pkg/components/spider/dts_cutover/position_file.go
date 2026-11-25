/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package dts_cutover

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/dtsutil"
)

// PositionState 位点文件状态机（03 方案第 6.2 / 7.3 节）。
type PositionState string

const (
	// PositionStateStopping 停任务前写入；停任务结果未知时保留
	PositionStateStopping PositionState = "stopping"
	// PositionStateStopped 持锁确认全部停止且锁完好
	PositionStateStopped PositionState = "stopped"
	// PositionStateInconsistent 已停止但锁中途丢失，一致性不成立
	PositionStateInconsistent PositionState = "inconsistent"
)

// PositionFile 位点文件内容：写入 MysqlDtsInfo 后即为权威记录的本地缓存。
type PositionFile struct {
	State              PositionState                    `json:"state"`
	TaskName           string                           `json:"task_name"`
	LockMode           string                           `json:"lock_mode"`
	LockSnapshot       map[string]dtsutil.BinlogCoord   `json:"lock_snapshot"`
	Sources            []dtsutil.SourcePositionSnapshot `json:"sources"`
	LockAcquiredAt     string                           `json:"lock_acquired_at,omitempty"`
	StopIssuedAt       string                           `json:"stop_issued_at,omitempty"`
	StoppedConfirmedAt string                           `json:"stopped_confirmed_at,omitempty"`
	LockReleasedAt     string                           `json:"lock_released_at,omitempty"`
	Nodes              []NodeLockResult                 `json:"nodes,omitempty"`
}

func cutoverDir(deployPath string) string {
	return filepath.Join(deployPath, "cutover")
}

func positionFilePath(deployPath, key string) string {
	return filepath.Join(cutoverDir(deployPath), key+".json")
}

func newPositionFile(
	p *Params, snapshots map[string]dtsutil.BinlogCoord, items []dtsutil.TaskStatusItem,
) *PositionFile {
	return &PositionFile{
		TaskName:     p.TaskName,
		LockMode:     p.LockMode,
		LockSnapshot: snapshots,
		Sources:      dtsutil.BuildPositionOutput(p.TaskName, items).Sources,
	}
}

// writePositionFileAtomic 临时文件 + fsync + rename，避免半截文件。
func writePositionFileAtomic(path string, pf *PositionFile) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return fmt.Errorf("创建位点目录失败: %w", err)
	}
	raw, err := json.MarshalIndent(pf, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化位点文件失败: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".position-*.tmp")
	if err != nil {
		return fmt.Errorf("创建临时位点文件失败: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		if _, serr := os.Stat(tmpName); serr == nil {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err = tmp.Write(raw); err != nil {
		tmp.Close()
		return fmt.Errorf("写临时位点文件失败: %w", err)
	}
	if err = tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("fsync 临时位点文件失败: %w", err)
	}
	if err = tmp.Close(); err != nil {
		return fmt.Errorf("关闭临时位点文件失败: %w", err)
	}
	if err = os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename 位点文件失败: %w", err)
	}
	logger.Info("位点文件已写入 state=%s path=%s", pf.State, path)
	return nil
}

// readPositionFile 读取位点文件；文件不存在返回 (nil, nil)。
func readPositionFile(path string) (*PositionFile, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("读取位点文件失败: %w", err)
	}
	var pf PositionFile
	if err = json.Unmarshal(raw, &pf); err != nil {
		return nil, fmt.Errorf("解析位点文件失败 %s: %w", path, err)
	}
	return &pf, nil
}

func removePositionFile(path string) error {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	logger.Info("位点文件已删除 path=%s", path)
	return nil
}

func nowUTC() string {
	return time.Now().UTC().Format(time.RFC3339)
}
