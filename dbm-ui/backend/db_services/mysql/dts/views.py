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
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from backend.bk_web import viewsets
from backend.bk_web.swagger import common_swagger_auto_schema
from backend.db_meta.models import Cluster, MysqlDtsCluster
from backend.db_meta.models.mysql_dts import MysqlDtsInfo
from backend.db_services.mysql.dts.handlers import MySQLDtsMigrateHandler
from backend.db_services.mysql.dts.serializers import (
    ForceFailedMigrateSerializer,
    MigratePreviewSerializer,
    QueryMigrateRecordsResponseSerializer,
    QueryMigrateRecordsSerializer,
)
from backend.iam_app.handlers.drf_perm.base import DBManagePermission

SWAGGER_TAG = "db_services/mysql/dts"


class MySQLDtsMigrateViewSet(viewsets.SystemViewSet):
    default_permission_class = [DBManagePermission()]

    @common_swagger_auto_schema(
        operation_summary=_("获取迁移记录"),
        query_serializer=QueryMigrateRecordsSerializer(),
        tags=[SWAGGER_TAG],
        responses={status.HTTP_200_OK: QueryMigrateRecordsResponseSerializer()},
    )
    @action(methods=["GET"], detail=False, serializer_class=QueryMigrateRecordsSerializer)
    def query_migrate_records(self, request, bk_biz_id):
        self.params_validate(self.get_serializer_class())
        records = MysqlDtsInfo.objects.filter(bk_biz_id=bk_biz_id).order_by("-create_at").values()
        cluster_ids = set()
        for record in records:
            cluster_ids.add(record["source_cluster_id"])
            cluster_ids.add(record["target_cluster_id"])
        clusters = Cluster.objects.filter(id__in=cluster_ids)
        domain_map = {c.id: c.immute_domain for c in clusters}
        result = []
        for record in records:
            item = dict(record)
            item["source_domain"] = domain_map.get(record["source_cluster_id"], "")
            item["target_domain"] = domain_map.get(record["target_cluster_id"], "")
            result.append(item)
        return Response(result)

    @common_swagger_auto_schema(
        operation_summary=_("强制终止迁移"),
        request_body=ForceFailedMigrateSerializer(),
        tags=[SWAGGER_TAG],
    )
    @action(methods=["POST"], detail=False, serializer_class=ForceFailedMigrateSerializer)
    def force_failed_migrate(self, request, *args, **kwargs):
        data = self.params_validate(self.get_serializer_class())
        MySQLDtsMigrateHandler.force_failed_migrate(dts_id=data["dts_id"])
        return Response()

    @common_swagger_auto_schema(
        operation_summary=_("迁移计划预览"),
        request_body=MigratePreviewSerializer(),
        tags=[SWAGGER_TAG],
    )
    @action(methods=["POST"], detail=False, serializer_class=MigratePreviewSerializer)
    def preview(self, request, *args, **kwargs):
        data = self.params_validate(self.get_serializer_class())
        plan = MySQLDtsMigrateHandler.preview_migrate_plan(data)
        return Response({"task_specs": [spec.__dict__ for spec in plan.task_specs]})

    @common_swagger_auto_schema(
        operation_summary=_("查询 DTS 集群列表"),
        tags=[SWAGGER_TAG],
    )
    @action(methods=["GET"], detail=False)
    def list_dts_clusters(self, request, bk_biz_id):
        clusters = MysqlDtsCluster.objects.filter(bk_biz_id=bk_biz_id).order_by("-create_at")
        return Response([c.__dict__ for c in clusters])
