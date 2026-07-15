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
from backend.flow.plugins.components.collections.mysql.dts.migrate.create_task import MysqlDtsCreateTaskComponent
from backend.flow.plugins.components.collections.mysql.dts.migrate.register_source import (
    MysqlDtsRegisterSourceComponent,
)
from backend.flow.plugins.components.collections.mysql.dts.migrate.start_task import MysqlDtsStartTaskComponent
from backend.flow.plugins.components.collections.mysql.dts.migrate.update_meta import MysqlDtsUpdateMetaComponent
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, DtsTaskSpec


def mysql_dts_migrate_task_subflow(
    *,
    root_id: str,
    bk_biz_id: int,
    ticket_id: int,
    master_addr: str,
    task_spec: DtsTaskSpec,
    migrate_plan: DtsMigratePlan,
    creator: str = "",
) -> SubBuilder:
    """单 DTS Task 注册并启动。"""
    sub = SubBuilder(
        root_id=root_id,
        data={
            "bk_biz_id": bk_biz_id,
            "ticket_id": ticket_id,
            "creator": creator,
        },
    )

    sub.add_act(
        act_name=_("注册 DTS Source"),
        act_component_code=MysqlDtsRegisterSourceComponent.code,
        kwargs={
            "master_addr": master_addr,
            "task_spec": task_spec,
            "migrate_plan": migrate_plan,
            "migrate_type": migrate_plan.migrate_type,
        },
    )
    sub.add_act(
        act_name=_("创建 DTS 任务"),
        act_component_code=MysqlDtsCreateTaskComponent.code,
        kwargs={
            "master_addr": master_addr,
            "task_spec": task_spec,
            "migrate_plan": migrate_plan,
        },
    )
    sub.add_act(
        act_name=_("启动 DTS 任务"),
        act_component_code=MysqlDtsStartTaskComponent.code,
        kwargs={
            "master_addr": master_addr,
            "task_name": task_spec.task_name,
        },
    )
    sub.add_act(
        act_name=_("写入迁移元数据"),
        act_component_code=MysqlDtsUpdateMetaComponent.code,
        kwargs={
            "bk_biz_id": bk_biz_id,
            "ticket_id": ticket_id,
            "task_spec": task_spec,
            "migrate_type": migrate_plan.migrate_type,
            "migrate_topology": migrate_plan.topology,
            "task_name": task_spec.task_name,
            "dts_cluster_id": migrate_plan.dts_cluster_id,
            "creator": creator,
        },
    )
    return sub
