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
import logging
from dataclasses import asdict, is_dataclass

from django.utils.translation import gettext as _

from backend.db_meta.api.cluster.mysqldts import decommission
from backend.db_meta.models import MysqlDtsCluster
from backend.db_meta.models.mysql_dts import MysqlDtsInfo, MysqlDtsStatus
from backend.flow.consts import StateType
from backend.flow.engine.bamboo.engine import BambooEngine
from backend.flow.signal.callback_map import create_ticket_handler
from backend.flow.utils.mysql.dts.constants import DtsLifecycleMode
from backend.ticket.constants import TicketType

logger = logging.getLogger("flow")


def _as_mapping(value) -> dict:
    if value is None:
        return {}
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return {}


def _sync_migrate_status(ticket_id: int, status: StateType):
    status_map = {
        StateType.RUNNING: MysqlDtsStatus.FullOnline.value,
        StateType.FAILED: MysqlDtsStatus.FullFailed.value,
        StateType.REVOKED: MysqlDtsStatus.Terminated.value,
        StateType.FINISHED: MysqlDtsStatus.Disconnected.value,
    }
    mapped = status_map.get(status)
    if mapped:
        MysqlDtsInfo.objects.filter(ticket_id=ticket_id).update(status=mapped)


def _finalize_ephemeral_dts(global_data: dict):
    """临时 DTS 终态回收。

    ---------------------------------------------------------------------------
    【备忘】临时账号 drop_user 可在此兜底接入（Signal 路径）
    ---------------------------------------------------------------------------
    适用：FAILED / REVOKED，或确认迁移已 Disconnected 且不再需要连库。
    不建议：仅 Flow FINISHED（start_task 成功）就 drop——增量可能仍在跑。
    做法：读 MysqlDtsInfo.temp_account_snapshot →
          build_drop_user_subflow_input_from_snapshot →
          异步触发 drop（或同步尽力 DROP，失败只打日志）。
    详见 mysql_dts_migrate_subflow / mysql_dts_drop_user_subflow。
    ---------------------------------------------------------------------------
    """
    ticket_id = global_data.get("ticket_id")
    migrate_plan = _as_mapping(global_data.get("migrate_plan"))
    lifecycle = migrate_plan.get("dts_lifecycle", "")
    cleanup_after = migrate_plan.get("cleanup_after_migrate", False)
    if lifecycle != DtsLifecycleMode.DEPLOY_EPHEMERAL.value and not cleanup_after:
        return
    dts_info = MysqlDtsInfo.objects.filter(ticket_id=ticket_id).first()
    if not dts_info or not dts_info.dts_cluster_id:
        return
    dts_cluster = MysqlDtsCluster.objects.filter(id=dts_info.dts_cluster_id).first()
    if not dts_cluster:
        return
    # NOTE【备忘】: 回收元数据前可先按 dts_info.temp_account_snapshot drop 临时账号。
    # 一期终态先回收元数据；完整 stop_task/kill 进程由独立 DESTROY 单据或后续 cleanup 编排补齐
    decommission(
        dts_cluster_id=dts_cluster.id,
        recycle_hosts=migrate_plan.get("recycle_dts_hosts", True),
        updater=global_data.get("created_by", ""),
    )


@create_ticket_handler(TicketType.MYSQL_HA_TO_HA_MIGRATE)
def mysql_ha_to_ha_migrate_callback_handler(root_id: str, node_id: str, status: StateType, **kwargs):
    """HA→HA 迁移状态回调与终态清理。"""
    logger.info(_("执行 mysql_ha_to_ha_migrate_callback_handler root_id={}").format(root_id))
    engine = BambooEngine(root_id=root_id)
    global_data = engine.get_node_input_data(node_id=node_id).data.get("global_data", {})
    ticket_id = global_data.get("ticket_id")
    if ticket_id:
        _sync_migrate_status(ticket_id, status)
    if status in [StateType.FINISHED, StateType.FAILED, StateType.REVOKED]:
        _finalize_ephemeral_dts(global_data)


@create_ticket_handler(TicketType.MYSQL_HA_TO_CLUSTER_MIGRATE)
def mysql_ha_to_cluster_migrate_callback_handler(root_id: str, node_id: str, status: StateType, **kwargs):
    """HA→Cluster 迁移状态回调与终态清理。"""
    logger.info(_("执行 mysql_ha_to_cluster_migrate_callback_handler root_id={}").format(root_id))
    engine = BambooEngine(root_id=root_id)
    global_data = engine.get_node_input_data(node_id=node_id).data.get("global_data", {})
    ticket_id = global_data.get("ticket_id")
    if ticket_id:
        _sync_migrate_status(ticket_id, status)
    if status in [StateType.FINISHED, StateType.FAILED, StateType.REVOKED]:
        _finalize_ephemeral_dts(global_data)
