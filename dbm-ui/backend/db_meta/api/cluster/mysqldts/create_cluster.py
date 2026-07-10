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

from backend.db_meta import request_validator
from backend.db_meta.api import machine, proxy_instance, storage_instance
from backend.db_meta.enums import ClusterEntryType, ClusterPhase, ClusterStatus, ClusterType, InstanceRole, MachineType
from backend.db_meta.models import Cluster, ClusterEntry, MysqlDtsCluster, ProxyInstance, StorageInstance
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

    _register_master_nodes(bk_biz_id, bk_cloud_id, master_nodes)
    _register_worker_nodes(bk_biz_id, bk_cloud_id, worker_nodes)

    proxy_objs = ProxyInstance.objects.filter(
        machine__ip__in=[n["ip"] for n in master_nodes], port=MYSQL_DTS_MASTER_PORT
    )
    storage_objs = StorageInstance.objects.filter(
        machine__ip__in=[n["ip"] for n in worker_nodes],
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


def _register_master_nodes(bk_biz_id: int, bk_cloud_id: int, master_nodes: List[dict]):
    machines = []
    proxies = []
    for node in master_nodes:
        machines.append(
            {
                "ip": node["ip"],
                "bk_biz_id": bk_biz_id,
                "bk_cloud_id": bk_cloud_id,
                "machine_type": MachineType.MYSQL_DTS_MASTER.value,
            }
        )
        proxies.append(
            {
                "ip": node["ip"],
                "port": node.get("port", MYSQL_DTS_MASTER_PORT),
            }
        )
    machine.create(machines=machines, bk_cloud_id=bk_cloud_id)
    proxy_instance.create(proxies=proxies, creator="")


def _register_worker_nodes(bk_biz_id: int, bk_cloud_id: int, worker_nodes: List[dict]):
    machines = []
    instances = []
    for node in worker_nodes:
        machines.append(
            {
                "ip": node["ip"],
                "bk_biz_id": bk_biz_id,
                "bk_cloud_id": bk_cloud_id,
                "machine_type": MachineType.MYSQL_DTS_WORKER.value,
            }
        )
        instances.append(
            {
                "ip": node["ip"],
                "port": node.get("port", MYSQL_DTS_WORKER_PORT),
                "instance_role": InstanceRole.MYSQL_DTS_WORKER_MASTER.value,
            }
        )
    machine.create(machines=machines, bk_cloud_id=bk_cloud_id)
    storage_instance.create(instances=instances)


def append_worker_nodes(dts_cluster_id: int, new_worker_nodes: List[dict], updater: str = ""):
    dts_cluster = MysqlDtsCluster.objects.get(id=dts_cluster_id)
    merged = list(dts_cluster.worker_nodes)
    merged.extend(new_worker_nodes)
    dts_cluster.worker_nodes = merged
    dts_cluster.updater = updater
    dts_cluster.save(update_fields=["worker_nodes", "updater", "update_at"])
    _register_worker_nodes(dts_cluster.bk_biz_id, dts_cluster.bk_cloud_id, new_worker_nodes)
    cluster = Cluster.objects.get(id=dts_cluster.cluster_id)
    storage_objs = StorageInstance.objects.filter(
        machine__ip__in=[n["ip"] for n in new_worker_nodes],
        port__in=[n.get("port", MYSQL_DTS_WORKER_PORT) for n in new_worker_nodes],
    )
    cluster.storageinstance_set.add(*storage_objs)
