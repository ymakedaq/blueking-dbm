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
	"context"
	"database/sql"
	"fmt"
	"strconv"
	"strings"
	"time"

	"dbm-services/common/go-pubpkg/cmutil"
	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
)

const (
	defaultReplicaApplyPollMax = 60
	replicaApplyPollInterval   = 500 * time.Millisecond
)

// replicaQueryTimeout 持锁之后单次询问的截止时间。
// 轮询次数只统计已经返回的回答，一次不返回的询问不会消耗次数，表锁也不会放开。
var replicaQueryTimeout = 5 * time.Second

func withReplicaQuery[T any](fn func(context.Context) (T, error)) (T, error) {
	ctx, cancel := context.WithTimeout(context.Background(), replicaQueryTimeout)
	defer cancel()
	return fn(ctx)
}

// ReplicaEndpoint DTS 实际连接的从库。账号沿用所属 SourceEndpoint 的临时账号。
type ReplicaEndpoint struct {
	Host string `json:"host" validate:"required"`
	Port int    `json:"port" validate:"required,gt=0"`
}

// isMySQL84OrLater DbWorker 用的是 > 8.4.0，正好 8.4.0 会发出已删除的旧语句，这里必须是 >=。
func isMySQL84OrLater(version string) bool {
	return cmutil.MySQLVersionCompare(version, "8.4.0") >= 0
}

func binlogStatusSQL(version string) string {
	if isMySQL84OrLater(version) {
		return "SHOW BINARY LOG STATUS"
	}
	return "SHOW MASTER STATUS"
}

func replicaStatusSQL(version string) string {
	if isMySQL84OrLater(version) {
		return "SHOW REPLICA STATUS"
	}
	return "SHOW SLAVE STATUS"
}

func logReplicaUpdatesVar(version string) string {
	if isMySQL84OrLater(version) {
		return "log_replica_updates"
	}
	return "log_slave_updates"
}

type replicaStatusColumns struct {
	SourceHost  string
	SourcePort  string
	IORunning   string
	SQLRunning  string
	ExecFile    string
	ExecPos     string
	LastSQLErrn string
	LastSQLErr  string
}

func replicaStatusColumnsFor(version string) replicaStatusColumns {
	if isMySQL84OrLater(version) {
		return replicaStatusColumns{
			SourceHost:  "Source_Host",
			SourcePort:  "Source_Port",
			IORunning:   "Replica_IO_Running",
			SQLRunning:  "Replica_SQL_Running",
			ExecFile:    "Relay_Source_Log_File",
			ExecPos:     "Exec_Source_Log_Pos",
			LastSQLErrn: "Last_SQL_Errno",
			LastSQLErr:  "Last_SQL_Error",
		}
	}
	return replicaStatusColumns{
		SourceHost:  "Master_Host",
		SourcePort:  "Master_Port",
		IORunning:   "Slave_IO_Running",
		SQLRunning:  "Slave_SQL_Running",
		ExecFile:    "Relay_Master_Log_File",
		ExecPos:     "Exec_Master_Log_Pos",
		LastSQLErrn: "Last_SQL_Errno",
		LastSQLErr:  "Last_SQL_Error",
	}
}

// binlogStatus SHOW MASTER STATUS / SHOW BINARY LOG STATUS 的一行。
type binlogStatus struct {
	Coord   BinlogCoord
	GtidSet string
}

// replicaStatus 复制状态中与一个上游对应的一行。
type replicaStatus struct {
	SourceHost  string
	SourcePort  int
	IORunning   string
	SQLRunning  string
	ExecFile    string
	ExecPos     int64
	LastSQLErrn string
	LastSQLErr  string
}

type rowQueryer interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

