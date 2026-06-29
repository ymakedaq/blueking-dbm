/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package spiderctl

import (
	"fmt"
	"os"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util/ddlrewrite"
)

// rewriteSchemaDumpFile backs up dumpPath to dumpPath.orig, then rewrites CREATE TABLE to IF NOT EXISTS in dumpPath.
func rewriteSchemaDumpFile(dumpPath string) error {
	originalPath := dumpPath + ".orig"
	if err := os.Rename(dumpPath, originalPath); err != nil {
		return fmt.Errorf("backup dump file %s failed: %w", dumpPath, err)
	}
	if err := ddlrewrite.RewriteCreateTableIfNotExistsInFile(originalPath, dumpPath); err != nil {
		return fmt.Errorf("rewrite create table in dump file %s failed: %w", originalPath, err)
	}
	logger.Info(
		"rewrite create table if not exists success, original=%s rewritten=%s",
		originalPath,
		dumpPath,
	)
	return nil
}
