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
from datetime import datetime
from typing import Any, Dict

from django.db import models
from django.db.models import Q
from django.utils.timezone import localtime
from django.utils.translation import gettext_lazy as _

from backend.bk_web.constants import LEN_LONG, LEN_NORMAL, LEN_SHORT
from backend.bk_web.models import AuditedModel
from backend.db_meta.exceptions import ClusterExclusiveOperateException
from blue_krill.data_types.enum import EnumField, StrStructuredEnum

logger = logging.getLogger("root")


class MysqlDtsClusterStatus(StrStructuredEnum):
    DEPLOYING = EnumField("deploying", _("deploying"))
    RUNNING = EnumField("running", _("running"))
    STOPPED = EnumField("stopped", _("stopped"))
    DESTROYING = EnumField("destroying", _("destroying"))
    DESTROYED = EnumField("destroyed", _("destroyed"))
    ERROR = EnumField("error", _("error"))


class MysqlDtsStatus(StrStructuredEnum):
    """MySQL DTS 迁移记录状态（独立于 SQL Server DtsStatus）。"""

    ToDo = EnumField("todo", _("待执行"))
    Terminated = EnumField("terminated", _("已终止"))
    Disconnecting = EnumField("disconnecting", _("中断中"))
    Disconnected = EnumField("disconnected", _("已断开"))

    FullOnline = EnumField("full_online", _("全量传输中"))
    FullFailed = EnumField("full_failed", _("全量传输失败"))
    FullSuccess = EnumField("full_success", _("全量传输完成"))

    IncrOnline = EnumField("incr_online", _("增量传输中"))
    IncrFailed = EnumField("incr_failed", _("增量传输失败"))
    IncrSuccess = EnumField("incr_success", _("增量传输完成"))


class MysqlDtsCluster(AuditedModel):
    """MySQL DTS 集群业务表"""

    name = models.CharField(_("集群名称"), max_length=LEN_NORMAL)
    bk_biz_id = models.IntegerField(_("业务ID"), default=0)
    bk_cloud_id = models.IntegerField(_("云区域ID"), default=0)
    cluster_id = models.IntegerField(
        _("DBM Cluster ID"),
        default=0,
        help_text=_("已废弃，恒为 0；仅兼容历史数据（曾关联 ClusterType.MySQLDTS）"),
    )
    status = models.CharField(
        _("状态"),
        max_length=LEN_SHORT,
        choices=MysqlDtsClusterStatus.get_choices(),
        default=MysqlDtsClusterStatus.DEPLOYING.value,
    )
    master_nodes = models.JSONField(_("Master节点列表"), default=list)
    worker_nodes = models.JSONField(_("Worker节点列表"), default=list)
    master_addr = models.CharField(_("dmctl入口"), max_length=LEN_LONG, default="")
    deploy_path = models.CharField(_("部署路径"), max_length=LEN_LONG, default="")
    version = models.CharField(_("版本"), max_length=LEN_NORMAL, default="")

    class Meta:
        verbose_name = verbose_name_plural = _("MySQL DTS集群表")


class MysqlDtsInfo(AuditedModel):
    """MySQL DTS 数据迁移记录表"""

    bk_biz_id = models.IntegerField(default=0, help_text=_("关联的业务id"))
    source_cluster_id = models.IntegerField(default=0, help_text=_("源集群ID"))
    target_cluster_id = models.IntegerField(default=0, help_text=_("目标集群ID"))
    dts_cluster_id = models.IntegerField(default=0, help_text=_("DTS集群ID"))
    migrate_type = models.CharField(max_length=LEN_SHORT, default="", help_text=_("迁移类型"))
    migrate_topology = models.CharField(max_length=LEN_SHORT, default="", help_text=_("迁移拓扑"))
    ticket_id = models.PositiveIntegerField(default=0, help_text=_("关联的单据id"))
    root_id = models.CharField(max_length=64, default="", help_text=_("关联root_id"))
    status = models.CharField(
        max_length=64,
        choices=MysqlDtsStatus.get_choices(),
        default=MysqlDtsStatus.ToDo.value,
        help_text=_("状态"),
    )
    sync_scope_snapshot = models.JSONField(default=dict, help_text=_("同步范围快照"))
    dts_task_config_snapshot = models.JSONField(default=dict, help_text=_("任务配置快照"))
    # 临时账号快照（不含密码），供终态 drop_user 使用：
    # {"user": "dts_migrate_xxx", "grant_hosts": [...], "grant_targets": [{"bk_cloud_id", "address", ...}]}
    temp_account_snapshot = models.JSONField(default=dict, help_text=_("DTS临时账号快照(不含密码)"))
    dts_task_id = models.CharField(max_length=LEN_NORMAL, default="", help_text=_("DTS任务ID"))
    dts_source_names = models.JSONField(default=list, help_text=_("DTS Source名称列表"))

    class Meta:
        verbose_name = verbose_name_plural = _("MySQL DTS数据迁移记录表")

    def to_dict(self):
        opts = self._meta
        data = {}
        for field in opts.concrete_fields:
            value = field.value_from_object(self)
            if isinstance(value, datetime):
                value = localtime(value).isoformat(timespec="seconds")
            elif isinstance(field, models.FileField):
                value = value.url if value else None
            data[field.name] = value
        return data

    @classmethod
    def dts_info_clusive(cls, ticket_id: int, ticket_type: str, details: Dict[str, Any]):
        # 延迟导入，避免 models ↔ ticket.builders 循环依赖
        from backend.ticket.builders.common.base import fetch_cluster_ids
        from backend.ticket.constants import TicketType

        cluster_ids = fetch_cluster_ids(details=details)
        active_status = [
            MysqlDtsStatus.Disconnecting,
            MysqlDtsStatus.Disconnected,
            MysqlDtsStatus.FullOnline,
            MysqlDtsStatus.IncrOnline,
        ]
        dts_infos = cls.objects.filter(
            (Q(source_cluster_id__in=cluster_ids) | Q(target_cluster_id__in=cluster_ids)) & Q(status__in=active_status)
        ).exclude(ticket_id=ticket_id)
        for dts_info in dts_infos:
            raise ClusterExclusiveOperateException(
                _("当前操作「{}(单据：{})」与迁移记录(关联单据：{})存在执行互斥").format(
                    TicketType.get_choice_label(ticket_type), ticket_id, dts_info.ticket_id
                )
            )
