/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package spiderctlcmd

import (
	"fmt"

	"github.com/spf13/cobra"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/internal/subcmd"
	"dbm-services/mysql/db-tools/dbactuator/pkg/components/spiderctl"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util"
)

// RestoreSchemaFromBackendViaCtlAct restores table schema from backend shard0 to tdbctl with CREATE TABLE IF NOT EXISTS rewrite.
type RestoreSchemaFromBackendViaCtlAct struct {
	*subcmd.BaseOptions
	Service spiderctl.ImportSchemaFromBackendComp
}

// NewRestoreSchemaFromBackendViaCtlCommand creates restore-schema-from-backend-via-ctl subcommand.
func NewRestoreSchemaFromBackendViaCtlCommand() *cobra.Command {
	act := RestoreSchemaFromBackendViaCtlAct{
		BaseOptions: subcmd.GBaseOptions,
	}
	subCmdStr := "restore-schema-from-backend-via-ctl"
	cmd := &cobra.Command{
		Use:   subCmdStr,
		Short: "从 Backend 导出表结构改写后导入中控恢复",
		Example: fmt.Sprintf(
			`dbactuator spiderctl %s %s %s`,
			subCmdStr,
			subcmd.CmdBaseExampleStr,
			subcmd.ToPrettyJson(act.Service.Example()),
		),
		Run: func(cmd *cobra.Command, args []string) {
			util.CheckErr(act.Validate())
			util.CheckErr(act.Init())
			util.CheckErr(act.Run())
		},
	}
	return cmd
}

// Init prepares runtime parameters.
func (d *RestoreSchemaFromBackendViaCtlAct) Init() (err error) {
	logger.Info("restore schema from backend via ctl init")
	if err = d.Deserialize(&d.Service.Params); err != nil {
		logger.Error("DeserializeAndValidate failed, %v", err)
		return err
	}
	d.Service.GeneralParam = subcmd.GeneralRuntimeParam
	return nil
}

// Run executes dump, rewrite CREATE TABLE IF NOT EXISTS, then import to tdbctl.
func (d *RestoreSchemaFromBackendViaCtlAct) Run() (err error) {
	steps := subcmd.Steps{
		{
			FunName: "初始化",
			Func:    d.Service.Init,
		},
		{
			FunName: "从backend导出表结构改写后导入中控",
			Func:    d.Service.MigrateSchemaRewriteToTdbctl,
		},
	}

	if err = steps.Run(); err != nil {
		return err
	}

	logger.Info("restore schema from backend via ctl success ~")
	return nil
}