func queryStringRows(ctx context.Context, q rowQueryer, query string) ([]map[string]string, error) {
	rows, err := q.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	var out []map[string]string
	for rows.Next() {
		vals := make([]sql.NullString, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err = rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		row := make(map[string]string, len(cols))
		for i, c := range cols {
			row[c] = vals[i].String
		}
		out = append(out, row)
	}
	return out, rows.Err()
}

func requiredColumn(row map[string]string, name string) (string, error) {
	v, ok := row[name]
	if !ok {
		return "", fmt.Errorf("结果缺少列 %s", name)
	}
	return strings.TrimSpace(v), nil
}

func parseBinlogStatusRows(rows []map[string]string) (binlogStatus, error) {
	if len(rows) != 1 {
		return binlogStatus{}, fmt.Errorf("binlog 状态应返回 1 行，实际 %d 行（log_bin 可能未开启）", len(rows))
	}
	row := rows[0]
	file, err := requiredColumn(row, "File")
	if err != nil {
		return binlogStatus{}, err
	}
	posText, err := requiredColumn(row, "Position")
	if err != nil {
		return binlogStatus{}, err
	}
	pos, err := strconv.ParseInt(posText, 10, 64)
	if err != nil || pos < 0 || file == "" {
		return binlogStatus{}, fmt.Errorf("binlog 位点非法: file=%q position=%q", file, posText)
	}
	return binlogStatus{
		Coord:   BinlogCoord{File: file, Position: pos},
		GtidSet: strings.TrimSpace(row["Executed_Gtid_Set"]),
	}, nil
}

func parseReplicaStatusRows(rows []map[string]string, version string) ([]replicaStatus, error) {
	cols := replicaStatusColumnsFor(version)
	out := make([]replicaStatus, 0, len(rows))
	for _, row := range rows {
		var st replicaStatus
		var portText, posText string
		for _, f := range []struct {
			name string
			dst  *string
		}{
			{cols.SourceHost, &st.SourceHost},
			{cols.SourcePort, &portText},
			{cols.IORunning, &st.IORunning},
			{cols.SQLRunning, &st.SQLRunning},
			{cols.ExecFile, &st.ExecFile},
			{cols.ExecPos, &posText},
			{cols.LastSQLErrn, &st.LastSQLErrn},
			{cols.LastSQLErr, &st.LastSQLErr},
		} {
			v, err := requiredColumn(row, f.name)
			if err != nil {
				return nil, err
			}
			*f.dst = v
		}
		port, err := strconv.Atoi(portText)
		if err != nil {
			return nil, fmt.Errorf("复制状态 %s 非法: %q", cols.SourcePort, portText)
		}
		st.SourcePort = port
		pos, err := strconv.ParseInt(posText, 10, 64)
		if err != nil || pos < 0 {
			return nil, fmt.Errorf("复制状态 %s 非法: %q", cols.ExecPos, posText)
		}
		st.ExecPos = pos
		out = append(out, st)
	}
	return out, nil
}

// pickHealthyUpstream 找到上游是 master 的那一行，并要求复制线程都在跑、没有 SQL 错误。
func pickHealthyUpstream(sts []replicaStatus, masterHost string, masterPort int) (replicaStatus, error) {
	if len(sts) == 0 {
		return replicaStatus{}, fmt.Errorf("从库没有复制状态")
	}
	for _, st := range sts {
		if st.SourceHost != strings.TrimSpace(masterHost) || st.SourcePort != masterPort {
			continue
		}
		if st.IORunning != "Yes" || st.SQLRunning != "Yes" {
			return replicaStatus{}, fmt.Errorf("从库复制线程未运行: io=%s sql=%s", st.IORunning, st.SQLRunning)
		}
		if (st.LastSQLErrn != "" && st.LastSQLErrn != "0") || st.LastSQLErr != "" {
			return replicaStatus{}, fmt.Errorf("从库 SQL 线程报错: errno=%s error=%s", st.LastSQLErrn, st.LastSQLErr)
		}
		return st, nil
	}
	return replicaStatus{}, fmt.Errorf("从库上游不是主库 %s:%d", masterHost, masterPort)
}

// appliedCoord GTID 为空时比较 SQL 线程已执行到的主库位点。
func (st replicaStatus) appliedCoord(target BinlogCoord) (bool, error) {
	cmp, err := compareBinlogCoordStrict(BinlogCoord{File: st.ExecFile, Position: st.ExecPos}, target)
	if err != nil {
		return false, err
	}
	return cmp >= 0, nil
}

// gtidSubsetQuery GTID 集合带换行，只能走绑定参数。空集合是任何集合的子集，不允许拿去查。
func gtidSubsetQuery(gtidSet string) (string, []any, error) {
	gtidSet = strings.TrimSpace(gtidSet)
	if gtidSet == "" {
		return "", nil, fmt.Errorf("GTID 集合为空，禁止做 GTID_SUBSET")
	}
	return "SELECT GTID_SUBSET(?, @@global.gtid_executed)", []any{gtidSet}, nil
}

func replicaHasGtidSet(ctx context.Context, db *sql.DB, gtidSet string) (bool, error) {
	query, args, err := gtidSubsetQuery(gtidSet)
	if err != nil {
		return false, err
	}
	var subset sql.NullInt64
	if err = db.QueryRowContext(ctx, query, args...).Scan(&subset); err != nil {
		return false, fmt.Errorf("从库 GTID_SUBSET 查询失败: %w", err)
	}
	return subset.Valid && subset.Int64 == 1, nil
}

// pollReplicaApplied check 报错立即失败；返回 true 即通过；次数用完则超时。
func pollReplicaApplied(check func() (bool, error), pollMax int, sleep func()) error {
	if pollMax < 1 {
		pollMax = 1
	}
	for attempt := 0; attempt < pollMax; attempt++ {
		if attempt > 0 && sleep != nil {
			sleep()
		}
		done, err := check()
		if err != nil {
			return fmt.Errorf("等待从库执行到主库锁表位置失败（不执行 stop，将 unlock）: %w", err)
		}
		if done {
			logger.Info("从库已执行到主库锁表时的位置 attempt=%d/%d", attempt+1, pollMax)
			return nil
		}
		logger.Warn("从库还没执行到主库锁表时的位置 attempt=%d/%d", attempt+1, pollMax)
	}
	return fmt.Errorf("等待从库执行到主库锁表位置超时（不执行 stop，将 unlock）: attempts=%d", pollMax)
}

type replicaConn struct {
	Replica ReplicaEndpoint
	Worker  *native.DbWorker
}

func (r *replicaConn) Close() {
	if r != nil && r.Worker != nil {
		r.Worker.Close()
		r.Worker = nil
	}
}

func (r *replicaConn) label() string {
	return fmt.Sprintf("%s:%d", r.Replica.Host, r.Replica.Port)
}

func (r *replicaConn) upstreamStatus(ctx context.Context, master SourceEndpoint) (replicaStatus, error) {
	ver := r.Worker.ServerVersion
	rows, err := queryStringRows(ctx, r.Worker.Db, replicaStatusSQL(ver))
	if err != nil {
		return replicaStatus{}, fmt.Errorf("查询从库 %s 复制状态失败: %w", r.label(), err)
	}
	sts, err := parseReplicaStatusRows(rows, ver)
	if err != nil {
		return replicaStatus{}, fmt.Errorf("解析从库 %s 复制状态失败: %w", r.label(), err)
	}
	st, err := pickHealthyUpstream(sts, master.Host, master.Port)
	if err != nil {
		return replicaStatus{}, fmt.Errorf("从库 %s: %w", r.label(), err)
	}
	return st, nil
}

func validateReplicaEndpoint(ep SourceEndpoint) error {
	r := ep.Replica
	if strings.TrimSpace(r.Host) == "" || r.Port <= 0 {
		return fmt.Errorf("source %s:%d 的 replica 地址不完整: %s:%d", ep.Host, ep.Port, r.Host, r.Port)
	}
	if strings.TrimSpace(r.Host) == strings.TrimSpace(ep.Host) && r.Port == ep.Port {
		return fmt.Errorf("replica 与主库是同一实例 %s:%d", ep.Host, ep.Port)
	}
	return nil
}

// openReplicaBeforeLock 加锁前连从库，确认它正在复制这台主库，并且会把事件写进自己的 binlog。任一项不满足都不加锁。
func openReplicaBeforeLock(ep SourceEndpoint) (*replicaConn, error) {
	if err := validateReplicaEndpoint(ep); err != nil {
		return nil, err
	}
	inst := native.InsObject{Host: ep.Replica.Host, Port: ep.Replica.Port, User: ep.User, Pwd: ep.Password}
	worker, err := inst.Conn()
	if err != nil {
		return nil, fmt.Errorf("连接从库 %s:%d 失败（未加锁）: %w", ep.Replica.Host, ep.Replica.Port, err)
	}
	rc := &replicaConn{Replica: *ep.Replica, Worker: worker}
	ctx := context.Background()
	if _, err = rc.upstreamStatus(ctx, ep); err != nil {
		rc.Close()
		return nil, fmt.Errorf("%w（未加锁）", err)
	}
	varName := logReplicaUpdatesVar(worker.ServerVersion)
	var on string
	if err = worker.Db.QueryRowContext(ctx, "SELECT @@global."+varName).Scan(&on); err != nil {
		rc.Close()
		return nil, fmt.Errorf("读取从库 %s %s 失败（未加锁）: %w", rc.label(), varName, err)
	}
	if strings.TrimSpace(on) != "1" {
		rc.Close()
		return nil, fmt.Errorf("从库 %s %s=%s，必须为 1（未加锁）", rc.label(), varName, on)
	}
	logger.Info(
		"从库 %s 复制正常 version=%s %s=%s 上游 %s:%d",
		rc.label(), worker.ServerVersion, varName, strings.TrimSpace(on), ep.Host, ep.Port,
	)
	return rc, nil
}

func readBinlogStatus(ctx context.Context, q rowQueryer, version string) (binlogStatus, error) {
	rows, err := queryStringRows(ctx, q, binlogStatusSQL(version))
	if err != nil {
		return binlogStatus{}, err
	}
	return parseBinlogStatusRows(rows)
}

// catchUpReplicaToLockedMaster 在主库锁未释放时取主库锁点 M，等从库应用 M，再返回从库自己的位点 S。
func catchUpReplicaToLockedMaster(sl *SourceLockConn, rc *replicaConn, pollMax int, sleep func()) (BinlogCoord, error) {
	ep := sl.Endpoint
	m, err := withReplicaQuery(func(ctx context.Context) (binlogStatus, error) {
		return readBinlogStatus(ctx, sl.LockConn, sl.Worker.ServerVersion)
	})
	if err != nil {
		return BinlogCoord{}, fmt.Errorf("持锁连接读取主库 %s:%d 位点失败（不执行 stop）: %w", ep.Host, ep.Port, err)
	}
	waitBy := "用 binlog 文件和位点判断从库是否已经执行到这里"
	if m.GtidSet != "" {
		waitBy = "用 GTID 判断从库是否已经执行到这里"
	}
	logger.Info(
		"主库 %s:%d 已锁表，binlog file=%s pos=%d gtid=%q。等从库 %s。%s",
		ep.Host, ep.Port, m.Coord.File, m.Coord.Position, m.GtidSet, rc.label(), waitBy,
	)

	err = pollReplicaApplied(func() (bool, error) {
		st, serr := withReplicaQuery(func(ctx context.Context) (replicaStatus, error) {
			return rc.upstreamStatus(ctx, ep)
		})
		if serr != nil {
			return false, serr
		}
		if m.GtidSet != "" {
			done, gerr := withReplicaQuery(func(ctx context.Context) (bool, error) {
				return replicaHasGtidSet(ctx, rc.Worker.Db, m.GtidSet)
			})
			if gerr != nil {
				return false, gerr
			}
			if !done {
				logger.Info(
					"从库 %s 的 GTID 还没包含主库锁表时的事务 io=%s sql=%s 当前 binlog file=%s pos=%d",
					rc.label(), st.IORunning, st.SQLRunning, st.ExecFile, st.ExecPos,
				)
			}
			return done, nil
		}
		done, aerr := st.appliedCoord(m.Coord)
		if aerr != nil {
			return false, aerr
		}
		if !done {
			logger.Info(
				"从库 %s 还没执行到主库锁表位置 io=%s sql=%s 从库 file=%s pos=%d 主库 file=%s pos=%d",
				rc.label(), st.IORunning, st.SQLRunning, st.ExecFile, st.ExecPos, m.Coord.File, m.Coord.Position,
			)
		}
		return done, nil
	}, pollMax, sleep)
	if err != nil {
		return BinlogCoord{}, err
	}

	s, err := withReplicaQuery(func(ctx context.Context) (binlogStatus, error) {
		return readBinlogStatus(ctx, rc.Worker.Db, rc.Worker.ServerVersion)
	})
	if err != nil {
		return BinlogCoord{}, fmt.Errorf("读取从库 %s 位点失败（不执行 stop）: %w", rc.label(), err)
	}
	logger.Info("从库 %s 自己的 binlog file=%s pos=%d，切换位点用这个", rc.label(), s.Coord.File, s.Coord.Position)
	return s.Coord, nil
}

// replicaSnapshotKey 从库位点 S 对应的 status source 键。
func replicaSnapshotKey(ep SourceEndpoint, items []TaskStatusItem) (string, error) {
	if name := strings.TrimSpace(ep.SourceName); name != "" {
		for _, item := range items {
			if statusSourceKey(item) == name {
				return name, nil
			}
		}
		return "", fmt.Errorf("任务状态中没有 source %s", name)
	}
	if len(items) == 1 {
		return statusSourceKey(items[0]), nil
	}
	return "", fmt.Errorf("source %s:%d 带 replica 但未给 source_name，无法对应任务状态", ep.Host, ep.Port)
}

// buildLockSnapshots 带 replica 的源用从库位点 S，其余源用加锁后 DTS status 的 master_binlog。
// replicaSnaps 以 endpoints 下标为键。
func buildLockSnapshots(
	endpoints []SourceEndpoint,
	items []TaskStatusItem,
	replicaSnaps map[int]BinlogCoord,
) (map[string]BinlogCoord, error) {
	if len(replicaSnaps) == 0 {
		return BuildLockMasterSnapshots(items)
	}
	if len(items) == 0 {
		return nil, fmt.Errorf("任务状态为空，无法建立加锁位点快照")
	}
	byKey := make(map[string]BinlogCoord, len(replicaSnaps))
	for i, coord := range replicaSnaps {
		key, err := replicaSnapshotKey(endpoints[i], items)
		if err != nil {
			return nil, err
		}
		byKey[key] = coord
	}
	out := make(map[string]BinlogCoord, len(items))
	for _, item := range items {
		key := statusSourceKey(item)
		if coord, ok := byKey[key]; ok {
			logger.Info("source=%s 的切换位点取自从库自己的 binlog file=%s pos=%d", key, coord.File, coord.Position)
			out[key] = coord
			continue
		}
		one, err := BuildLockMasterSnapshots([]TaskStatusItem{item})
		if err != nil {
			return nil, err
		}
		logger.Info("source=%s 的切换位点取自 DTS 上报的主库 binlog file=%s pos=%d", key, one[key].File, one[key].Position)
		out[key] = one[key]
	}
	return out, nil
}
