/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package mysql

import (
	"strings"
	"testing"
)

func TestDisjointDumpWarning(t *testing.T) {
	cases := []struct {
		name    string
		formDbs []string
		sqlDbs  []string
		want    bool
	}{
		{name: "disjoint form and sql", formDbs: []string{"db1"}, sqlDbs: []string{"db2"}, want: true},
		{name: "intersect no warn", formDbs: []string{"db1"}, sqlDbs: []string{"db1", "db2"}, want: false},
		{name: "empty form no warn", formDbs: nil, sqlDbs: []string{"db2"}, want: false},
		{name: "empty sql no warn", formDbs: []string{"db1"}, sqlDbs: nil, want: false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := disjointDumpWarning(c.formDbs, c.sqlDbs)
			if c.want && got == "" {
				t.Fatalf("expected warning, got empty")
			}
			if !c.want && got != "" {
				t.Fatalf("expected no warning, got %q", got)
			}
			if c.want {
				if !strings.Contains(got, "db1") || !strings.Contains(got, "db2") {
					t.Fatalf("warning should contain both dbs: %q", got)
				}
			}
		})
	}
}

func TestEmptyDumpDbsError(t *testing.T) {
	err := emptyDumpDbsError([]string{"db1"}, []string{"db2"}, nil)
	if err == nil {
		t.Fatal("expected error")
	}
	msg := err.Error()
	if !strings.Contains(msg, "db1") || !strings.Contains(msg, "db2") {
		t.Fatalf("error should contain form and sql dbs: %q", msg)
	}
	if strings.Contains(msg, "线上") {
		t.Fatalf("error should not say 线上: %q", msg)
	}
}

func TestSqlParseDbsSkipsEmptySpecialName(t *testing.T) {
	got := sqlParseDbs([]string{"db2"}, []string{"db3"}, []SpecialTblInfo{{DbName: "", Tbls: []string{"tb"}}})
	if len(got) != 2 {
		t.Fatalf("sqlParseDbs = %v, want db2 and db3", got)
	}
}
