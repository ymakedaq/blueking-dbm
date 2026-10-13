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
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
)

func TestVersionSelectsStatements(t *testing.T) {
	cases := []struct {
		version, binlog, replica, logVar, execFile, execPos string
	}{
		{"5.7.20-tmysql-3.4-log", "SHOW MASTER STATUS", "SHOW SLAVE STATUS", "log_slave_updates",
			"Relay_Master_Log_File", "Exec_Master_Log_Pos"},
		{"8.0.32", "SHOW MASTER STATUS", "SHOW SLAVE STATUS", "log_slave_updates",
			"Relay_Master_Log_File", "Exec_Master_Log_Pos"},
		{"8.3.99", "SHOW MASTER STATUS", "SHOW SLAVE STATUS", "log_slave_updates",
			"Relay_Master_Log_File", "Exec_Master_Log_Pos"},
		{"8.4.0", "SHOW BINARY LOG STATUS", "SHOW REPLICA STATUS", "log_replica_updates",
			"Relay_Source_Log_File", "Exec_Source_Log_Pos"},
		{"8.4.0-log", "SHOW BINARY LOG STATUS", "SHOW REPLICA STATUS", "log_replica_updates",
			"Relay_Source_Log_File", "Exec_Source_Log_Pos"},
		{"8.4.3", "SHOW BINARY LOG STATUS", "SHOW REPLICA STATUS", "log_replica_updates",
			"Relay_Source_Log_File", "Exec_Source_Log_Pos"},
	}
	for _, c := range cases {
		require.Equal(t, c.binlog, binlogStatusSQL(c.version), c.version)
		require.Equal(t, c.replica, replicaStatusSQL(c.version), c.version)
		require.Equal(t, c.logVar, logReplicaUpdatesVar(c.version), c.version)
		cols := replicaStatusColumnsFor(c.version)
		require.Equal(t, c.execFile, cols.ExecFile, c.version)
		require.Equal(t, c.execPos, cols.ExecPos, c.version)
	}
}

func TestGtidSubsetQueryBindsGtidAsArg(t *testing.T) {
	gtid := "3e11fa47-71ca-11e1-9e33-c80aa9429562:1-5,\n4e11fa47-71ca-11e1-9e33-c80aa9429562:1-9"
	query, args, err := gtidSubsetQuery(gtid)
	require.NoError(t, err)
	require.Equal(t, "SELECT GTID_SUBSET(?, @@global.gtid_executed)", query)
	require.NotContains(t, query, "3e11fa47")
	require.Equal(t, []any{gtid}, args)
}

func TestGtidSubsetQueryRejectsEmpty(t *testing.T) {
	for _, gtid := range []string{"", "  \n "} {
		_, _, err := gtidSubsetQuery(gtid)
		require.Error(t, err)
	}
}

func TestCompareBinlogCoordStrictFailsOnUnparsableSeq(t *testing.T) {
	ok := BinlogCoord{File: "binlog20000.000010", Position: 100}
	for _, bad := range []string{"binlog", "binlog.abc", "binlog."} {
		_, err := compareBinlogCoordStrict(BinlogCoord{File: bad, Position: 100}, ok)
		require.Error(t, err, bad)
		_, err = compareBinlogCoordStrict(ok, BinlogCoord{File: bad, Position: 100})
		require.Error(t, err, bad)
	}
}

func TestCompareBinlogCoordStrictOrdersBySeqThenPos(t *testing.T) {
	a := BinlogCoord{File: "binlog20000.000009", Position: 900}
	b := BinlogCoord{File: "binlog20001.000010", Position: 4}
	cmp, err := compareBinlogCoordStrict(a, b)
	require.NoError(t, err)
	require.Equal(t, -1, cmp)
	cmp, err = compareBinlogCoordStrict(b, BinlogCoord{File: "binlog20001.000010", Position: 4})
	require.NoError(t, err)
	require.Equal(t, 0, cmp)
	cmp, err = compareBinlogCoordStrict(BinlogCoord{File: "binlog.000010", Position: 5}, b)
	require.NoError(t, err)
	require.Equal(t, 1, cmp)
}

