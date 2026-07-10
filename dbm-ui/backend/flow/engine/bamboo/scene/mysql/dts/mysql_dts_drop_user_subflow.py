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
from django.utils.translation import gettext as _

from backend.flow.engine.bamboo.scene.common.builder import SubBuilder
from backend.flow.plugins.components.collections.mysql.dts.migrate.drop_user import MysqlDtsDropUserComponent
from backend.flow.utils.mysql.dts.context import MysqlDtsDropUserSubflowInput


def mysql_dts_drop_user_subflow(inp: MysqlDtsDropUserSubflowInput) -> SubBuilder:
    """删除 DTS 迁移临时账号子流程。

    仅负责在源/目标实例上 DROP USER `dts_user`@`grant_host`。
    本文件不主动挂到任何业务 Flow；调用方自行决定时机。

    ---------------------------------------------------------------------------
    【备忘】推荐挂载点（当前均为空，待业务侧接入）
    ---------------------------------------------------------------------------
    1. 断开同步 / 停止迁移单据或 API（主路径）
    2. mysql_dts_cleanup_subflow：stop_tasks 之后（DESTROY）
    3. mysql_dts_migrate_handler._finalize_ephemeral_dts：终态兜底
    不要挂：mysql_dts_migrate_subflow 成功末尾（增量仍需账号）

    入参组装：
      - build_drop_user_subflow_input_from_context(migrate_context)
      - build_drop_user_subflow_input_from_snapshot(temp_account_snapshot)
    快照字段：MysqlDtsInfo.temp_account_snapshot（create/update_meta 已写入，不含密码）
    ---------------------------------------------------------------------------
    """
    sub = SubBuilder(
        root_id=inp.root_id,
        data={
            "bk_biz_id": inp.bk_biz_id,
            "creator": inp.creator,
        },
    )
    sub.add_act(
        act_name=_("删除 DTS 迁移临时账号 {}").format(inp.dts_user),
        act_component_code=MysqlDtsDropUserComponent.code,
        kwargs={
            "dts_user": inp.dts_user,
            "grant_hosts": inp.grant_hosts,
            "grant_targets": inp.grant_targets,
            "ignore_errors": inp.ignore_errors,
            "creator": inp.creator,
        },
    )
    return sub
