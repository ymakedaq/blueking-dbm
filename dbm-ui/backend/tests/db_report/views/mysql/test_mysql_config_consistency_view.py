# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing limitations under the License.
"""
import pytest

from backend.configuration.constants import DBType
from backend.db_meta.enums import ClusterType
from backend.db_report.enums import ReportType
from backend.db_report.register import db_report_maps

pytestmark = pytest.mark.django_db


def test_config_consistency_registered_on_mysql_and_tendbcluster():
    mysql_types = [cls.report_type for cls in db_report_maps[DBType.MySQL]]
    tendb_types = [cls.report_type for cls in db_report_maps[DBType.TenDBCluster]]
    assert ReportType.CONFIG_CONSISTENCY_CHECK in mysql_types
    assert ReportType.CONFIG_CONSISTENCY_CHECK in tendb_types


def test_config_consistency_enum_label():
    choice_map = dict(ReportType.get_choices())
    assert choice_map[ReportType.CONFIG_CONSISTENCY_CHECK.value] == "配置一致性巡检"


def test_mysql_queryset_excludes_tendbcluster():
    from backend.db_report.views.mysql.mysql_config_consistency_view import MysqlConfigConsistencyReportViewSet

    sql = str(MysqlConfigConsistencyReportViewSet.queryset.query)
    assert ClusterType.TenDBSingle.value in sql
    assert ClusterType.TenDBHA.value in sql
    assert f"'{ClusterType.TenDBCluster.value}'" not in sql


def test_tendbcluster_queryset_is_tendbcluster_only():
    from backend.db_report.views.tendbcluster.tendbcluster_config_consistency_view import (
        TendbClusterConfigConsistencyReportViewSet,
    )

    sql = str(TendbClusterConfigConsistencyReportViewSet.queryset.query)
    assert ClusterType.TenDBCluster.value in sql
    assert ClusterType.TenDBSingle.value not in sql
    assert ClusterType.TenDBHA.value not in sql
