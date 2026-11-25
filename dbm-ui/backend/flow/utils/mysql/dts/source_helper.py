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

from backend.db_meta.models import Cluster, StorageInstance


def require_dump_from_echo(want, got) -> None:
    """请求带了导出从库时，返回里必须还是同一台。密码回包会被打码，不比。"""
    if want is None:
        return
    same = got is not None and got.host == want.host and int(got.port) == int(want.port) and got.user == want.user
    if not same:
        raise ValueError(_("创建 source 的返回结果里没有导出地址 {}:{}").format(want.host, want.port))


def storage_by_instance_id(cluster: Cluster, instance_id: int) -> StorageInstance:
    try:
        return StorageInstance.objects.get(id=instance_id, cluster=cluster)
    except StorageInstance.DoesNotExist as exc:
        raise ValueError(_("集群 {} 的源实例 {} 不存在").format(cluster.id, instance_id)) from exc


def storage_by_instance_role(cluster: Cluster, role: str) -> StorageInstance:
    try:
        return cluster.storageinstance_set.get(instance_role=role)
    except StorageInstance.DoesNotExist as exc:
        raise ValueError(_("集群 {} 未找到角色为 {} 的源实例").format(cluster.id, role)) from exc
    except StorageInstance.MultipleObjectsReturned as exc:
        raise ValueError(_("集群 {} 的角色 {} 匹配到多条源实例").format(cluster.id, role)) from exc
