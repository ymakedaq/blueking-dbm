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
from backend.flow.plugins.components.collections.mysql.dts.migrate.prepare_user import (
    MysqlDtsPrepareMigrateUserComponent,
)
from backend.flow.utils.mysql.dts.migrate_credentials import (
    DtsGrantTarget,
    build_dts_add_user_parallel_acts,
    grant_targets_to_dicts,
)
from backend.flow.utils.mysql.dts.migrate_helper import plan_deploy_cluster_name
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan


def spider_dts_grant_subflow(
    *,
    root_id: str,
    bk_biz_id: int,
    ticket_id: int,
    migrate_plan: DtsMigratePlan,
    creator: str,
    dts_user: str,
    dts_password: str,
    grant_hosts: list[str],
    grant_targets: list[DtsGrantTarget],
) -> SubBuilder:
    """cluster_to_cluster 临时账号与授权：源端分片 remote master/slave + Spider master/mnt，目标端复用。"""
    sub = SubBuilder(
        root_id=root_id,
        data={
            "bk_biz_id": bk_biz_id,
            "ticket_id": ticket_id,
            "uid": ticket_id,
            "creator": creator,
            "created_by": creator,
            "root_id": root_id,
        },
    )
    sub.add_act(
        act_name=_("准备 DTS 迁移临时账号"),
        act_component_code=MysqlDtsPrepareMigrateUserComponent.code,
        kwargs={
            "dts_user": dts_user,
            "dts_password": dts_password,
            "grant_hosts": grant_hosts,
            "grant_targets": grant_targets_to_dicts(grant_targets),
            "bk_biz_id": bk_biz_id,
            "dts_cluster_id": getattr(migrate_plan, "dts_cluster_id", None) or None,
            "cluster_name": plan_deploy_cluster_name(migrate_plan),
        },
    )
    sub.add_parallel_acts(
        acts_list=build_dts_add_user_parallel_acts(
            dts_user=dts_user,
            dts_password=dts_password,
            grant_hosts=grant_hosts,
            grant_targets=grant_targets,
        )
    )
    return sub
