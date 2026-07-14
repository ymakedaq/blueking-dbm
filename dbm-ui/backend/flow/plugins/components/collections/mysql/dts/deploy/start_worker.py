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
from jinja2.sandbox import SandboxedEnvironment as Environment
from pipeline.component_framework.component import Component

from backend.flow.plugins.components.collections.mysql.dts.base_shell import MysqlDtsExecShellService
from backend.flow.utils.mysql.dts.constants import MYSQL_DTS_WORKER_PORT
from backend.flow.utils.mysql.dts.package_resolver import resolve_dts_pkg_name
from backend.flow.utils.mysql.dts.script_template import start_mysql_dts_worker_template

logger = logging.getLogger("flow")
env = Environment()


class MysqlDtsStartWorkerService(MysqlDtsExecShellService):
    """启动 dm-worker 进程。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        trans_data = data.get_one_of_inputs("trans_data")
        pkg_name = resolve_dts_pkg_name(kwargs, trans_data)
        if not pkg_name:
            self.log_error(_("无法解析 DTS 介质包名，拒绝启动 Worker"))
            return False

        shell_script = env.from_string(start_mysql_dts_worker_template).render(
            deploy_path=kwargs["deploy_path"],
            pkg_name=pkg_name,
            config_file=kwargs["config_file"],
            node_name=kwargs["node_name"],
            listen_port=kwargs.get("listen_port", MYSQL_DTS_WORKER_PORT),
        )
        kwargs["shell_script"] = shell_script
        self.log_info(_("启动 dm-worker {}，介质包={}").format(kwargs["node_name"], pkg_name))
        return super()._execute(data, parent_data)


class MysqlDtsStartWorkerComponent(Component):
    name = __name__
    code = "mysql_dts_start_worker"
    bound_service = MysqlDtsStartWorkerService
