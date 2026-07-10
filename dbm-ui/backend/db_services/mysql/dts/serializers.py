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
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers


class QueryMigrateRecordsSerializer(serializers.Serializer):
    pass


class QueryMigrateRecordsResponseSerializer(serializers.Serializer):
    pass


class ForceFailedMigrateSerializer(serializers.Serializer):
    dts_id = serializers.IntegerField(help_text=_("迁移记录ID"))


class MigratePreviewSerializer(serializers.Serializer):
    migrate_topology = serializers.CharField()
    one_to_one = serializers.DictField(required=False)
    many_to_one = serializers.DictField(required=False)
    one_to_many = serializers.DictField(required=False)
    dts_task_config = serializers.DictField(required=False)
