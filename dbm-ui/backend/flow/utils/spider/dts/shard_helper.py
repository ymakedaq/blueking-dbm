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

from backend.db_meta.enums import ClusterType, InstanceRole, InstanceStatus
from backend.db_meta.models import Cluster
from backend.flow.utils.mysql.dts.constants import FullLoadEngine
from backend.flow.utils.mysql.dts.migrate_plan import DtsTaskConfig, MyloaderSpec, SourceSpec, copy_myloader_spec


def list_cluster_shards(cluster: Cluster) -> list:
    """按 shard_id 升序列出 TenDBCluster 分片；无分片元数据视为异常。"""
    shards = list(cluster.tendbclusterstorageset_set.all().order_by("shard_id"))
    if not shards:
        raise ValueError(_("TenDBCluster {} 无分片元数据，无法展开 Source").format(cluster.id))
    return shards


def validate_shard_topology(cluster: Cluster) -> int:
    """提单期校验：分片号从 0 连续；每个分片都有 remote master 与运行中的 remote slave。

    返回分片数。任一不满足直接抛 ValueError（拒单）。
    """
    if cluster.cluster_type != ClusterType.TenDBCluster.value:
        raise ValueError(_("集群 {} 不是 TenDBCluster，无法校验分片拓扑").format(cluster.id))
    shards = list_cluster_shards(cluster)
    for idx, shard in enumerate(shards):
        if shard.shard_id != idx:
            raise ValueError(_("集群 {} 的分片号必须从 0 开始连续，实际第 {} 个分片号为 {}").format(cluster.id, idx, shard.shard_id))
        if shard.storage_instance_tuple.ejector is None:
            raise ValueError(_("集群 {} 的分片 {} 未找到 Remote Master").format(cluster.id, shard.shard_id))
        receiver = shard.storage_instance_tuple.receiver
        if receiver is None:
            raise ValueError(_("集群 {} 的分片 {} 未找到 Remote Slave").format(cluster.id, shard.shard_id))
        if receiver.status != InstanceStatus.RUNNING.value:
            raise ValueError(
                _("集群 {} 的分片 {} Remote Slave {} 状态为 {}，不可用").format(
                    cluster.id, shard.shard_id, receiver.machine.ip, receiver.status
                )
            )
    return len(shards)


def get_shard_by_index(cluster: Cluster, shard_index: int):
    for shard in list_cluster_shards(cluster):
        if shard.shard_id == int(shard_index):
            return shard
    raise ValueError(_("集群 {} 未找到分片 {}").format(cluster.id, shard_index))


def _pick_shard_remote_instance(shard, instance_role=None):
    """分片连接端点：默认 Remote Master（ejector）；指定 remote_slave 时用 receiver。"""
    receiver = shard.storage_instance_tuple.receiver
    ejector = shard.storage_instance_tuple.ejector
    role = getattr(instance_role, "value", instance_role) or ""
    if role == InstanceRole.REMOTE_SLAVE.value:
        if receiver is None:
            raise ValueError(_("分片 {} 未找到 Remote Slave").format(shard.shard_id))
        return receiver
    if ejector is None:
        raise ValueError(_("分片 {} 未找到 Remote Master").format(shard.shard_id))
    return ejector


def shard_remote_master(cluster: Cluster, shard_index: int):
    """指定分片的 remote master（binlog 拉取节点）。"""
    return _pick_shard_remote_instance(get_shard_by_index(cluster, shard_index))


def shard_remote_slave(cluster: Cluster, shard_index: int):
    """指定分片的 remote slave（全量导出节点）；缺失即抛错。"""
    receiver = get_shard_by_index(cluster, shard_index).storage_instance_tuple.receiver
    if receiver is None:
        raise ValueError(_("集群 {} 的分片 {} 未找到 Remote Slave，无法作为全量导出节点").format(cluster.id, shard_index))
    return receiver


def expand_tendbcluster_source_specs(
    source: SourceSpec,
    task_cfg: DtsTaskConfig | None = None,
) -> list[SourceSpec]:
    """将 TenDBCluster 源展开为 N 个 SourceSpec（一分片一 Source）。

    已带 shard_index 的 source 视为手工展开，原样返回。
    非 TenDBCluster 原样返回。
    """
    if source.shard_index is not None:
        return [source]

    cluster = Cluster.objects.get(id=source.cluster_id)
    if cluster.cluster_type != ClusterType.TenDBCluster.value:
        return [source]

    if (source.backup_locale or "").strip():
        raise ValueError(_("集群 {} 不支持通过 backup_locale 选择实例").format(cluster.id))

    shards = list_cluster_shards(cluster)

    shard_count = len(shards)
    spider_cluster_id = source.spider_cluster_id or cluster.immute_domain
    base_name = source.source_name or "source"
    use_myloader = False
    if task_cfg and task_cfg.full_load_engine == FullLoadEngine.MYLOADER.value:
        use_myloader = True
    if source.myloader is not None or (task_cfg and task_cfg.myloader is not None):
        use_myloader = True

    expanded: list[SourceSpec] = []
    for shard in shards:
        ins = _pick_shard_remote_instance(shard, source.source_instance_role)
        myloader = copy_myloader_spec(source.myloader)
        if myloader is None and task_cfg:
            myloader = copy_myloader_spec(task_cfg.myloader)
        if use_myloader:
            if myloader is None:
                myloader = MyloaderSpec()
            myloader.shard_id = shard.shard_id
        expanded.append(
            SourceSpec(
                cluster_id=source.cluster_id,
                source_name=f"{base_name}-{shard.shard_id}",
                sync_scope=source.sync_scope,
                source_instance_id=ins.id,
                source_instance_role=None,
                source_host=None,
                myloader=myloader,
                shard_index=shard.shard_id,
                shard_count=shard_count,
                spider_cluster_id=spider_cluster_id,
                worker_name=source.worker_name or "",
            )
        )
    return expanded