func TestAppliedCoordFailsWhenExecFileUnparsable(t *testing.T) {
	st := replicaStatus{ExecFile: "relay-bin", ExecPos: 1000}
	_, err := st.appliedCoord(BinlogCoord{File: "binlog.000001", Position: 10})
	require.Error(t, err)
}

func oldReplicaRow(host, port, io, sqlRunning string) map[string]string {
	return map[string]string{
		"Master_Host":           host,
		"Master_Port":           port,
		"Slave_IO_Running":      io,
		"Slave_SQL_Running":     sqlRunning,
		"Relay_Master_Log_File": "binlog20000.000003",
		"Exec_Master_Log_Pos":   "500",
		"Master_Log_File":       "binlog20000.000009",
		"Last_SQL_Errno":        "0",
		"Last_SQL_Error":        "",
	}
}

func TestParseReplicaStatusUsesExecColumnsNotIOColumns(t *testing.T) {
	sts, err := parseReplicaStatusRows([]map[string]string{oldReplicaRow("127.0.0.1", "20000", "Yes", "Yes")}, "5.7.20")
	require.NoError(t, err)
	require.Equal(t, "binlog20000.000003", sts[0].ExecFile)
	require.Equal(t, int64(500), sts[0].ExecPos)
	require.Equal(t, 20000, sts[0].SourcePort)
}

func TestParseReplicaStatusNewColumnsAt840(t *testing.T) {
	row := map[string]string{
		"Source_Host":           "127.0.0.1",
		"Source_Port":           "20000",
		"Replica_IO_Running":    "Yes",
		"Replica_SQL_Running":   "Yes",
		"Relay_Source_Log_File": "binlog.000002",
		"Exec_Source_Log_Pos":   "77",
		"Last_SQL_Errno":        "0",
		"Last_SQL_Error":        "",
	}
	sts, err := parseReplicaStatusRows([]map[string]string{row}, "8.4.0")
	require.NoError(t, err)
	require.Equal(t, "binlog.000002", sts[0].ExecFile)
	_, err = parseReplicaStatusRows([]map[string]string{row}, "8.0.32")
	require.Error(t, err)
}

func TestPickHealthyUpstream(t *testing.T) {
	parse := func(rows ...map[string]string) []replicaStatus {
		sts, err := parseReplicaStatusRows(rows, "5.7.20")
		require.NoError(t, err)
		return sts
	}
	_, err := pickHealthyUpstream(parse(oldReplicaRow("127.0.0.1", "20000", "Yes", "Yes")), "127.0.0.1", 20000)
	require.NoError(t, err)

	_, err = pickHealthyUpstream(nil, "127.0.0.1", 20000)
	require.Error(t, err)

	_, err = pickHealthyUpstream(parse(oldReplicaRow("127.0.0.1", "20001", "Yes", "Yes")), "127.0.0.1", 20000)
	require.ErrorContains(t, err, "上游不是主库")

	_, err = pickHealthyUpstream(parse(oldReplicaRow("127.0.0.1", "20000", "Connecting", "Yes")), "127.0.0.1", 20000)
	require.ErrorContains(t, err, "复制线程未运行")

	errRow := oldReplicaRow("127.0.0.1", "20000", "Yes", "Yes")
	errRow["Last_SQL_Errno"] = "1062"
	errRow["Last_SQL_Error"] = "Duplicate entry"
	_, err = pickHealthyUpstream(parse(errRow), "127.0.0.1", 20000)
	require.ErrorContains(t, err, "SQL 线程报错")

	st, err := pickHealthyUpstream(parse(
		oldReplicaRow("127.0.0.9", "20000", "No", "No"),
		oldReplicaRow("127.0.0.1", "20000", "Yes", "Yes"),
	), "127.0.0.1", 20000)
	require.NoError(t, err)
	require.Equal(t, "127.0.0.1", st.SourceHost)
}

