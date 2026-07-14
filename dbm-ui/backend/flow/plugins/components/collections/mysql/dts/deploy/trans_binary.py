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

from backend.flow.engine.bamboo.scene.common.get_file_list import GetFileList
from backend.flow.models import FlowNode
from backend.flow.plugins.components.collections.mysql.trans_flies import TransFileService
from backend.flow.utils.mysql.dts.package_resolver import resolve_mysql_dts_package

logger = logging.getLogger("flow")


class MysqlDtsTransBinaryService(TransFileService):
    """通过 V2 介质解析下发 MySQL DTS 二进制包。"""

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        trans_data = data.get_one_of_inputs("trans_data")

        exec_targets = kwargs.get("exec_targets") or []
        if not exec_targets:
            self.log_error(_("DTS 介质下发目标为空"))
            return False

        file_list, pkg_name = GetFileList.mysql_dts_deploy(pkg_id=kwargs.get("dts_pkg_id"))
        pkg = resolve_mysql_dts_package(pkg_id=kwargs.get("dts_pkg_id"))

        kwargs["file_list"] = file_list
        kwargs["exec_ip"] = [t["ip"] for t in exec_targets]
        kwargs["bk_cloud_id"] = kwargs.get("bk_cloud_id", exec_targets[0]["bk_cloud_id"])
        kwargs.setdefault("file_target_path", "/data/install")

        if trans_data is not None and hasattr(trans_data, "deploy_context"):
            trans_data.deploy_context.pkg_name = pkg_name
            if pkg.db_version:
                trans_data.deploy_context.dts_version = pkg.db_version.full_version
            # 必须写入 outputs，否则下一节点拿不到 pkg_name（schedule 成功路径不会回写 trans_data）
            data.outputs["trans_data"] = trans_data

        self.log_info(_("DTS 介质包: name={}, path={}").format(pkg_name, pkg.path))

        root_id = kwargs["root_id"]
        node_id = kwargs["node_id"]
        FlowNode.objects.filter(root_id=root_id, node_id=node_id).update(hosts=[t["ip"] for t in exec_targets])

        result = super()._execute(data, parent_data)
        if result:
            data.outputs.exec_ips = [{"ip": t["ip"], "bk_cloud_id": t["bk_cloud_id"]} for t in exec_targets]
            # TransFile 父类 execute/schedule 可能覆盖 outputs，再次确保 pkg_name 落盘
            if trans_data is not None and hasattr(trans_data, "deploy_context"):
                data.outputs["trans_data"] = trans_data
        return result

    def _schedule(self, data, parent_data, callback_data=None) -> bool:
        # 先取出 execute 阶段写入的 pkg_name，避免父类 schedule 成功路径不回写 trans_data
        trans_data = data.get_one_of_outputs("trans_data") or data.get_one_of_inputs("trans_data")
        result = super()._schedule(data, parent_data, callback_data)
        if result and trans_data is not None and hasattr(trans_data, "deploy_context"):
            data.outputs["trans_data"] = trans_data
        return result


class MysqlDtsTransBinaryComponent(Component):
    name = __name__
    code = "mysql_dts_trans_binary"
    bound_service = MysqlDtsTransBinaryService
