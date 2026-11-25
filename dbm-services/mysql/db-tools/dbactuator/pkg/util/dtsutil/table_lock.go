/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package dtsutil

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/native"
)

// Close 关闭连接（unlock 应由调用方先执行）。
func (s *SourceLockConn) Close() {
	if s.LockConn != nil {
		_ = s.LockConn.Close()
		s.LockConn = nil
	}
	if s.Worker != nil {
		s.Worker.Close()
		s.Worker = nil
	}
}

// BuildFlushTablesReadLockSQL 构造单句 FLUSH TABLES t1,t2,... WITH READ LOCK（禁止裸 FTWRL / 假分批）。
func BuildFlushTablesReadLockSQL(tables []LockedTable) (string, error) {
	if len(tables) == 0 {
		return "", fmt.Errorf("加锁表清单为空，禁止执行裸 FLUSH TABLES WITH READ LOCK")
	}
	parts := make([]string, 0, len(tables))
	for _, t := range tables {
		parts = append(parts, fmt.Sprintf("%s.%s", quoteIdent(t.Schema), quoteIdent(t.Table)))
	}
	return "FLUSH TABLES " + strings.Join(parts, ",") + " WITH READ LOCK", nil
}

func quoteIdent(name string) string {
	return "`" + strings.ReplaceAll(name, "`", "``") + "`"
}

// ValidateFlushSQLBudget 校验语句长度相对 max_allowed_packet 的预算。
func ValidateFlushSQLBudget(flushSQL string, maxAllowedPacket int64) error {
	if maxAllowedPacket <= 0 {
		return fmt.Errorf("max_allowed_packet 无效: %d", maxAllowedPacket)
	}
	budget := maxAllowedPacket - PacketMarginBytes
	if budget <= 0 {
		return fmt.Errorf(
			"max_allowed_packet(%d) 过小，无法满足 margin=%d",
			maxAllowedPacket, PacketMarginBytes,
		)
	}
	n := int64(len([]byte(flushSQL)))
	if n >= budget {
		return fmt.Errorf(
			"FLUSH 语句长度 %d 字节超过预算 %d (max_allowed_packet=%d - margin=%d)；请缩小迁移范围或调大 max_allowed_packet；禁止假分批 FLUSH",
			n, budget, maxAllowedPacket, PacketMarginBytes,
		)
	}
	return nil
}

// flushClientSlack FLUSH 客户端超时相对 lock_wait_timeout 的余量，避免服务端已放弃而客户端仍挂起
const flushClientSlack = 5 * time.Second

// sessionSetTimeout 会话变量设置的超时，防止连接异常时卡在 SET 上
const sessionSetTimeout = 5 * time.Second

// CheckFlushSQLBudget 读取实例 max_allowed_packet 并校验 FLUSH 语句长度预算。
func CheckFlushSQLBudget(db *sql.DB, flushSQL string) error {
	var maxAllowedPacket int64
	if err := db.QueryRow("SELECT @@max_allowed_packet").Scan(&maxAllowedPacket); err != nil {
		return fmt.Errorf("读取 max_allowed_packet 失败: %w", err)
	}
	return ValidateFlushSQLBudget(flushSQL, maxAllowedPacket)
}

// FlushReadLock 在专用连接上设置会话 lock_wait_timeout，再执行 FLUSH ... WITH READ LOCK。
// FLUSH 的客户端超时 = lock_wait_timeout + flushClientSlack；FLUSH 本身的错误原样返回，由调用方补充节点信息。
func FlushReadLock(ctx context.Context, conn *sql.Conn, flushSQL string, lockWaitTimeoutSec int) error {
	setCtx, setCancel := context.WithTimeout(ctx, sessionSetTimeout)
	_, err := conn.ExecContext(setCtx, fmt.Sprintf("SET lock_wait_timeout = %d", lockWaitTimeoutSec))
	setCancel()
	if err != nil {
		return fmt.Errorf("设置 lock_wait_timeout 失败: %w", err)
	}
	lockCtx, cancel := context.WithTimeout(ctx, time.Duration(lockWaitTimeoutSec)*time.Second+flushClientSlack)
	defer cancel()
	_, err = conn.ExecContext(lockCtx, flushSQL)
	return err
}

// UnlockTables 在持锁连接上执行 UNLOCK TABLES；conn 为空时直接返回。
func UnlockTables(ctx context.Context, conn *sql.Conn) error {
	if conn == nil {
		return nil
	}
	_, err := conn.ExecContext(ctx, "UNLOCK TABLES")
	return err
}

// LockSourceTables 连接源端、展开（或使用给定表）、单句表级 READ 锁。
func LockSourceTables(ep Endpoint, scope *SyncScope, lockTables []TableItem) (*SourceLockConn, error) {
	inst := native.InsObject{
		Host: ep.Host,
		Port: ep.Port,
		User: ep.User,
		Pwd:  ep.Password,
	}
	worker, err := inst.Conn()
	if err != nil {
		return nil, fmt.Errorf(
			"连接源端 %s:%d 失败（请检查临时账号是否授权 dts-master IP）: %w",
			ep.Host, ep.Port, err,
		)
	}
	sl := &SourceLockConn{Endpoint: ep, Worker: worker}
	cleanupOnErr := true
	defer func() {
		if cleanupOnErr {
			sl.Close()
		}
	}()

	tables, err := ResolveTablesList(worker.Db, scope, lockTables)
	if err != nil {
		return nil, err
	}
	sl.Tables = tables

	flushSQL, err := BuildFlushTablesReadLockSQL(tables)
	if err != nil {
		return nil, err
	}
	if err = CheckFlushSQLBudget(worker.Db, flushSQL); err != nil {
		return nil, err
	}

	ctx := context.Background()
	lockConn, err := worker.Db.Conn(ctx)
	if err != nil {
		return nil, fmt.Errorf("获取持锁连接失败: %w", err)
	}
	sl.LockConn = lockConn

	logger.Info(
		"源端 %s:%d 对 %d 张表执行单句 FLUSH TABLES ... WITH READ LOCK (sql_bytes=%d)",
		ep.Host, ep.Port, len(tables), len(flushSQL),
	)
	if err = FlushReadLock(ctx, lockConn, flushSQL, LockWaitTimeoutSec); err != nil {
		return nil, fmt.Errorf("源端加表读锁失败 %s:%d: %w", ep.Host, ep.Port, err)
	}

	cleanupOnErr = false
	return sl, nil
}

// UnlockSource 释放表锁。
func UnlockSource(sl *SourceLockConn) error {
	if sl == nil || sl.LockConn == nil {
		return nil
	}
	if err := UnlockTables(context.Background(), sl.LockConn); err != nil {
		logger.Error("源端 %s:%d UNLOCK TABLES 失败: %s", sl.Endpoint.Host, sl.Endpoint.Port, err.Error())
		return err
	}
	logger.Info("源端 %s:%d 已 UNLOCK TABLES", sl.Endpoint.Host, sl.Endpoint.Port)
	return nil
}
