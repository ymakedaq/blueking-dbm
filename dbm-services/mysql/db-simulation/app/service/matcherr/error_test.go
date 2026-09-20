/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package matcherr

import (
	"strings"
	"testing"
)

func TestNoMatchExecuteDbError(t *testing.T) {
	err := NoMatchExecuteDbError([]string{"db1"}, []string{}, []string{"db2"})
	if err == nil {
		t.Fatal("expected error")
	}
	msg := err.Error()
	if !strings.Contains(msg, "db1") || !strings.Contains(msg, "db2") {
		t.Fatalf("error should contain form and instance dbs: %q", msg)
	}
	if strings.Contains(msg, "线上") {
		t.Fatalf("error should not say 线上: %q", msg)
	}
	if !strings.Contains(msg, "模拟实例") {
		t.Fatalf("error should mention 模拟实例: %q", msg)
	}
}
