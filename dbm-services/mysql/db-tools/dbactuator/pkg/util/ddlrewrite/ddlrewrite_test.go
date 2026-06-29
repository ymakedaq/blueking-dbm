/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package ddlrewrite_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"dbm-services/mysql/db-tools/dbactuator/pkg/util/ddlrewrite"
)

func TestAddCreateTableIfNotExists(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    string
		wantErr string
		contain []string
	}{
		{
			name:    "plain create table",
			input:   "CREATE TABLE t1 (id INT PRIMARY KEY) ENGINE=InnoDB",
			want:    "CREATE TABLE IF NOT EXISTS t1 (id INT PRIMARY KEY) ENGINE=InnoDB",
			contain: []string{"create table", "if not exists", "t1", "id", "engine=inno"},
		},
		{
			name:  "preserve table options",
			input: "CREATE TABLE t1 (id INT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
			want:  "CREATE TABLE IF NOT EXISTS t1 (id INT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci",
		},
		{
			name:  "leading line comment",
			input: "-- dump\nCREATE TABLE t1 (id INT) ENGINE=InnoDB",
			want:  "-- dump\nCREATE TABLE IF NOT EXISTS t1 (id INT) ENGINE=InnoDB",
		},
		{
			name:    "already has if not exists",
			input:   "CREATE TABLE IF NOT EXISTS t1 (id INT PRIMARY KEY)",
			contain: []string{"create table", "if not exists", "t1"},
		},
		{
			name:    "create table like",
			input:   "CREATE TABLE t2 LIKE t1",
			contain: []string{"create table", "if not exists", "t2", "like", "t1"},
		},
		{
			name:    "temporary table",
			input:   "CREATE TEMPORARY TABLE t3 (id INT)",
			contain: []string{"create", "temporary", "table", "if not exists", "t3"},
		},
		{
			name:    "qualified table name",
			input:   "CREATE TABLE `db`.`t` (id INT)",
			contain: []string{"create table", "if not exists", "db", "t", "id"},
		},
		{
			name:    "partitioned table",
			input:   "CREATE TABLE t (id INT) PARTITION BY HASH(id) PARTITIONS 3",
			contain: []string{"create table", "if not exists", "t", "partition by hash", "partitions 3"},
		},
		{
			name:    "empty statement",
			input:   "",
			wantErr: "empty statement",
		},
		{
			name:    "not create table",
			input:   "ALTER TABLE t ADD c1 INT",
			wantErr: "statement is not a CREATE TABLE",
		},
		{
			name:    "invalid syntax",
			input:   "CREATE TABLE (",
			wantErr: "failed to parse statement",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := ddlrewrite.AddCreateTableIfNotExists(tt.input)
			if tt.wantErr != "" {
				require.Error(t, err)
				require.Contains(t, err.Error(), tt.wantErr)
				return
			}
			require.NoError(t, err)
			if tt.want != "" {
				require.Equal(t, tt.want, got, "output should preserve original SQL formatting")
			}
			lower := strings.ToLower(got)
			for _, part := range tt.contain {
				require.Contains(t, lower, strings.ToLower(part), "output: %s", got)
			}
			require.Equal(t, 1, strings.Count(lower, "if not exists"))
		})
	}
}

func TestRewriteCreateTableIfNotExistsInFile(t *testing.T) {
	inputPath := filepath.Join("testdata", "create_tables_input.sql")
	expectedPath := filepath.Join("testdata", "create_tables_output.sql")
	outputPath := filepath.Join(t.TempDir(), "create_tables_output.sql")

	err := ddlrewrite.RewriteCreateTableIfNotExistsInFile(inputPath, outputPath)
	require.NoError(t, err)

	got, err := os.ReadFile(outputPath)
	require.NoError(t, err)

	expected, err := os.ReadFile(expectedPath)
	require.NoError(t, err)
	require.Equal(t, string(expected), string(got))
}

func TestIsCreateTable(t *testing.T) {
	require.True(t, ddlrewrite.IsCreateTable("CREATE TABLE t1 (id INT)"))
	require.True(t, ddlrewrite.IsCreateTable("CREATE TABLE IF NOT EXISTS t1 (id INT)"))
	require.True(t, ddlrewrite.IsCreateTable("CREATE TEMPORARY TABLE t1 (id INT)"))
	require.True(t, ddlrewrite.IsCreateTable("CREATE TABLE t2 LIKE t1"))
	require.False(t, ddlrewrite.IsCreateTable("ALTER TABLE t1 ADD c1 INT"))
	require.False(t, ddlrewrite.IsCreateTable(""))
	require.False(t, ddlrewrite.IsCreateTable("CREATE TABLE ("))
}
