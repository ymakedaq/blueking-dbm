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
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Union

from django.utils import timezone
from django.utils.translation import gettext as _

from backend.components.dbresource.client import DBResourceApi
from backend.db_meta.models import Cluster
from backend.db_periodic_task.models import MySQLBackupRollbackTask
from backend.db_services.dbresource.exceptions import ResourceApplyException, ResourceApplyInsufficientException
from backend.db_services.mysql.fixpoint_rollback.handlers import FixPointRollbackHandler
from backend.flow.engine.bamboo.scene.mysql.mysql_rollback_exercise import MySQLRollbackExerciseFlow
from backend.ticket.constants import ResourceApplyErrCode
from backend.utils.basic import generate_root_id

logger = logging.getLogger("root")


# 定义任务状态的常量
class TaskStatus:
    GENERATED = "generated"  # 已生成任务
    RESOURCE_APPLIED = "resource_applied"  # 已申请资源
    RESOURCE_APPLIED_FAILED = "resource_applied_failed"
    COMMIT_SUCCESS = "commit_success"  # 提交单据成功


# 查询备份记录生成回档任务
def gen_rollback_task():
    # 先获取从未进行过回档演练的业务下的集群
    exclude_biz_ids = MySQLBackupRollbackTask.get_all_practiced_biz_ids()
    exclude_cluster_id = MySQLBackupRollbackTask.get_all_practiced_cluster_ids()
    clusters = Cluster.objects.exclude(
        bk_biz_id__in=exclude_biz_ids,
        id__in=exclude_cluster_id,
    )
    if not clusters:
        # 如果都演练过的话,则选择没有演练过的集群
        clusters = Cluster.objects.exclude(id__in=exclude_cluster_id)
        # 每个业务保留一个演练集群
    # 打乱clusters顺序
    clusters = list(clusters)
    random.shuffle(clusters)
    for cluster in clusters[:5]:
        handler = FixPointRollbackHandler(cluster.id, check_full_backup=True)
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=7)
        backup_records = handler.query_backup_log_from_bklog(start_time, end_time)
        if not backup_records:
            continue
        backup_records.sort(key=lambda x: x["backup_time"], reverse=False)
        # 选择第一个备份记录生成回档任务
        backup_record = backup_records[0]
        if not backup_record:
            logger.info("no backup record found")
            continue
        backup_id = backup_record["backup_id"]
        task = MySQLBackupRollbackTask(
            bk_biz_id=backup_record["bk_biz_id"],
            cluster_id=cluster.id,
            cluster_domain=backup_record.get("cluster_address", ""),
            cluster_type=cluster.cluster_type,
            backup_id=backup_id,
            backup_begin_time=backup_record["backup_begin_time"],
            backup_end_time=backup_record["backup_end_time"],
            backup_total_size=backup_record["backup_total_size"],
            backup_type=backup_record["backup_type"],
            backup_tool=backup_record["backup_tool"],
            time_zone=backup_record["total_filesize"],
            task_status=TaskStatus.GENERATED,
            created_by="system",
            created_at=datetime.now(),
            updated_by="system",
            updated_at=datetime.now(),
        )
        task.save()
        # 申请资源
        apply_params: Dict[str, Union[str, Any]] = {
            "for_biz_id": 0,
            "resource_type": "mysql",
            # 消费情况下的task id为inner flow
            "task_id": "xxx",
            "operator": "system",
            "details": {
                "count": 1,
                "group_mark": "backup_recovery_exercise_0",
                # 演练专属资源标签
                "lables": "111",
            },
        }
        resp = DBResourceApi.resource_apply(params=apply_params, raw=True)
        if resp["code"] != 0:
            task.update(task_status=TaskStatus.RESOURCE_APPLIED_FAILED)
            if resp["code"] == ResourceApplyErrCode.RESOURCE_LAKE:
                raise ResourceApplyInsufficientException(_("资源不足申请失败，请前往补货后重试{}").format(resp.get("message")))
            elif resp["code"] in ResourceApplyErrCode.get_values():
                raise ResourceApplyException(
                    _("资源池服务出现系统错误，请联系管理员或稍后重试。错误信息: [{}]{}").format(
                        ResourceApplyErrCode.get_choice_label(resp["code"]), resp.get("message")
                    )
                )
            else:
                raise ResourceApplyException(
                    _("资源池相关服务出现未知异常，请联系管理员处理。错误信息: [{}]{}").format(resp["code"], resp.get("message"))
                )
        else:
            task.update(task_status=TaskStatus.RESOURCE_APPLIED)
        resource_request_id, apply_data = resp["request_id"], resp["data"]
        logger.info(f"resource_request_id: {resource_request_id}, apply_data: {apply_data}")
        mch_info = apply_data[0]["data"]
        rollback_host = {
            "ip": mch_info["ip"],
            "bk_host_id": mch_info["bk_host_id"],
            "bk_cloud_id": mch_info["bk_cloud_id"],
        }
        # 提交演练任务
        flow_context = {
            "ticket_type": "MYSQL_ROLLBACK_EXERCISE",
            "exercise_cluster_id": cluster.id,
            "backup_id": backup_id,
            "rollback_ip": rollback_host,
            "bk_biz_id": 123,
            "backupinfo": backup_record,
        }
        root_id = generate_root_id()
        flow = MySQLRollbackExerciseFlow(root_id=root_id, data=flow_context)
        flow.run()
