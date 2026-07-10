# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
from django.utils.translation import gettext as _

from backend.flow.engine.bamboo.scene.common.builder import SubBuilder
from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_ensure_cluster_subflow import (
    mysql_dts_ensure_cluster_subflow,
)
from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_migrate_task_subflow import mysql_dts_migrate_task_subflow
from backend.flow.plugins.components.collections.mysql.dts.migrate.create_user import MysqlDtsCreateUserComponent
from backend.flow.utils.mysql.dts.constants import MigrateTopology
from backend.flow.utils.mysql.dts.context import MysqlDtsMigrateSubflowInput


def mysql_dts_migrate_subflow(inp: MysqlDtsMigrateSubflowInput) -> SubBuilder:
    """迁移总入口：确保 DTS 集群 → 创建临时账号 → 执行 N 个 Task。

    ---------------------------------------------------------------------------
    【备忘】DTS 临时账号 drop_user 接入点（当前故意不挂在本流程末尾）
    ---------------------------------------------------------------------------
    原因：本 Flow 在 start_task 后即结束，增量同步仍长期依赖临时账号，
         不能像 checksum 那样在成功节点立刻 DROP。

    已具备能力：
      - 子流程：mysql_dts_drop_user_subflow
      - 组件：MysqlDtsDropUserComponent
      - 入参组装：
          build_drop_user_subflow_input_from_context(migrate_context)
          build_drop_user_subflow_input_from_snapshot(MysqlDtsInfo.temp_account_snapshot)
      - create_user 已写入 migrate_context.grant_hosts / grant_targets
      - update_meta 已落库 MysqlDtsInfo.temp_account_snapshot（不含密码）

    建议后续挂载位置（择一或组合）：
      1. 「断开同步 / 停止迁移」单据或控制 API（主路径，最干净）
      2. Signal 终态：FAILED / REVOKED / 确认已 Disconnected 后（见 mysql_dts_migrate_handler）
      3. DTS 集群 DESTROY：cleanup_subflow 在 stop_tasks 之后（见 mysql_dts_cleanup_subflow）

    接入示例：
      from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_drop_user_subflow import (
          mysql_dts_drop_user_subflow,
      )
      from backend.flow.utils.mysql.dts.migrate_credentials import (
          build_drop_user_subflow_input_from_snapshot,
      )
      drop_inp = build_drop_user_subflow_input_from_snapshot(
          root_id=..., bk_biz_id=..., snapshot=dts_info.temp_account_snapshot,
      )
      if drop_inp:
          sub.add_sub_pipeline(
              mysql_dts_drop_user_subflow(drop_inp).build_sub_process(
                  sub_name=_("删除 DTS 临时账号")
              )
          )
    ---------------------------------------------------------------------------
    """
    plan = inp.migrate_plan
    sub = SubBuilder(
        root_id=inp.root_id,
        data={
            "bk_biz_id": inp.bk_biz_id,
            "ticket_id": inp.ticket_id,
            "creator": inp.creator,
        },
    )
    sub.add_sub_pipeline(mysql_dts_ensure_cluster_subflow(inp).build_sub_process(sub_name=_("确保 DTS 集群")))

    sub.add_act(
        act_name=_("创建 DTS 迁移临时账号"),
        act_component_code=MysqlDtsCreateUserComponent.code,
        kwargs={
            "migrate_plan": plan,
            "creator": inp.creator,
        },
    )

    # NOTE: drop_user 不在此处调用，见上方 docstring「备忘」。

    task_subflows = []
    for task_spec in plan.task_specs:
        task_sub = mysql_dts_migrate_task_subflow(
            root_id=inp.root_id,
            bk_biz_id=inp.bk_biz_id,
            ticket_id=inp.ticket_id,
            master_addr="",
            task_spec=task_spec,
            migrate_plan=plan,
            creator=inp.creator,
        )
        task_subflows.append(task_sub.build_sub_process(sub_name=_("迁移任务 {}").format(task_spec.task_name)))

    if plan.topology == MigrateTopology.ONE_TO_MANY.value and len(task_subflows) > 1:
        sub.add_parallel_sub_pipeline(task_subflows)
    else:
        for task_subflow in task_subflows:
            sub.add_sub_pipeline(task_subflow)
    return sub
