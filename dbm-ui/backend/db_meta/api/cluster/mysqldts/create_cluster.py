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
from typing import List

from django.db import transaction
from django.utils.translation import gettext as _

from backend.constants import DEFAULT_TIME_ZONE
from backend.db_meta import request_validator
from backend.db_meta.api import machine
from backend.db_meta.enums import (
    AccessLayer,
    ClusterEntryType,
    ClusterPhase,
    ClusterStatus,
    ClusterType,
    InstanceInnerRole,
    InstancePhase,
    InstanceRole,
    InstanceStatus,
    MachineType,
)
from backend.db_meta.models import Cluster, ClusterEntry, Machine, MysqlDtsCluster, ProxyInstance, StorageInstance
from backend.db_meta.models.mysql_dts import MysqlDtsClusterStatus
from backend.flow.utils.mysql.dts.constants import MYSQL_DTS_MASTER_PORT, MYSQL_DTS_WORKER_PORT

logger = logging.getLogger("root")


@transaction.atomic
def create(
    bk_biz_id: int,
    bk_cloud_id: int,
    name: str,
    master_nodes: List[dict],
    worker_nodes: List[dict],
    master_addr: str,
    deploy_path: str,
    version: str = "",
    creator: str = "",
    db_module_id: int = 0,
) -> MysqlDtsCluster:
    bk_biz_id = request_validator.validated_integer(bk_biz_id)
    immute_domain = request_validator.validated_domain(f"{name}.dts.db")
    db_module_id = request_validator.validated_integer(db_module_id) if db_module_id else 0

    cluster = Cluster.objects.create(
        bk_biz_id=bk_biz_id,
        name=name,
        alias=name,
        cluster_type=ClusterType.MySQLDTS.value,
        db_module_id=db_module_id,
        immute_domain=immute_domain,
        creator=creator,
        updater=creator,
        phase=ClusterPhase.ONLINE.value,
        status=ClusterStatus.NORMAL.value,
        bk_cloud_id=bk_cloud_id,
        major_version=version,
    )

    _ensure_machines(bk_biz_id, bk_cloud_id, master_nodes, worker_nodes, creator=creator)
    _create_master_proxies(bk_cloud_id, master_nodes, creator=creator)
    _create_worker_storages(bk_cloud_id, worker_nodes, creator=creator)

    proxy_objs = ProxyInstance.objects.filter(
        machine__ip__in=[n["ip"] for n in master_nodes],
        machine__bk_cloud_id=bk_cloud_id,
        port=MYSQL_DTS_MASTER_PORT,
    )
    storage_objs = StorageInstance.objects.filter(
        machine__ip__in=[n["ip"] for n in worker_nodes],
        machine__bk_cloud_id=bk_cloud_id,
        port__in=[n.get("port", MYSQL_DTS_WORKER_PORT) for n in worker_nodes],
    )
    cluster.proxyinstance_set.add(*proxy_objs)
    cluster.storageinstance_set.add(*storage_objs)
    cluster.save()

    cluster_entry = ClusterEntry.objects.create(
        cluster=cluster,
        cluster_entry_type=ClusterEntryType.DNS.value,
        entry=master_addr,
        creator=creator,
    )
    cluster_entry.proxyinstance_set.add(*proxy_objs)
    cluster_entry.save()

    dts_cluster = MysqlDtsCluster.objects.create(
        name=name,
        bk_biz_id=bk_biz_id,
        bk_cloud_id=bk_cloud_id,
        cluster_id=cluster.id,
        status=MysqlDtsClusterStatus.RUNNING.value,
        master_nodes=master_nodes,
        worker_nodes=worker_nodes,
        master_addr=master_addr,
        deploy_path=deploy_path,
        version=version,
        creator=creator,
        updater=creator,
    )
    logger.info(_("MySQL DTS 集群注册成功: {} cluster_id={}").format(name, cluster.id))
    return dts_cluster


