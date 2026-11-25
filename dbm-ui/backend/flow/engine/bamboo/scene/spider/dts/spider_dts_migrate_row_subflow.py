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
import shlex

from django.utils.translation import gettext as _

from backend.flow.engine.bamboo.scene.common.builder import SubBuilder
from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_ensure_cluster_subflow import (
    mysql_dts_ensure_cluster_subflow,
)
from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_task_clean_subflow import mysql_dts_task_clean_subflow
from backend.flow.engine.bamboo.scene.spider.dts.spider_dts_grant_subflow import spider_dts_grant_subflow
from backend.flow.engine.bamboo.scene.spider.dts.spider_dts_migrate_task_subflow import spider_dts_migrate_task_subflow
from backend.flow.plugins.components.collections.mysql.dts.base_shell import MysqlDtsExecShellComponent
from backend.flow.utils.mysql.dts.constants import FullLoadEngine
from backend.flow.utils.mysql.dts.context import MysqlDtsMigrateSubflowInput, MysqlDtsTaskCleanSubflowInput
from backend.flow.utils.mysql.dts.cutover_helper import resolve_dts_master_exec_target, resolve_master_addr_from_plan
from backend.flow.utils.mysql.dts.migrate_credentials import grant_targets_to_dicts
from backend.flow.utils.mysql.dts.migrate_helper import build_ticket_dts_clean_names, plan_deploy_cluster_name
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan
from backend.flow.utils.spider.dts.cutover_helper import resolve_spider_dts_deploy_path
from backend.flow.utils.spider.dts.grant_helper import resolve_cluster_to_cluster_temp_account_for_pipeline


def build_migrate_row_sub_name(plan) -> str:
    """画布行节点名：{src_id} 迁移-> {dst_id}。"""
    src_ids: list[int] = []
    dst_ids: list[int] = []
    seen_src: set[int] = set()
    seen_dst: set[int] = set()
    for spec in getattr(plan, "task_specs", None) or []:
        for source in getattr(spec, "sources", None) or []:
            cid = getattr(source, "cluster_id", None)
            if cid and cid not in seen_src:
                seen_src.add(cid)
                src_ids.append(int(cid))
        dst = getattr(spec, "target_cluster_id", None)
        if dst and dst not in seen_dst:
            seen_dst.add(dst)
            dst_ids.append(int(dst))
    src_label = ",".join(str(i) for i in src_ids) or "-"
    dst_label = ",".join(str(i) for i in dst_ids) or "-"
    return _("{} 迁移-> {}").format(src_label, dst_label)


def _build_position_file_clean_kwargs(
    *, root_id: str, migrate_plan: DtsMigratePlan, master_addr: str, task_name: str
) -> dict:
    """删除 DTS Master 上的切换位点文件（dts-task-clean 后置，失败忽略）。"""
    exec_target = resolve_dts_master_exec_target(migrate_plan, master_addr)
    deploy_path = resolve_spider_dts_deploy_path(migrate_plan)
    position_file_key = "{}_{}".format(root_id, task_name)
    # 位点文件名来自 root_id 与 task_name：禁止路径分隔符与 .. ，避免 rm 越出 cutover 目录
    if not position_file_key or any(sep in position_file_key for sep in ("/", "\\")) or ".." in position_file_key:
        raise ValueError(_("非法的切换位点文件名: {}").format(position_file_key))
    cutover_dir = "{}/cutover".format(deploy_path.rstrip("/"))
    # 只删本任务位点文件，不动 cutover 目录（DTS Master 可能被复用）
    position_file = "{}/{}.json".format(cutover_dir, position_file_key)
    shell_script = "rm -f {}\nexit 0\n".format(shlex.quote(position_file))
    return {
        "exec_targets": [exec_target],
        "shell_script": shell_script,
    }