func TestParseBinlogStatusRows(t *testing.T) {
	bs, err := parseBinlogStatusRows([]map[string]string{{
		"File": "binlog20000.000011", "Position": "4096", "Executed_Gtid_Set": " uuid:1-3,\nuuid2:1-2 ",
	}})
	require.NoError(t, err)
	require.Equal(t, BinlogCoord{File: "binlog20000.000011", Position: 4096}, bs.Coord)
	require.Equal(t, "uuid:1-3,\nuuid2:1-2", bs.GtidSet)

	_, err = parseBinlogStatusRows(nil)
	require.Error(t, err)
	_, err = parseBinlogStatusRows([]map[string]string{{"File": "binlog.000001", "Position": "x"}})
	require.Error(t, err)
}

func TestPollReplicaApplied(t *testing.T) {
	calls := 0
	err := pollReplicaApplied(func() (bool, error) {
		calls++
		return calls == 3, nil
	}, 60, func() {})
	require.NoError(t, err)
	require.Equal(t, 3, calls)

	calls = 0
	err = pollReplicaApplied(func() (bool, error) {
		calls++
		return false, errors.New("io=No")
	}, 60, func() { t.Fatal("出错后不应继续等待") })
	require.Equal(t, 1, calls)
	require.ErrorContains(t, err, "不执行 stop")

	calls = 0
	err = pollReplicaApplied(func() (bool, error) {
		calls++
		return false, nil
	}, 4, func() {})
	require.Equal(t, 4, calls)
	require.ErrorContains(t, err, "超时")
}

func TestReplicaApplyBudgetIndependentOfCatchup(t *testing.T) {
	require.Equal(t, 60, defaultReplicaApplyPollMax)
	require.Equal(t, catchupPollInterval, replicaApplyPollInterval)
}

func TestValidateReplicaEndpointRejectsSameInstance(t *testing.T) {
	ep := SourceEndpoint{Host: "127.0.0.1", Port: 20000, Replica: &ReplicaEndpoint{Host: "127.0.0.1", Port: 20000}}
	require.Error(t, validateReplicaEndpoint(ep))
	ep.Replica.Port = 20001
	require.NoError(t, validateReplicaEndpoint(ep))
	ep.Replica.Host = ""
	require.Error(t, validateReplicaEndpoint(ep))
}

func statusItem(source, master, syncer string) TaskStatusItem {
	return TaskStatusItem{
		SourceName: source,
		Stage:      TaskStageRunning,
		SyncStatus: &SyncStatus{MasterBinlog: master, SyncerBinlog: syncer},
	}
}

func TestBuildLockSnapshotsWithoutReplicaUsesDtsMasterBinlog(t *testing.T) {
	eps := []SourceEndpoint{{Host: "127.0.0.1", Port: 20000, SourceName: "src1"}}
	items := []TaskStatusItem{statusItem("src1", "(binlog.000005, 300)", "(binlog.000005, 100)")}
	snaps, err := buildLockSnapshots(eps, items, map[int]BinlogCoord{})
	require.NoError(t, err)
	require.Equal(t, BinlogCoord{File: "binlog.000005", Position: 300}, snaps["src1"])
}

func TestBuildLockSnapshotsReplicaUsesReplicaPosition(t *testing.T) {
	eps := []SourceEndpoint{{
		Host: "127.0.0.1", Port: 20000, SourceName: "src1",
		Replica: &ReplicaEndpoint{Host: "127.0.0.2", Port: 20000},
	}}
	items := []TaskStatusItem{statusItem("src1", "(binlog.000001, 10)", "(binlog.000001, 5)")}
	s := BinlogCoord{File: "binlog20000.000007", Position: 900}
	snaps, err := buildLockSnapshots(eps, items, map[int]BinlogCoord{0: s})
	require.NoError(t, err)
	require.Equal(t, s, snaps["src1"])
}

