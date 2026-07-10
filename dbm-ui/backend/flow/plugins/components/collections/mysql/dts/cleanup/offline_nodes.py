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
from backend.flow.plugins.components.collections.common.base_service import BaseService

logger = logging.getLogger("flow")


class MysqlDtsOfflineNodesService(BaseService):
    """从 DTS Master 下线 Worker / Master 节点注册信息。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        master_addr = kwargs.get("master_addr")
        force_destroy = kwargs.get("force_destroy", False)
        worker_nodes = kwargs.get("worker_nodes") or []
        master_nodes = kwargs.get("master_nodes") or []
        if not master_addr:
            self.log_warning(_("master_addr 为空，跳过 offline_worker/offline_master"))
            return True

        for node in worker_nodes:
            worker_name = node.get("name") or node.get("worker_name")
            if not worker_name:
                continue
            try:
                MySQLDTSApi.offline_worker(master_addr, worker_name)
                self.log_info(_("下线 Worker 成功: {}").format(worker_name))
            except Exception as exc:  # pylint: disable=broad-except
                if force_destroy:
                    self.log_warning(_("强制清理：下线 Worker {} 失败: {}").format(worker_name, exc))
                else:
                    self.log_error(_("下线 Worker {} 失败: {}").format(worker_name, exc))
                    return False

        for node in master_nodes:
            master_name = node.get("name") or node.get("master_name")
            if not master_name:
                continue
            try:
                MySQLDTSApi.offline_master(master_addr, master_name)
                self.log_info(_("下线 Master 成功: {}").format(master_name))
            except Exception as exc:  # pylint: disable=broad-except
                if force_destroy:
                    self.log_warning(_("强制清理：下线 Master {} 失败: {}").format(master_name, exc))
                else:
                    self.log_error(_("下线 Master {} 失败: {}").format(master_name, exc))
                    return False
        return True


class MysqlDtsOfflineNodesComponent(Component):
    name = __name__
    code = "mysql_dts_offline_nodes"
    bound_service = MysqlDtsOfflineNodesService
