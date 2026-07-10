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

from backend.components import MySQLDTSApi
from backend.db_meta.models import MysqlDtsCluster
from backend.db_meta.models.mysql_dts import MysqlDtsInfo, MysqlDtsStatus
from backend.db_periodic_task.local_tasks.register import register_periodic_task

logger = logging.getLogger("celery")

_ACTIVE_DTS_STATUSES = [
    MysqlDtsStatus.FullOnline.value,
    MysqlDtsStatus.IncrOnline.value,
    MysqlDtsStatus.Disconnecting.value,
]


@register_periodic_task(run_every=60)
def poll_mysql_dts_migrate_status():
    """轮询 MySQL DTS 迁移任务状态并更新 MysqlDtsInfo。"""
    infos = MysqlDtsInfo.objects.filter(status__in=_ACTIVE_DTS_STATUSES, dts_task_id__gt="")
    for info in infos:
        try:
            dts_cluster = MysqlDtsCluster.objects.get(id=info.dts_cluster_id)
            task_status = MySQLDTSApi.get_task_status(dts_cluster.master_addr, info.dts_task_id)
            stage = ""
            if task_status.data:
                stage = task_status.data[0].stage or ""
            if "Full" in stage:
                info.status = MysqlDtsStatus.FullOnline.value
            elif "Incr" in stage or "Sync" in stage:
                info.status = MysqlDtsStatus.IncrOnline.value
            elif "Failed" in stage or "Error" in stage:
                info.status = MysqlDtsStatus.FullFailed.value
            info.save(update_fields=["status", "update_at"])
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(_("轮询 MySQL DTS 任务状态失败 id={}: {}").format(info.id, exc))