func TestBuildLockSnapshotsReplicaWithoutSourceNameNeedsSingleItem(t *testing.T) {
	eps := []SourceEndpoint{{Host: "127.0.0.1", Port: 20000, Replica: &ReplicaEndpoint{Host: "127.0.0.2", Port: 20000}}}
	s := BinlogCoord{File: "binlog.000002", Position: 1}
	snaps, err := buildLockSnapshots(eps, []TaskStatusItem{statusItem("only", "", "")}, map[int]BinlogCoord{0: s})
	require.NoError(t, err)
	require.Equal(t, s, snaps["only"])

	_, err = buildLockSnapshots(eps, []TaskStatusItem{statusItem("a", "", ""), statusItem("b", "", "")},
		map[int]BinlogCoord{0: s})
	require.Error(t, err)
}

func TestSourceEndpointReplicaIsOptional(t *testing.T) {
	var ep SourceEndpoint
	require.NoError(t, json.Unmarshal([]byte(`{"host":"127.0.0.1","port":20000,"user":"u","password":"p"}`), &ep))
	require.Nil(t, ep.Replica)

	require.NoError(t, json.Unmarshal(
		[]byte(`{"host":"127.0.0.1","port":20000,"user":"u","password":"p","replica":{"host":"127.0.0.2","port":20001}}`),
		&ep,
	))
	require.Equal(t, &ReplicaEndpoint{Host: "127.0.0.2", Port: 20001}, ep.Replica)
}

func TestCatchUpQueryDeadlineOnMasterStatus(t *testing.T) {
	db := openScriptDB(t, []queryStep{{hang: true}})
	conn := scriptConnOf(t, db)
	sl := lockOn(conn)
	err := catchUpWithShortQueryTimeout(t, sl, nil)
	require.ErrorIs(t, err, context.DeadlineExceeded)
	require.ErrorContains(t, err, "持锁连接读取主库 127.0.0.1:20000 位点失败（不执行 stop）")
}

func TestCatchUpQueryDeadlineOnReplicaStatus(t *testing.T) {
	masterDB := openScriptDB(t, []queryStep{binlogStep("binlog20000.000010", "100", "")})
	replicaDB := openScriptDB(t, []queryStep{{hang: true}})
	sl := lockOn(scriptConnOf(t, masterDB))
	rc := replicaOn(replicaDB)
	err := catchUpWithShortQueryTimeout(t, sl, rc)
	require.ErrorIs(t, err, context.DeadlineExceeded)
	require.ErrorContains(t, err, "查询从库 127.0.0.2:20000 复制状态失败")
	require.ErrorContains(t, err, "不执行 stop")
}

func TestCatchUpQueryDeadlineOnGtidSubset(t *testing.T) {
	masterDB := openScriptDB(t, []queryStep{binlogStep("binlog20000.000010", "100", "3e11fa47-71ca-11e1-9e33-c80aa9429562:1-5")})
	replicaDB := openScriptDB(t, []queryStep{replicaStatusStep(), {hang: true}})
	sl := lockOn(scriptConnOf(t, masterDB))
	rc := replicaOn(replicaDB)
	err := catchUpWithShortQueryTimeout(t, sl, rc)
	require.ErrorIs(t, err, context.DeadlineExceeded)
	require.ErrorContains(t, err, "GTID_SUBSET")
	require.ErrorContains(t, err, "不执行 stop")
}

func TestCatchUpQueryDeadlineOnReplicaBinlog(t *testing.T) {
	masterDB := openScriptDB(t, []queryStep{binlogStep("binlog20000.000010", "100", "")})
	replicaDB := openScriptDB(t, []queryStep{replicaStatusStep(), {hang: true}})
	sl := lockOn(scriptConnOf(t, masterDB))
	rc := replicaOn(replicaDB)
	err := catchUpWithShortQueryTimeout(t, sl, rc)
	require.ErrorIs(t, err, context.DeadlineExceeded)
	require.ErrorContains(t, err, "读取从库 127.0.0.2:20000 位点失败（不执行 stop）")
}

