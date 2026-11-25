/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package spidercmd

import (
	"context"
	"fmt"
	"os/signal"
	"syscall"

	"dbm-services/common/go-pubpkg/logger"
	"dbm-services/mysql/db-tools/dbactuator/internal/subcmd"
	spiderdts "dbm-services/mysql/db-tools/dbactuator/pkg/components/spider/dts_cutover"
	"dbm-services/mysql/db-tools/dbactuator/pkg/util"

	"github.com/spf13/cobra"
)

// SpiderDtsCutoverAct TenDBCluster DTS 安全切换命令
type SpiderDtsCutoverAct struct {
	*subcmd.BaseOptions
	Service spiderdts.Comp
}

const (
	// SpiderDtsCutover 命令名
	SpiderDtsCutover = "dts-cutover"
)

// NewSpiderDtsCutoverCommand godoc
//
//	@Summary TenDBCluster DTS 安全切换
//	@Description 在 DTS Master 上：预检 → 锁定源集群全部 SPIDER_MASTER/SPIDER_MNT → 各分片 remote master 快照 → 持锁复核追平 → 停任务收敛 → 位点文件落盘 → 解锁
//	@Tags spider
//	@Accept json
//	@Param body body spiderdts.Comp true "description"
//	@Router /spider/dts-cutover [post]
func NewSpiderDtsCutoverCommand() *cobra.Command {
	act := SpiderDtsCutoverAct{
		BaseOptions: subcmd.GBaseOptions,
	}
	cmd := &cobra.Command{
		Use:   SpiderDtsCutover,
		Short: "TenDBCluster DTS 安全切换（Spider 锁 + 分片快照 + 停任务）",
		Example: fmt.Sprintf(
			`dbactuator spider %s %s %s`,
			SpiderDtsCutover, subcmd.CmdBaseExampleStr, subcmd.ToPrettyJson(act.Service.Example()),
		),
		Run: func(cmd *cobra.Command, args []string) {
			util.CheckErr(act.Validate())
			util.CheckErr(act.Init())
			util.CheckErr(act.Run())
		},
	}
	return cmd
}

// Validate 基本验证
func (c *SpiderDtsCutoverAct) Validate() (err error) {
	return c.BaseOptions.Validate()
}

// Init 初始化
func (c *SpiderDtsCutoverAct) Init() (err error) {
	if err = c.Deserialize(&c.Service.Params); err != nil {
		logger.Error("DeserializeAndValidate err %s", err.Error())
		return err
	}
	c.Service.GeneralParam = subcmd.GeneralRuntimeParam
	logger.Info(
		"spider dts-cutover params loaded: task=%s deploy_path=%s master=%s key=%s",
		c.Service.Params.TaskName, c.Service.Params.DeployPath,
		c.Service.Params.DtsMasterAddr, c.Service.Params.PositionFileKey,
	)
	return nil
}

// Run 执行：signal.NotifyContext 感知 SIGTERM/SIGINT（03 方案第 7.2 节 A2），主流程取消后 defer 正常解锁
func (c *SpiderDtsCutoverAct) Run() (err error) {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	c.Service.Ctx = ctx

	// 预检由 Service.Run 内部在幂等检查之后执行：
	// 放在这里会先于幂等检查——任务已 Stopped 的重试会被“任务不在运行中”拦死，
	// 且残留会话清理会早于幂等判定执行。
	steps := subcmd.Steps{
		{
			FunName: "Init",
			Func:    c.Service.Init,
		},
		{
			FunName: "TenDBCluster DTS 安全切换",
			Func:    c.Service.Run,
		},
	}
	if err = steps.Run(); err != nil {
		return err
	}
	logger.Info("spider DTS cutover 完成")
	return nil
}
