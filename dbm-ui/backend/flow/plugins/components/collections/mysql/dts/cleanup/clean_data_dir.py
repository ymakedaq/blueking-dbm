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
from backend.flow.utils.mysql.dts.script_template import clean_mysql_dts_data_dir_template

logger = logging.getLogger("flow")
env = Environment()


class MysqlDtsCleanDataDirService(MysqlDtsExecShellService):
    """清理 DTS 部署目录。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        shell_script = env.from_string(clean_mysql_dts_data_dir_template).render(
            deploy_path=kwargs["deploy_path"],
        )
        kwargs["shell_script"] = shell_script
        self.log_info(_("清理 DTS 部署目录: {}").format(kwargs["deploy_path"]))
        return super()._execute(data, parent_data)


class MysqlDtsCleanDataDirComponent(Component):
    name = __name__
    code = "mysql_dts_clean_data_dir"
    bound_service = MysqlDtsCleanDataDirService
