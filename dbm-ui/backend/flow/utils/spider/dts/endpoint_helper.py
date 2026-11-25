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

from backend.db_meta.enums import ClusterType, InstanceRole
from backend.db_meta.models import Cluster
from backend.db_services.dbbase.constants import IP_PORT_DIVIDER
from backend.flow.utils.mysql.dts.constants import FullLoadEngine
from backend.flow.utils.mysql.dts.migrate_plan import SourceSpec
from backend.flow.utils.mysql.dts.source_helper import storage_by_instance_id, storage_by_instance_role
from backend.flow.utils.spider.dts.shard_helper import shard_remote_master, shard_remote_slave


def resolve_spider_master_endpoint(cluster: Cluster) -> tuple[str, int]:
    """TenDBCluster 源集群主库端点：取第一个 remote master（与既有 TenDBCluster 分支语义一致）。"""
    ins = cluster.storageinstance_set.filter(instance_role=InstanceRole.REMOTE_MASTER).first()
    if not ins:
        raise ValueError(_("集群 {} 未找到可用的 Remote Master 源实例").format(cluster.id))
    return ins.machine.ip, ins.port


def effective_spider_backup_locale(source_spec: SourceSpec, cluster: Cluster, full_load_engine: str = "") -> str:
    """TenDBCluster 源的导出节点语义：

    - myloader：全量来自备份，不生效，返回空
    - 显式 backup_locale：原样返回（dump 解析处再统一拒绝）
    - 分片 source（cluster_to_cluster）：默认 slave（各分片 remote slave）
    - 未展开的 TenDBCluster 源：保持兼容，返回空
    """
    if (full_load_engine or "") == FullLoadEngine.MYLOADER.value:
        return ""
    role = (source_spec.backup_locale or "").strip()
    if role:
        return role
    if source_spec.shard_index is not None:
        return "slave"
    return ""


def _reject_explicit_backup_locale(source_spec: SourceSpec, cluster: Cluster) -> None:
    if (source_spec.backup_locale or "").strip():
        raise ValueError(_("集群 {} 不支持通过 backup_locale 选择实例").format(cluster.id))


def resolve_spider_dump_endpoint(
    source_spec: SourceSpec, cluster: Cluster, full_load_engine: str = ""
) -> tuple[str, int] | None:
    """TenDBCluster 源的全量导出端点：分片 source 取该分片 remote slave；缺失即抛错（拒单/建流失败）。"""
    if (full_load_engine or "") == FullLoadEngine.MYLOADER.value:
        return None
    _reject_explicit_backup_locale(source_spec, cluster)
    if source_spec.shard_index is None:
        # 未展开的 TenDBCluster 源保持既有行为：无导出从库
        return None
    receiver = shard_remote_slave(cluster, source_spec.shard_index)
    return receiver.machine.ip, receiver.port


def resolve_spider_source_endpoint(
    source_spec: SourceSpec, cluster: Cluster, full_load_engine: str = ""
) -> tuple[str, int]:
    """TenDBCluster 源的 binlog 端点：分片 source 固定该分片 remote master；未展开保持既有语义。"""
    _reject_explicit_backup_locale(source_spec, cluster)
    if source_spec.shard_index is not None:
        master = shard_remote_master(cluster, source_spec.shard_index)
        return master.machine.ip, master.port

    # 以下与 mysql/dts/migrate_helper 的既有分支保持一致（pinned 优先级不变）
    if source_spec.source_host:
        if IP_PORT_DIVIDER in source_spec.source_host:
            ip, port = source_spec.source_host.split(IP_PORT_DIVIDER, 1)
            return ip, int(port)
        return source_spec.source_host, 3306
    if source_spec.source_instance_id:
        ins = storage_by_instance_id(cluster, source_spec.source_instance_id)
        return ins.machine.ip, ins.port
    if source_spec.source_instance_role:
        ins = storage_by_instance_role(cluster, source_spec.source_instance_role)
        return ins.machine.ip, ins.port
    return resolve_spider_master_endpoint(cluster)


def is_tendbcluster_cluster(cluster: Cluster | None) -> bool:
    return cluster is not None and cluster.cluster_type == ClusterType.TenDBCluster.value
