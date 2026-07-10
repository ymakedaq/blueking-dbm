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

from backend.components import MySQLDTSApi
from backend.db_meta.models import MysqlDtsCluster
from backend.db_meta.models.mysql_dts import MysqlDtsInfo, MysqlDtsStatus
from backend.flow.utils.mysql.dts.migrate_plan import build_migrate_plan


class MySQLDtsMigrateHandler:
    @classmethod
    def force_failed_migrate(cls, dts_id: int):
        info = MysqlDtsInfo.objects.get(id=dts_id)
        dts_cluster = MysqlDtsCluster.objects.get(id=info.dts_cluster_id)
        if info.dts_task_id:
            try:
                MySQLDTSApi.stop_task(dts_cluster.master_addr, info.dts_task_id)
            except Exception as exc:  # pylint: disable=broad-except
                raise ValueError(_("停止 DTS 任务失败: {}").format(exc)) from exc
        info.status = MysqlDtsStatus.Terminated.value
        info.save(update_fields=["status", "update_at"])

    @classmethod
    def preview_migrate_plan(cls, ticket_details: dict):
        return build_migrate_plan(ticket_details)
