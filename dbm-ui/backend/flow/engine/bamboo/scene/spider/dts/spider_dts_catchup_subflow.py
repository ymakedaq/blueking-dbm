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
from dataclasses import asdict

from django.utils.translation import gettext as _

from backend.configuration.constants import DBType
from backend.flow.consts import DBA_ROOT_USER
from backend.flow.engine.bamboo.scene.common.builder import SubBuilder
from backend.flow.engine.bamboo.scene.common.get_file_list import GetFileList
from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_wait_catchup_subflow import mysql_dts_wait_catchup_subflow
from backend.flow.plugins.components.collections.common.pause import PauseComponent
from backend.flow.plugins.components.collections.mysql.dts.migrate.cutover_meta import MysqlDtsCutoverMetaComponent
from backend.flow.plugins.components.collections.mysql.dts.migrate.poll_confirm_alive import (
    MysqlDtsPollConfirmAliveComponent,
)
from backend.flow.plugins.components.collections.mysql.exec_actuator_script import ExecuteDBActuatorScriptComponent
from backend.flow.plugins.components.collections.mysql.trans_flies import TransFileComponent
from backend.flow.utils.mysql.dts.context import (
    MysqlDtsCutoverSubflowInput,
    MysqlDtsTransData,
    MysqlDtsWaitCatchupSubflowInput,
)
from backend.flow.utils.mysql.dts.cutover_helper import resolve_dts_master_exec_target
from backend.flow.utils.mysql.dts.migrate_helper import task_mode_runs_incremental
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, DtsTaskSpec
from backend.flow.utils.mysql.mysql_act_dataclass import DownloadMediaKwargs, ExecActuatorKwargs
from backend.flow.utils.mysql.mysql_act_playload import MysqlActPayload
from backend.flow.utils.spider.dts.constants import SPIDER_DTS_CUTOVER_JOB_TIMEOUT
from backend.flow.utils.spider.dts.cutover_helper import (
    build_spider_dts_cutover_payload,
    resolve_spider_dts_deploy_path,
)


def spider_dts_catchup_subflow(
    *,
    root_id: str,
    bk_biz_id: int,
    ticket_id: int,
    master_addr: str,
    task_spec: DtsTaskSpec,
    migrate_plan: DtsMigratePlan,
    dts_user: str,
    dts_password: str,
    creator: str = "",
) -> SubBuilder:
    """追平切换子流程（02 第 6.1 节）：追平前置门槛 → 人工确认 → spider dts-cutover → 写终态。

    - 追平判断偏松（SyncStatus），只当前置门槛；严格判定由 dbactuator 持锁快照复核完成（03）
    - master_addr 建流期确定并写入各节点 kwargs：位点文件落在该 DTS Master 本地，
      节点重试不得换机器（DTS Master 故障需人工处理）
    - 命令上限约 3 分钟（03 第 8 节），Job 超时 5 分钟兜底
    """
    inp = MysqlDtsCutoverSubflowInput(
        root_id=root_id,
        bk_biz_id=bk_biz_id,
        ticket_id=ticket_id,
        master_addr=master_addr,
        task_name=task_spec.task_name,
        deploy_path=resolve_spider_dts_deploy_path(migrate_plan),
        dts_cluster_id=migrate_plan.dts_cluster_id or 0,
        creator=creator,
    )
    sub = SubBuilder(
        root_id=root_id,
        data={
            "bk_biz_id": bk_biz_id,
            "uid": ticket_id,
            "ticket_id": ticket_id,
            "creator": creator,
            "created_by": creator,
            "root_id": root_id,
        },
    )
    sub.add_sub_pipeline(
        sub_flow=mysql_dts_wait_catchup_subflow(
            MysqlDtsWaitCatchupSubflowInput(
                root_id=root_id,
                bk_biz_id=bk_biz_id,
                master_addr=master_addr,
                task_name=task_spec.task_name,
                bk_cloud_id=int(migrate_plan.bk_cloud_id or 0),
                source_name_list=[s.source_name for s in task_spec.sources if s.source_name] or None,
                creator=creator,
            )
        ).build_sub_process(sub_name=_("等待 DTS 追平（前置门槛）"))
    )

    task_mode = task_spec.dts_task_config.task_mode or migrate_plan.dts_task_config.task_mode or "all"
    confirm_name = _("增量同步中/确认切换（不切换域名）")
    if task_mode_runs_incremental(task_mode):
        sub.add_act(
            act_name=confirm_name,
            act_component_code=MysqlDtsPollConfirmAliveComponent.code,
            kwargs={
                "master_addr": master_addr,
                "bk_cloud_id": int(migrate_plan.bk_cloud_id or 0),
                "task_name": task_spec.task_name,
                "source_name_list": [s.source_name for s in task_spec.sources if s.source_name] or None,
            },
        )
    else:
        sub.add_act(
            act_name=confirm_name,
            act_component_code=PauseComponent.code,
            kwargs={},
        )

    exec_target = resolve_dts_master_exec_target(migrate_plan, master_addr)
    cutover_extend = build_spider_dts_cutover_payload(
        root_id=root_id,
        master_addr=master_addr,
        deploy_path=inp.deploy_path,
        task_spec=task_spec,
        dts_user=dts_user,
        dts_password=dts_password,
    )
    sub.add_act(
        act_name=_("下发 db-actuator 到 DTS Master"),
        act_component_code=TransFileComponent.code,
        kwargs=asdict(
            DownloadMediaKwargs(
                bk_cloud_id=exec_target["bk_cloud_id"],
                exec_ip=exec_target["ip"],
                file_list=GetFileList(db_type=DBType.MySQL).get_db_actuator_package(),
            )
        ),
    )
    sub.add_act(
        act_name=_("执行 TenDBCluster DTS 安全切换（Spider 锁 + 分片快照 + 停任务）"),
        act_component_code=ExecuteDBActuatorScriptComponent.code,
        kwargs=asdict(
            ExecActuatorKwargs(
                bk_cloud_id=exec_target["bk_cloud_id"],
                exec_ip=exec_target["ip"],
                run_as_system_user=DBA_ROOT_USER,
                get_mysql_payload_func=MysqlActPayload.get_spider_dts_cutover_payload.__name__,
                cluster={"spider_dts_cutover": cutover_extend},
                job_timeout=SPIDER_DTS_CUTOVER_JOB_TIMEOUT,
            )
        ),
        write_payload_var=MysqlDtsTransData.get_cutover_position_var_name(),
    )
    sub.add_act(
        act_name=_("更新迁移元数据为已断开"),
        act_component_code=MysqlDtsCutoverMetaComponent.code,
        kwargs={
            "ticket_id": ticket_id,
            "task_name": task_spec.task_name,
            # 位点由上游 actuator write_payload_var → trans_data.cutover_position 注入
        },
    )
    return sub