func catchUpWithShortQueryTimeout(t *testing.T, sl *SourceLockConn, rc *replicaConn) error {
	t.Helper()
	prev := replicaQueryTimeout
	replicaQueryTimeout = 80 * time.Millisecond
	t.Cleanup(func() { replicaQueryTimeout = prev })
	start := time.Now()
	_, err := catchUpReplicaToLockedMaster(sl, rc, 60, func() {
		t.Fatal("询问未返回时不应进入下一轮等待")
	})
	require.Less(t, time.Since(start), 2*time.Second)
	return err
}

func lockOn(conn *sql.Conn) *SourceLockConn {
	return &SourceLockConn{
		Endpoint: SourceEndpoint{Host: "127.0.0.1", Port: 20000},
		Worker:   &native.DbWorker{ServerVersion: "8.0.32"},
		LockConn: conn,
	}
}

func replicaOn(db *sql.DB) *replicaConn {
	return &replicaConn{
		Replica: ReplicaEndpoint{Host: "127.0.0.2", Port: 20000},
		Worker:  &native.DbWorker{Db: db, ServerVersion: "8.0.32"},
	}
}

func binlogStep(file, pos, gtid string) queryStep {
	return queryStep{
		cols: []string{"File", "Position", "Executed_Gtid_Set"},
		vals: []driver.Value{file, pos, gtid},
	}
}

func replicaStatusStep() queryStep {
	return queryStep{
		cols: []string{
			"Master_Host", "Master_Port", "Slave_IO_Running", "Slave_SQL_Running",
			"Relay_Master_Log_File", "Exec_Master_Log_Pos", "Last_SQL_Errno", "Last_SQL_Error",
		},
		vals: []driver.Value{"127.0.0.1", "20000", "Yes", "Yes", "binlog20000.000010", "100", "0", ""},
	}
}

type queryStep struct {
	hang bool
	cols []string
	vals []driver.Value
}

type queryScript struct {
	mu    sync.Mutex
	steps []queryStep
	n     int
}

func (s *queryScript) query(ctx context.Context) (driver.Rows, error) {
	s.mu.Lock()
	step := queryStep{hang: true}
	if s.n < len(s.steps) {
		step = s.steps[s.n]
		s.n++
	}
	s.mu.Unlock()
	if step.hang {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return &staticRows{cols: step.cols, vals: step.vals}, nil
}

type staticRows struct {
	cols []string
	vals []driver.Value
	done bool
}

func (r *staticRows) Columns() []string { return r.cols }
func (r *staticRows) Close() error      { return nil }
func (r *staticRows) Next(dest []driver.Value) error {
	if r.done {
		return io.EOF
	}
	r.done = true
	for i := range dest {
		dest[i] = r.vals[i]
	}
	return nil
}

type scriptConn struct{ script *queryScript }

func (c *scriptConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("prepare unsupported")
}
func (c *scriptConn) Close() error { return nil }
func (c *scriptConn) Begin() (driver.Tx, error) {
	return nil, errors.New("tx unsupported")
}
func (c *scriptConn) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	return c.script.query(ctx)
}

type scriptDriver struct{}

func (scriptDriver) Open(name string) (driver.Conn, error) {
	v, ok := scriptStore.Load(name)
	if !ok {
		return nil, errors.New("missing script")
	}
	return &scriptConn{script: v.(*queryScript)}, nil
}

var scriptStore sync.Map
var scriptSeq atomic.Int64

func init() {
	sql.Register("dts-cutover-script", scriptDriver{})
}

func openScriptDB(t *testing.T, steps []queryStep) *sql.DB {
	t.Helper()
	name := strconv.FormatInt(scriptSeq.Add(1), 10)
	scriptStore.Store(name, &queryScript{steps: steps})
	db, err := sql.Open("dts-cutover-script", name)
	require.NoError(t, err)
	t.Cleanup(func() {
		db.Close()
		scriptStore.Delete(name)
	})
	return db
}

func scriptConnOf(t *testing.T, db *sql.DB) *sql.Conn {
	t.Helper()
	conn, err := db.Conn(context.Background())
	require.NoError(t, err)
	t.Cleanup(func() { conn.Close() })
	return conn
}
