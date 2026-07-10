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
from backend.flow.utils.mysql.dts.script_template import push_mysql_dts_config_template

logger = logging.getLogger("flow")
env = Environment()


class MysqlDtsPushConfigService(MysqlDtsExecShellService):
    """渲染并推送 DTS 节点配置文件。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        shell_script = env.from_string(push_mysql_dts_config_template).render(
            deploy_path=kwargs["deploy_path"],
            config_file=kwargs["config_file"],
            config_content=kwargs["config_content"],
        )
        kwargs["shell_script"] = shell_script
        self.log_info(_("推送 DTS 配置 {}").format(kwargs["config_file"]))
        return super()._execute(data, parent_data)


class MysqlDtsPushConfigComponent(Component):
    name = __name__
    code = "mysql_dts_push_config"
    bound_service = MysqlDtsPushConfigService
