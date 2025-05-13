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
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Union

from django.utils.translation import gettext as _

from backend.components.dbresource.client import DBResourceApi
from backend.db_meta.models import Cluster
from backend.db_periodic_task.models import MySQLBackupRollbackTask
from backend.db_services.dbresource.exceptions import ResourceApplyException, ResourceApplyInsufficientException
from backend.db_services.mysql.fixpoint_rollback.handlers import FixPointRollbackHandler
from backend.ticket.constants import ResourceApplyErrCode


# 定义任务状态的常量
class TaskStatus:
    GENERATED = "generated"  # 已生成任务
    RESOURCE_APPLIED = "resource_applied"  # 已申请资源
    RESOURCE_APPLIED_FAILED = "resource_applied_failed"
    COMMIT_SUCCESS = "commit_success"  # 提交单据成功


# 查询备份记录生成回档任务
def gen_rollback_task():
    # 先获取从未进行过回档演练的业务下的集群
    clusters = Cluster.objects.filter(
        bk_biz_id__not_in=MySQLBackupRollbackTask.get_all_practiced_biz_ids,
        id__not_in=MySQLBackupRollbackTask.get_all_practiced_cluster_ids,
    )
    if not clusters:
        # 如果都演练过的话,则选择没有演练过的集群
        clusters = Cluster.objects.filter(id__not_in=MySQLBackupRollbackTask.get_all_practiced_cluster_ids)
        # 每个业务保留一个演练集群
    # 打乱clusters顺序
    clusters = list(clusters)
    random.shuffle(clusters)
    for cluster in clusters[:5]:
        handler = FixPointRollbackHandler(cluster.id, check_full_backup=True)
        start_time = datetime.now() - timedelta(days=7)
        end_time = datetime.now()
        backup_records = handler.query_backup_log_from_bklog(start_time, end_time)
        if not backup_records:
            continue
        backup_records.sort(key=lambda x: x["backup_time"], reverse=False)
        # 选择第一个备份记录生成回档任务
        backup_record = backup_records[0]
        task = MySQLBackupRollbackTask(
            bk_biz_id=cluster.bk_biz_id,
            cluster_id=cluster.id,
            cluster_domain=backup_record.get("cluster_address", ""),
            cluster_type=cluster.cluster_type,
            backup_id=backup_record["backup_id"],
            backup_begin_time=backup_record["backup_begin_time"],
            backup_end_time=backup_record["backup_end_time"],
            time_zone=backup_record["time_zone"],
            task_status=TaskStatus.GENERATED,
            created_by="system",
            created_at=datetime.now(),
            updated_by="system",
            updated_at=datetime.now(),
        )
        task.save()
    # 申请资源

    # 向资源池申请机器
    apply_params: Dict[str, Union[str, Any]] = {
        "for_biz_id": 0,
        "resource_type": "mysql",
        # 消费情况下的task id为inner flow
        "task_id": "xxx",
        "operator": "system",
        "details": {
            "count": 1,
            "group_mark": "rollback_exercise_0",
            # 演练专属资源标签
            "lables": "111",
        },
    }
    resp = DBResourceApi.resource_apply(params=apply_params, raw=True)
    if resp["code"] == ResourceApplyErrCode.RESOURCE_LAKE:
        # 如果是资源不足，则创建补货单，用户手动处理后可以重试资源申请
        task.update(task_status=TaskStatus.RESOURCE_APPLIED_FAILED)
        raise ResourceApplyInsufficientException(_("资源不足申请失败，请前往补货后重试{}").format(resp.get("message")))
    elif resp["code"] in ResourceApplyErrCode.get_values():
        raise ResourceApplyException(
            _("资源池服务出现系统错误，请联系管理员或稍后重试。错误信息: [{}]{}").format(
                ResourceApplyErrCode.get_choice_label(resp["code"]), resp.get("message")
            )
        )
    elif resp["code"] != 0:
        raise ResourceApplyException(
            _("资源池相关服务出现未知异常，请联系管理员处理。错误信息: [{}]{}").format(resp["code"], resp.get("message"))
        )
