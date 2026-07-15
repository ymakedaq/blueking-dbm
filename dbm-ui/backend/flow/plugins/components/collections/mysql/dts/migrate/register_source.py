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

from django.utils.translation import gettext as _
from pipeline.component_framework.component import Component

from backend.components import MySQLDTSApi
from backend.db_meta.models import Cluster
from backend.flow.plugins.components.collections.common.base_service import BaseService
from backend.flow.utils.mysql.dts.migrate_helper import build_create_source_request
from backend.flow.utils.mysql.dts.migrate_plan import DtsTaskSpec

logger = logging.getLogger("flow")


class MysqlDtsRegisterSourceService(BaseService):
    """为迁移任务注册 DTS Source。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        trans_data = data.get_one_of_inputs("trans_data")
        master_addr = kwargs.get("master_addr") or trans_data.migrate_context.master_addr
        if not master_addr:
            self.log_error(_("DTS master_addr 为空"))
            return False
        task_spec: DtsTaskSpec = kwargs["task_spec"]
        dts_user = trans_data.migrate_context.dts_user
        dts_password = trans_data.migrate_context.dts_password
        if not dts_user or not dts_password:
            self.log_error(_("DTS 迁移临时账号未创建，请先执行 create_user 步骤"))
            return False
        worker_name = kwargs.get("worker_name")

        registered = []
        target_cluster = Cluster.objects.get(id=task_spec.target_cluster_id)
        migrate_type = kwargs.get("migrate_type") or ""
        if not migrate_type and kwargs.get("migrate_plan") is not None:
            migrate_type = getattr(kwargs["migrate_plan"], "migrate_type", "") or ""

        for source_spec in task_spec.sources:
            cluster = Cluster.objects.get(id=source_spec.cluster_id)
            request = build_create_source_request(
                source_spec,
                cluster,
                user=dts_user,
                password=dts_password,
                worker_name=worker_name,
                target_cluster=target_cluster,
                migrate_type=migrate_type,
            )
            self.log_info(
                _("注册 Source {} enable_gtid={} (源集群={}, 目标集群={})").format(
                    source_spec.source_name,
                    request.source.enable_gtid,
                    cluster.id,
                    target_cluster.id,
                )
            )
            resp = MySQLDTSApi.create_source(master_addr, request)
            source_name = resp.source_name or source_spec.source_name
            registered.append(source_name)
            self.log_info(_("注册 DTS Source 成功: {}").format(source_name))

        existing = list(trans_data.migrate_context.registered_source_names or [])
        existing.extend(registered)
        trans_data.migrate_context.registered_source_names = existing
        data.outputs["trans_data"] = trans_data
        data.outputs.registered_source_names = registered
        return True


class MysqlDtsRegisterSourceComponent(Component):
    name = __name__
    code = "mysql_dts_register_source"
    bound_service = MysqlDtsRegisterSourceService
