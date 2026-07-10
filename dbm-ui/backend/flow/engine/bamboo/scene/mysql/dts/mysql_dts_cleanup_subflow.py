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
from backend.flow.engine.bamboo.scene.mysql.dts.subflow_common import hosts_to_exec_targets
from backend.flow.plugins.components.collections.mysql.dts.cleanup.clean_data_dir import MysqlDtsCleanDataDirComponent
from backend.flow.plugins.components.collections.mysql.dts.cleanup.offline_nodes import MysqlDtsOfflineNodesComponent
from backend.flow.plugins.components.collections.mysql.dts.cleanup.precheck import MysqlDtsCleanupPrecheckComponent
from backend.flow.plugins.components.collections.mysql.dts.cleanup.stop_process import MysqlDtsStopProcessComponent
from backend.flow.plugins.components.collections.mysql.dts.cleanup.stop_tasks import MysqlDtsStopTasksComponent
from backend.flow.plugins.components.collections.mysql.dts.cleanup.unregister_meta import (
    MysqlDtsUnregisterClusterMetaComponent,
)
from backend.flow.utils.mysql.dts.context import DtsHostSpec, MysqlDtsCleanupSubflowInput


def _collect_cleanup_targets(inp: MysqlDtsCleanupSubflowInput) -> list[DtsHostSpec]:
    if inp.target_hosts:
        return inp.target_hosts
    seen = set()
    hosts = []
    for node in inp.master_nodes + inp.worker_nodes:
        ip = node["ip"]
        if ip in seen:
            continue
        seen.add(ip)
        hosts.append(DtsHostSpec(ip=ip, bk_cloud_id=node.get("bk_cloud_id", inp.bk_cloud_id)))
    return hosts


def mysql_dts_cleanup_subflow(inp: MysqlDtsCleanupSubflowInput) -> SubBuilder:
    """清理/销毁 DTS 集群。

    ---------------------------------------------------------------------------
    【备忘】临时账号 drop_user 建议挂在此处（DESTROY 路径）
    ---------------------------------------------------------------------------
    时机：stop_tasks（任务已停）之后、stop_process（杀进程）之前。
    数据：按 dts_cluster_id 查 MysqlDtsInfo.temp_account_snapshot，
         用 build_drop_user_subflow_input_from_snapshot 组装后调用
         mysql_dts_drop_user_subflow。
    注意：同一 DTS 集群可能关联多条迁移记录，需去重 user+targets 后批量 drop。
    详见 mysql_dts_migrate_subflow docstring 备忘。
    ---------------------------------------------------------------------------
    """
    cleanup_hosts = _collect_cleanup_targets(inp)
    exec_targets = hosts_to_exec_targets(cleanup_hosts)

    sub = SubBuilder(
        root_id=inp.root_id,
        data={
            "bk_biz_id": inp.bk_biz_id,
            "bk_cloud_id": inp.bk_cloud_id,
            "creator": inp.creator,
        },
    )

    sub.add_act(
        act_name=_("清理前置检查"),
        act_component_code=MysqlDtsCleanupPrecheckComponent.code,
        kwargs={
            "dts_cluster_id": inp.dts_cluster_id,
            "force_destroy": inp.force_destroy,
        },
    )
    sub.add_act(
        act_name=_("停止并删除 DTS 任务/Source"),
        act_component_code=MysqlDtsStopTasksComponent.code,
        kwargs={
            "master_addr": inp.master_addr,
            "force_destroy": inp.force_destroy,
        },
    )
    # NOTE【备忘】: 此处可插入 mysql_dts_drop_user_subflow（见上方 docstring）。
    sub.add_act(
        act_name=_("下线 DTS 节点注册信息"),
        act_component_code=MysqlDtsOfflineNodesComponent.code,
        kwargs={
            "master_addr": inp.master_addr,
            "worker_nodes": inp.worker_nodes,
            "master_nodes": inp.master_nodes,
            "force_destroy": inp.force_destroy,
        },
    )

    if exec_targets:
        sub.add_act(
            act_name=_("停止 DTS 进程"),
            act_component_code=MysqlDtsStopProcessComponent.code,
            kwargs={
                "exec_targets": exec_targets,
                "deploy_path": inp.deploy_path,
            },
        )
        if inp.clean_data_dir:
            sub.add_act(
                act_name=_("清理 DTS 部署目录"),
                act_component_code=MysqlDtsCleanDataDirComponent.code,
                kwargs={
                    "exec_targets": exec_targets,
                    "deploy_path": inp.deploy_path,
                },
            )

    sub.add_act(
        act_name=_("下线 DTS 集群元数据"),
        act_component_code=MysqlDtsUnregisterClusterMetaComponent.code,
        kwargs={
            "dts_cluster_id": inp.dts_cluster_id,
            "recycle_hosts": inp.recycle_hosts,
            "target_hosts": [{"ip": h.ip, "bk_cloud_id": h.bk_cloud_id} for h in cleanup_hosts]
            if inp.target_hosts
            else None,
            "creator": inp.creator,
        },
    )
    return sub
