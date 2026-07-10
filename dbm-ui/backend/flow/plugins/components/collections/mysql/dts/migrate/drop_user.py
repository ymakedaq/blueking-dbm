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

from backend.components import DRSApi
from backend.flow.plugins.components.collections.common.base_service import BaseService

logger = logging.getLogger("flow")


def _is_ignorable_drop_error(error_msg: str) -> bool:
    """用户不存在等场景视为可忽略。"""
    if not error_msg:
        return False
    lower = error_msg.lower()
    keywords = [
        "can't drop",
        "operation drop user failed",
        "unknown user",
        "does not exist",
        "1396",  # ERROR 1396 (HY000): Operation DROP USER failed
    ]
    return any(k in lower for k in keywords)


class MysqlDtsDropUserService(BaseService):
    """在源/目标实例上删除 DTS 迁移临时账号（user@grant_host 笛卡尔积）。"""

    def _drop_one(self, *, address: str, bk_cloud_id: int, user: str, host: str, ignore_errors: bool) -> bool:
        sql = "drop user `{}`@`{}`;".format(user, host)
        try:
            resp = DRSApi.rpc(
                {
                    "addresses": [address],
                    "cmds": [sql],
                    "force": False,
                    "bk_cloud_id": bk_cloud_id,
                }
            )
            top_err = resp[0].get("error_msg") or ""
            cmd_err = ""
            if resp[0].get("cmd_results"):
                cmd_err = resp[0]["cmd_results"][0].get("error_msg") or ""
            err = top_err or cmd_err
            if err:
                if ignore_errors and _is_ignorable_drop_error(err):
                    self.log_warning(_("忽略删除临时用户失败 {}@{}@{}: {}").format(user, host, address, err))
                    return True
                self.log_error(_("在「{}」删除临时用户「{}@{}」失败: {}").format(address, user, host, err))
                return False
        except Exception as exc:  # pylint: disable=broad-except
            if ignore_errors:
                self.log_warning(_("删除临时用户接口异常(忽略) {}@{}@{}: {}").format(user, host, address, exc))
                return True
            self.log_error(_("删除临时用户接口异常: {}").format(exc))
            return False

        self.log_info(_("在「{}」删除临时用户「{}@{}」成功").format(address, user, host))
        return True

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        trans_data = data.get_one_of_inputs("trans_data")

        dts_user = kwargs.get("dts_user") or (
            getattr(getattr(trans_data, "migrate_context", None), "dts_user", "") if trans_data else ""
        )
        grant_hosts = kwargs.get("grant_hosts")
        if grant_hosts is None and trans_data is not None:
            grant_hosts = list(trans_data.migrate_context.grant_hosts or [])
        grant_targets = kwargs.get("grant_targets")
        if grant_targets is None and trans_data is not None:
            grant_targets = list(trans_data.migrate_context.grant_targets or [])
        ignore_errors = kwargs.get("ignore_errors", True)

        if not dts_user:
            self.log_error(_("dts_user 为空，无法删除临时账号"))
            return False
        if not grant_hosts:
            self.log_error(_("grant_hosts 为空，无法删除临时账号"))
            return False
        if not grant_targets:
            self.log_error(_("grant_targets 为空，无法删除临时账号"))
            return False

        ok = True
        for target in grant_targets:
            address = target.get("address")
            bk_cloud_id = target.get("bk_cloud_id")
            if not address or bk_cloud_id is None:
                self.log_error(_("grant_target 缺少 address/bk_cloud_id: {}").format(target))
                if not ignore_errors:
                    return False
                ok = False
                continue
            for host in grant_hosts:
                if not self._drop_one(
                    address=address,
                    bk_cloud_id=int(bk_cloud_id),
                    user=dts_user,
                    host=host,
                    ignore_errors=ignore_errors,
                ):
                    ok = False
                    if not ignore_errors:
                        return False

        if trans_data is not None and ok:
            # 清理 context 中的临时账号痕迹（保留 dts_user 便于审计日志关联）
            trans_data.migrate_context.grant_hosts = []
            trans_data.migrate_context.grant_targets = []
            data.outputs["trans_data"] = trans_data
        return ok if not ignore_errors else True


class MysqlDtsDropUserComponent(Component):
    name = __name__
    code = "mysql_dts_drop_user"
    bound_service = MysqlDtsDropUserService
