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
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from django.utils.crypto import get_random_string

from backend.flow.utils.mysql.dts.constants import MYSQL_DTS_MIGRATE_USER_PREFIX

if TYPE_CHECKING:
    from backend.flow.utils.mysql.dts.context import MysqlDtsDropUserSubflowInput, MysqlDtsMigrateContext


@dataclass(frozen=True)
class DtsGrantTarget:
    bk_cloud_id: int
    address: str
    cluster_id: int


def generate_dts_migrate_credentials() -> tuple[str, str]:
    """生成迁移用临时账号，由 Flow 内部使用，不暴露给提单页。"""
    user = "{}{}".format(MYSQL_DTS_MIGRATE_USER_PREFIX, get_random_string(length=8).lower())
    password = get_random_string(length=16)
    return user, password


def grant_targets_to_dicts(targets: list[DtsGrantTarget]) -> list[dict]:
    return [asdict(t) for t in targets]


def build_temp_account_snapshot(
    *,
    dts_user: str,
    grant_hosts: list[str],
    grant_targets: list[DtsGrantTarget] | list[dict],
) -> dict:
    """构建可落库/可回放的临时账号快照（不含密码）。"""
    targets: list[dict] = []
    for item in grant_targets:
        if isinstance(item, DtsGrantTarget):
            targets.append(asdict(item))
        else:
            targets.append(dict(item))
    return {
        "user": dts_user,
        "grant_hosts": list(grant_hosts),
        "grant_targets": targets,
    }


def build_drop_user_subflow_input_from_context(
    *,
    root_id: str,
    bk_biz_id: int,
    migrate_context: "MysqlDtsMigrateContext",
    ignore_errors: bool = True,
    creator: str = "",
) -> "MysqlDtsDropUserSubflowInput | None":
    """从 migrate_context 组装 drop_user 子流程入参；信息不完整时返回 None。"""
    from backend.flow.utils.mysql.dts.context import MysqlDtsDropUserSubflowInput

    if not migrate_context.dts_user or not migrate_context.grant_hosts or not migrate_context.grant_targets:
        return None
    return MysqlDtsDropUserSubflowInput(
        root_id=root_id,
        bk_biz_id=bk_biz_id,
        dts_user=migrate_context.dts_user,
        grant_hosts=list(migrate_context.grant_hosts),
        grant_targets=list(migrate_context.grant_targets),
        ignore_errors=ignore_errors,
        creator=creator,
    )


def build_drop_user_subflow_input_from_snapshot(
    *,
    root_id: str,
    bk_biz_id: int,
    snapshot: dict,
    ignore_errors: bool = True,
    creator: str = "",
) -> "MysqlDtsDropUserSubflowInput | None":
    """从 temp_account_snapshot 组装 drop_user 子流程入参。"""
    from backend.flow.utils.mysql.dts.context import MysqlDtsDropUserSubflowInput

    user = snapshot.get("user") or ""
    grant_hosts = snapshot.get("grant_hosts") or []
    grant_targets = snapshot.get("grant_targets") or []
    if not user or not grant_hosts or not grant_targets:
        return None
    return MysqlDtsDropUserSubflowInput(
        root_id=root_id,
        bk_biz_id=bk_biz_id,
        dts_user=user,
        grant_hosts=list(grant_hosts),
        grant_targets=list(grant_targets),
        ignore_errors=ignore_errors,
        creator=creator,
    )
