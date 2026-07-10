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

from backend.components import DBPrivManagerApi
from backend.db_meta.models import MysqlDtsCluster
from backend.flow.plugins.components.collections.common.base_service import BaseService
from backend.flow.utils.mysql.dts.migrate_credentials import generate_dts_migrate_credentials
from backend.flow.utils.mysql.dts.migrate_helper import collect_migrate_grant_targets
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan

logger = logging.getLogger("flow")

# Wiki: 源端读 + binlog；目标端写。一期共用临时账号，权限取并集。
# RELOAD 属于 GLOBAL 权限（见 dbpermission.constants.MySQLPrivType.GLOBAL）
_DTS_MIGRATE_DML_DDL_PRIV = (
    "SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, "
    "LOCK TABLES, CREATE VIEW, SHOW VIEW, TRIGGER, EVENT"
)
_DTS_MIGRATE_GLOBAL_PRIV = "REPLICATION SLAVE, REPLICATION CLIENT, PROCESS, RELOAD"


class MysqlDtsCreateUserService(BaseService):
    """在源/目标实例上创建 DTS 迁移临时账号。"""

    def _resolve_grant_hosts(self, trans_data) -> list[str]:
        worker_ips = [node["ip"] for node in (trans_data.deploy_context.deployed_worker_nodes or []) if node.get("ip")]
        if worker_ips:
            return sorted(set(worker_ips))

        dts_cluster_id = trans_data.migrate_context.dts_cluster_id
        if dts_cluster_id:
            dts_cluster = MysqlDtsCluster.objects.filter(id=dts_cluster_id).first()
            if dts_cluster:
                worker_ips = [node["ip"] for node in (dts_cluster.worker_nodes or []) if node.get("ip")]
                if worker_ips:
                    trans_data.deploy_context.deployed_worker_nodes = list(dts_cluster.worker_nodes)
                    return sorted(set(worker_ips))
        return []

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        global_data = data.get_one_of_inputs("global_data")
        trans_data = data.get_one_of_inputs("trans_data")
        plan: DtsMigratePlan = kwargs["migrate_plan"]

        if trans_data.migrate_context.dts_user and trans_data.migrate_context.dts_password:
            dts_user = trans_data.migrate_context.dts_user
            dts_password = trans_data.migrate_context.dts_password
        else:
            dts_user, dts_password = generate_dts_migrate_credentials()
            trans_data.migrate_context.dts_user = dts_user
            trans_data.migrate_context.dts_password = dts_password

        grant_hosts = self._resolve_grant_hosts(trans_data)
        if not grant_hosts:
            self.log_error(_("未解析到 DTS Worker IP，拒绝使用 %% 授权，请先确保 DTS 集群已部署"))
            return False

        grant_targets = collect_migrate_grant_targets(plan)
        if not grant_targets:
            self.log_error(_("未找到需要授权的迁移实例"))
            return False

        operator = global_data.get("created_by") or kwargs.get("creator", "")
        for target in grant_targets:
            try:
                DBPrivManagerApi.add_priv_without_account_rule(
                    params={
                        "bk_cloud_id": target.bk_cloud_id,
                        "bk_biz_id": global_data["bk_biz_id"],
                        "operator": operator,
                        "user": dts_user,
                        "psw": dts_password,
                        "hosts": grant_hosts,
                        "dbname": "%",
                        "dml_ddl_priv": _DTS_MIGRATE_DML_DDL_PRIV,
                        "global_priv": _DTS_MIGRATE_GLOBAL_PRIV,
                        "address": target.address,
                    }
                )
                self.log_info(_("在实例 {} 创建 DTS 迁移临时用户 {} 成功").format(target.address, dts_user))
            except Exception as exc:  # pylint: disable=broad-except
                self.log_error(_("在实例 {} 创建 DTS 迁移用户失败: {}").format(target.address, exc))
                return False

        # 写入授权快照，供后续 drop_user 子流程使用（不含密码）
        from backend.flow.utils.mysql.dts.migrate_credentials import grant_targets_to_dicts

        trans_data.migrate_context.grant_hosts = list(grant_hosts)
        trans_data.migrate_context.grant_targets = grant_targets_to_dicts(grant_targets)
        data.outputs["trans_data"] = trans_data
        return True


class MysqlDtsCreateUserComponent(Component):
    name = __name__
    code = "mysql_dts_create_user"
    bound_service = MysqlDtsCreateUserService