def build_spider_migrate_row_subflow(
    *,
    root_id: str,
    bk_biz_id: int,
    ticket_id: int,
    migrate_plan: DtsMigratePlan,
    creator: str,
    sub_name: str,
    row_key: str = "",
):
    """一行独立子流程：ensure DTS 集群 → 授权 → 任务（追平切换）→ dts-task-clean → 清位点文件。

    每行自建临时账号（源端分片 + Spider，目标端复用），串行挂在本行 SubBuilder 内。
    """
    dts_user, dts_password, grant_hosts, grant_targets = resolve_cluster_to_cluster_temp_account_for_pipeline(
        migrate_plan
    )
    master_addr = resolve_master_addr_from_plan(migrate_plan)
    if not master_addr:
        raise ValueError(_("无法解析 DTS master_addr，无法构建迁移行子流程"))
    row = SubBuilder(
        root_id=root_id,
        data={
            "bk_biz_id": bk_biz_id,
            "ticket_id": ticket_id,
            "uid": ticket_id or root_id,
            "creator": creator,
            "created_by": creator,
            "root_id": root_id,
            "row_key": row_key,
            "dts_task_ids": [s.task_name for s in migrate_plan.task_specs if getattr(s, "task_name", None)],
        },
    )
    row.add_sub_pipeline(
        mysql_dts_ensure_cluster_subflow(
            MysqlDtsMigrateSubflowInput(
                root_id=root_id,
                bk_biz_id=bk_biz_id,
                ticket_id=ticket_id,
                migrate_plan=migrate_plan,
                creator=creator,
            )
        ).build_sub_process(sub_name=_("部署 DTS 集群") if not migrate_plan.dts_cluster_id else _("加载已有 DTS 集群"))
    )
    row.add_sub_pipeline(
        spider_dts_grant_subflow(
            root_id=root_id,
            bk_biz_id=bk_biz_id,
            ticket_id=ticket_id,
            migrate_plan=migrate_plan,
            creator=creator,
            dts_user=dts_user,
            dts_password=dts_password,
            grant_hosts=grant_hosts,
            grant_targets=grant_targets,
        ).build_sub_process(sub_name=_("创建 DTS 临时账号并授权"))
    )
    for task_spec in migrate_plan.task_specs:
        row.add_sub_pipeline(
            spider_dts_migrate_task_subflow(
                root_id=root_id,
                bk_biz_id=bk_biz_id,
                ticket_id=ticket_id,
                master_addr=master_addr,
                task_spec=task_spec,
                migrate_plan=migrate_plan,
                creator=creator,
                dts_user=dts_user,
                dts_password=dts_password,
            ).build_sub_process(sub_name=_("迁移任务 {}").format(task_spec.task_name))
        )

    task_names, source_names = build_ticket_dts_clean_names(migrate_plan)
    task_cfg = getattr(migrate_plan, "dts_task_config", None)
    task_mode = (getattr(task_cfg, "task_mode", None) or "all") if task_cfg is not None else "all"
    full_load_engine = (
        (getattr(task_cfg, "full_load_engine", None) or FullLoadEngine.BUILTIN.value)
        if task_cfg is not None
        else FullLoadEngine.BUILTIN.value
    )
    clean_inp = MysqlDtsTaskCleanSubflowInput(
        root_id=root_id,
        bk_biz_id=bk_biz_id,
        dts_user=dts_user,
        grant_hosts=grant_hosts,
        grant_targets=grant_targets_to_dicts(grant_targets),
        ignore_errors=True,
        creator=creator,
        master_addr=master_addr,
        bk_cloud_id=int(migrate_plan.bk_cloud_id or 0),
        task_names=task_names,
        source_names=source_names,
        dts_cluster_id=getattr(migrate_plan, "dts_cluster_id", None) or None,
        cluster_name=plan_deploy_cluster_name(migrate_plan),
        task_mode=task_mode,
        full_load_engine=full_load_engine,
    )
    row.add_sub_pipeline(mysql_dts_task_clean_subflow(clean_inp).build_sub_process(sub_name=_("dts-task-clean")))
    # 位点文件为权威记录落库后的本地缓存：task-clean 后删除，避免 DTS 集群复用时残留干扰
    for task_name in task_names:
        row.add_act(
            act_name=_("清理切换位点文件 {}").format(task_name),
            act_component_code=MysqlDtsExecShellComponent.code,
            kwargs=_build_position_file_clean_kwargs(
                root_id=root_id,
                migrate_plan=migrate_plan,
                master_addr=master_addr,
                task_name=task_name,
            ),
        )
    return row.build_sub_process(sub_name=sub_name)


def build_parallel_spider_migrate_row_pipelines(
    *,
    pipeline,
    root_id: str,
    data: dict,
    migrate_plans: list,
):
    """按 plan 列表并行挂行子流程（cluster_to_cluster 固定 migrate_type）。"""
    creator = data.get("created_by", "")
    bk_biz_id = int(data["bk_biz_id"])
    ticket_id = int(data.get("ticket_id", 0) or 0)
    row_subs = []
    for idx, plan in enumerate(migrate_plans):
        row_subs.append(
            build_spider_migrate_row_subflow(
                root_id=root_id,
                bk_biz_id=bk_biz_id,
                ticket_id=ticket_id,
                migrate_plan=plan,
                creator=creator,
                sub_name=build_migrate_row_sub_name(plan),
                row_key=f"row-{idx}",
            )
        )
    pipeline.add_parallel_sub_pipeline(sub_flow_list=row_subs)