def _ensure_machines(
    bk_biz_id: int,
    bk_cloud_id: int,
    master_nodes: List[dict],
    worker_nodes: List[dict],
    creator: str = "",
):
    """按 IP 去重创建 Machine。

    同机部署时 Master/Worker 共用一个 bk_host_id；若分别 machine.create 会触发
    Duplicate entry for PRIMARY，外层 atomic 回滚后库内又查不到该行。
    同机场景 Machine.machine_type 记为 MYSQL_DTS_COLOCATED；实例层各自写 master/worker。
    """
    master_ips = {n["ip"] for n in master_nodes}
    worker_ips = {n["ip"] for n in worker_nodes}
    # 追加 Worker 到已有 Master 同机时，master_nodes 可能为空，需结合已有元数据判断
    existing_master_like_ips = set(
        Machine.objects.filter(
            ip__in=worker_ips,
            bk_cloud_id=bk_cloud_id,
            machine_type__in=[
                MachineType.MYSQL_DTS_MASTER.value,
                MachineType.MYSQL_DTS_COLOCATED.value,
            ],
        ).values_list("ip", flat=True)
    )
    colocated_ips = (master_ips & worker_ips) | (worker_ips & existing_master_like_ips)
    machines = []
    for ip in sorted(master_ips | worker_ips):
        if ip in colocated_ips:
            machine_type = MachineType.MYSQL_DTS_COLOCATED.value
        elif ip in master_ips:
            machine_type = MachineType.MYSQL_DTS_MASTER.value
        else:
            machine_type = MachineType.MYSQL_DTS_WORKER.value
        machines.append(
            {
                "ip": ip,
                "bk_biz_id": bk_biz_id,
                "bk_cloud_id": bk_cloud_id,
                "machine_type": machine_type,
            }
        )
    if machines:
        # ignore_conflicts：同 IP 已存在（含同机二次注册）时跳过
        machine.get_or_create(bk_cloud_id=bk_cloud_id, machines=machines, creator=creator)

    # 已有纯 Master/Worker 机器后来变成同机时，升级 machine_type
    for ip in colocated_ips:
        Machine.objects.filter(ip=ip, bk_cloud_id=bk_cloud_id).exclude(
            machine_type=MachineType.MYSQL_DTS_COLOCATED.value
        ).update(
            machine_type=MachineType.MYSQL_DTS_COLOCATED.value,
            access_layer=AccessLayer.PROXY.value,
            cluster_type=ClusterType.MySQLDTS.value,
        )


def _create_master_proxies(bk_cloud_id: int, master_nodes: List[dict], creator: str = ""):
    """Master 注册为 ProxyInstance；实例字段按 DTS Master 角色写入，不依赖 Machine.access_layer。"""
    for node in master_nodes:
        ip = node["ip"]
        port = node.get("port", MYSQL_DTS_MASTER_PORT)
        machine_obj = Machine.objects.get(ip=ip, bk_cloud_id=bk_cloud_id)
        if ProxyInstance.objects.filter(machine=machine_obj, port=port).exists():
            continue
        ProxyInstance.objects.create(
            machine=machine_obj,
            port=port,
            admin_port=port + 1000,
            db_module_id=machine_obj.db_module_id,
            bk_biz_id=machine_obj.bk_biz_id,
            access_layer=AccessLayer.PROXY.value,
            machine_type=MachineType.MYSQL_DTS_MASTER.value,
            cluster_type=ClusterType.MySQLDTS.value,
            status=InstanceStatus.RUNNING.value,
            creator=creator,
            time_zone=DEFAULT_TIME_ZONE,
            version="",
        )


def _create_worker_storages(bk_cloud_id: int, worker_nodes: List[dict], creator: str = ""):
    """Worker 注册为 StorageInstance；同机时 Machine 可能是 MASTER 类型，实例层仍写 WORKER。"""
    for node in worker_nodes:
        ip = node["ip"]
        port = node.get("port", MYSQL_DTS_WORKER_PORT)
        machine_obj = Machine.objects.get(ip=ip, bk_cloud_id=bk_cloud_id)
        if StorageInstance.objects.filter(machine=machine_obj, port=port).exists():
            continue
        role = InstanceRole.MYSQL_DTS_WORKER_MASTER.value
        StorageInstance.objects.create(
            port=port,
            machine=machine_obj,
            db_module_id=machine_obj.db_module_id,
            bk_biz_id=machine_obj.bk_biz_id,
            access_layer=AccessLayer.STORAGE.value,
            machine_type=MachineType.MYSQL_DTS_WORKER.value,
            instance_role=role,
            instance_inner_role=InstanceInnerRole.MASTER.value,
            cluster_type=ClusterType.MySQLDTS.value,
            status=InstanceStatus.RUNNING.value,
            creator=creator,
            name="",
            time_zone=DEFAULT_TIME_ZONE,
            version="",
            is_stand_by=True,
            phase=InstancePhase.ONLINE.value,
        )


def append_worker_nodes(dts_cluster_id: int, new_worker_nodes: List[dict], updater: str = ""):
    dts_cluster = MysqlDtsCluster.objects.get(id=dts_cluster_id)
    merged = list(dts_cluster.worker_nodes)
    merged.extend(new_worker_nodes)
    dts_cluster.worker_nodes = merged
    dts_cluster.updater = updater
    dts_cluster.save(update_fields=["worker_nodes", "updater", "update_at"])

    _ensure_machines(
        dts_cluster.bk_biz_id,
        dts_cluster.bk_cloud_id,
        master_nodes=[],
        worker_nodes=new_worker_nodes,
        creator=updater,
    )
    _create_worker_storages(dts_cluster.bk_cloud_id, new_worker_nodes, creator=updater)

    cluster = Cluster.objects.get(id=dts_cluster.cluster_id)
    storage_objs = StorageInstance.objects.filter(
        machine__ip__in=[n["ip"] for n in new_worker_nodes],
        machine__bk_cloud_id=dts_cluster.bk_cloud_id,
        port__in=[n.get("port", MYSQL_DTS_WORKER_PORT) for n in new_worker_nodes],
    )
    cluster.storageinstance_set.add(*storage_objs)
