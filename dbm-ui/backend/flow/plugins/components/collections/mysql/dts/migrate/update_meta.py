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
from dataclasses import asdict

from django.utils.translation import gettext as _
from pipeline.component_framework.component import Component

from backend.db_meta.models import MysqlDtsInfo
from backend.db_meta.models.mysql_dts import MysqlDtsStatus
from backend.flow.plugins.components.collections.common.base_service import BaseService
from backend.flow.utils.mysql.dts.migrate_plan import DtsTaskSpec

logger = logging.getLogger("flow")


class MysqlDtsUpdateMetaService(BaseService):
    """创建 MySQL DTS 迁移记录。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        global_data = data.get_one_of_inputs("global_data") or {}
        trans_data = data.get_one_of_inputs("trans_data")
        task_spec: DtsTaskSpec = kwargs["task_spec"]
        source_cluster_id = task_spec.sources[0].cluster_id if task_spec.sources else 0
        sync_scope_snapshot = {}
        if task_spec.sources:
            sync_scope_snapshot = asdict(task_spec.sources[0].sync_scope)

        registered_source_names = kwargs.get("registered_source_names") or list(
            trans_data.migrate_context.registered_source_names or []
        )
        from backend.flow.utils.mysql.dts.migrate_credentials import build_temp_account_snapshot

        temp_account_snapshot = {}
        if trans_data.migrate_context.dts_user and trans_data.migrate_context.grant_hosts:
            temp_account_snapshot = build_temp_account_snapshot(
                dts_user=trans_data.migrate_context.dts_user,
                grant_hosts=trans_data.migrate_context.grant_hosts,
                grant_targets=trans_data.migrate_context.grant_targets,
            )

        # 任务刚启动，先记为全量传输中；后续由轮询/回调推进到增量或终态
        dts_info = MysqlDtsInfo.objects.create(
            bk_biz_id=kwargs["bk_biz_id"],
            source_cluster_id=source_cluster_id,
            target_cluster_id=task_spec.target_cluster_id,
            dts_cluster_id=kwargs.get("dts_cluster_id") or trans_data.migrate_context.dts_cluster_id or 0,
            migrate_type=kwargs["migrate_type"],
            migrate_topology=kwargs.get("migrate_topology", ""),
            ticket_id=kwargs.get("ticket_id", 0),
            root_id=kwargs.get("root_id") or global_data.get("root_id", ""),
            status=MysqlDtsStatus.FullOnline.value,
            sync_scope_snapshot=sync_scope_snapshot,
            dts_task_config_snapshot=asdict(task_spec.dts_task_config),
            temp_account_snapshot=temp_account_snapshot,
            dts_task_id=kwargs["task_name"],
            dts_source_names=registered_source_names,
            creator=kwargs.get("creator", ""),
            updater=kwargs.get("creator", ""),
        )
        trans_data.migrate_context.created_dts_info_ids.append(dts_info.id)
        data.outputs["trans_data"] = trans_data
        data.outputs.dts_info_id = dts_info.id
        self.log_info(_("创建 MySQL DTS 迁移记录成功: id={}").format(dts_info.id))
        return True


class MysqlDtsUpdateMetaComponent(Component):
    name = __name__
    code = "mysql_dts_update_meta"
    bound_service = MysqlDtsUpdateMetaService
