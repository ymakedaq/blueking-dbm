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
import copy
import itertools
import operator
from collections import defaultdict
from functools import reduce
from typing import Any, Dict, List

from django.db.models import Q
from django.db.models.query import QuerySet
from django.forms import model_to_dict

from backend.db_meta.enums import ClusterType, InstanceInnerRole, InstanceRole, TenDBClusterSpiderRole
from backend.db_meta.models import Cluster, DBModule, ProxyInstance, StorageInstance
from backend.db_meta.models.machine import Machine
from backend.db_services.dbbase.cluster.handlers import ClusterServiceHandler as BaseClusterServiceHandler
from backend.db_services.dbbase.dataclass import DBInstance
from backend.db_services.mysql.dataclass import ClusterFilter


class ClusterServiceHandler(BaseClusterServiceHandler):
    def __init__(self, bk_biz_id: int):
        self.bk_biz_id = bk_biz_id

    def query_clusters(self, cluster_filters: List[ClusterFilter]) -> List[Dict[str, Any]]:
        """
        # TODO: Deprecated, 这个方法将被移除，请不要调用
        根据过滤条件查询集群，默认是精确匹配
        """

        def _fill_mysql_instance_info(_cluster: Cluster, _cluster_info: Dict):
            _cluster_info["masters"] = []
            _cluster_info["slaves"] = []
            _cluster_info["repeaters"] = []
            # 录入proxy instance的信息和storage instance的信息
            _cluster_info["proxies"] = [inst.simple_desc for inst in _cluster.proxyinstance_set.all()]
            for inst in _cluster.storageinstance_set.all():
                role = (
                    "masters" if _cluster.cluster_type == ClusterType.TenDBSingle else f"{inst.instance_inner_role}s"
                )
                _cluster_info[role].append(inst.simple_desc)

        def _fill_spider_instance_info(_cluster: Cluster, _cluster_info: Dict):
            # 更新remote db/remote dr的信息
            _cluster_info.update(
                {
                    "remote_db": [
                        m.simple_desc
                        for m in _cluster.storageinstance_set.all()
                        if m.instance_inner_role == InstanceInnerRole.MASTER
                    ],
                    "remote_dr": [
                        m.simple_desc
                        for m in _cluster.storageinstance_set.all()
                        if m.instance_inner_role == InstanceInnerRole.SLAVE
                    ],
                }
            )
            # 更新spider角色信息
            _cluster_info.update(
                {
                    role: [
                        inst.simple_desc
                        for inst in _cluster.proxyinstance_set.all()
                        if inst.tendbclusterspiderext.spider_role == role
                    ]
                    for role in TenDBClusterSpiderRole.get_values()
                }
            )
            # 增加spider_ctl角色信息
            _cluster_info["spider_ctl"] = copy.deepcopy(_cluster_info["spider_master"])
            for instance in _cluster_info["spider_ctl"]:
                instance["port"] = instance["admin_port"]
                instance["instance_address"] = f"{instance['ip']}:{instance['port']}"

        filter_conditions = Q()
        for cluster_filter in cluster_filters:
            filter_conditions |= Q(**cluster_filter.export_filter_conditions())
        # 限制业务和集群类型只能是mysql & tendbcluster
        filter_conditions &= Q(
            cluster_type__in=[ClusterType.TenDBSingle, ClusterType.TenDBHA, ClusterType.TenDBCluster],
            bk_biz_id=self.bk_biz_id,
        )

        clusters: QuerySet = Cluster.objects.prefetch_related("storageinstance_set", "proxyinstance_set").filter(
            filter_conditions
        )
        cluster_db_module_ids: List[int] = [cluster.db_module_id for cluster in clusters]
        db_module_names: Dict[int, str] = {
            module.db_module_id: module.db_module_name
            for module in DBModule.objects.filter(db_module_id__in=cluster_db_module_ids)
        }

        filter_cluster_list = []
        for cluster in clusters:
            cluster_info = self._format_cluster_field(model_to_dict(cluster))
            cluster_info["db_module_name"] = db_module_names.get(cluster_info["db_module_id"], "")

            # 添加实例信息
            cluster_info["instance_count"] = (
                cluster.storageinstance_set.all().count() + cluster.proxyinstance_set.all().count()
            )
            if cluster.cluster_type == ClusterType.TenDBCluster:
                cluster_info["instance_count"] += cluster.proxyinstance_set.filter(
                    tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_MASTER
                ).count()
                _fill_spider_instance_info(cluster, cluster_info)
            else:
                _fill_mysql_instance_info(cluster, cluster_info)

            filter_cluster_list.append(cluster_info)

        return filter_cluster_list

    def get_remote_machine_pairs(self, cluster_ids: List[int]):
        """
        根据tendbcluster集群查询remote机器信息
        @param cluster_ids: 集群的ID列表
        """
        # 获取remote master对应的机器
        clusters: QuerySet[Cluster] = Cluster.objects.prefetch_related("storageinstance_set").filter(
            id__in=cluster_ids
        )
        machine_ids: List[int] = list(
            itertools.chain(
                *[
                    list(
                        cluster.storageinstance_set.select_related("machine")
                        .filter(instance_role=InstanceRole.REMOTE_MASTER)
                        .values_list("machine__bk_host_id", flat=True)
                    )
                    for cluster in clusters
                ]
            )
        )
        master_machines: QuerySet[Machine] = Machine.objects.prefetch_related("storageinstance_set").filter(
            bk_host_id__in=machine_ids
        )

        cluster_id__remote_machines: Dict[int, List[Dict]] = defaultdict(list)
        for master_machine in master_machines:
            # 查询master machine和slave machine
            slave_instance: StorageInstance = master_machine.storageinstance_set.first().as_ejector.first().receiver
            slave_machine: Machine = slave_instance.machine
            cluster_id: int = slave_instance.cluster.first().id
            cluster_id__remote_machines[cluster_id].append(
                {"remote_master": model_to_dict(master_machine), "remote_slave": model_to_dict(slave_machine)}
            )

        remote_machine_infos: List[Dict[str, Any]] = [
            {"cluster_id": cluster_id, "remote_machines": cluster_id__remote_machines[cluster_id]}
            for cluster_id in cluster_id__remote_machines.keys()
        ]
        return remote_machine_infos

    def get_remote_pairs(self, cluster_ids: List[int]):
        """
        根据tendbcluster集群查询remote db/remote dr
        @param cluster_ids: 集群的ID列表
        """
        # 获取集群和对应的remote db实例
        clusters: QuerySet[Cluster] = Cluster.objects.prefetch_related("storageinstance_set").filter(
            id__in=cluster_ids
        )
        master_insts = StorageInstance.objects.prefetch_related("as_ejector").filter(
            cluster__in=clusters, instance_role=InstanceRole.REMOTE_MASTER
        )

        # 根据remote db实例查询remote dr实例
        cluster_id__remote_pairs: Dict[int, List[Dict]] = defaultdict(list)
        for master in master_insts:
            cluster_id = master.cluster.first().id
            slave: StorageInstance = master.as_ejector.first().receiver
            cluster_id__remote_pairs[cluster_id].append(
                {"remote_db": master.simple_desc, "remote_dr": slave.simple_desc}
            )

        remote_pair_infos: List[Dict[str, Any]] = [
            {"cluster_id": cluster_id, "remote_pairs": cluster_id__remote_pairs[cluster_id]}
            for cluster_id in cluster_id__remote_pairs.keys()
        ]
        return remote_pair_infos

    def get_remote_machine_instance_pair(self, query: Dict[str, List[str]]):
        """
        查询remote的主机/实例关联的主机/实例, 仅用于查询spider的主从对关系
        """

        def _get_related_instance(inst: StorageInstance):
            # 这里remote的tuple表示一对db/dr
            if inst.as_ejector.exists():
                return inst.as_ejector.first().receiver
            if inst.as_receiver.exists():
                return inst.as_receiver.first().ejector
            return inst

        related_pairs: Dict[str, Dict] = defaultdict(dict)
        # 查询关联的实例信息
        if query.get("instances"):
            instance_filters = reduce(
                operator.or_,
                [Q(machine__ip=inst.split(":")[0], port=inst.split(":")[1]) for inst in query["instances"]],
            )
            insts = StorageInstance.objects.prefetch_related("as_ejector", "as_receiver").filter(instance_filters)
            related_pairs["instances"] = {inst.ip_port: _get_related_instance(inst).simple_desc for inst in insts}

        # 查询关联的机器信息
        if query.get("machines"):
            machine_filters = reduce(
                operator.or_,
                [Q(bk_cloud_id=machine.split(":")[0], ip=machine.split(":")[1]) for machine in query["machines"]],
            )
            machines = Machine.objects.prefetch_related("storageinstance_set").filter(machine_filters)
            related_pairs["machines"] = {
                f"{machine.bk_cloud_id}:{machine.ip}": _get_related_instance(
                    machine.storageinstance_set.first()
                ).machine.simple_desc
                for machine in machines
                if machine.storageinstance_set.exists()
            }

        return related_pairs

    def _get_instance_objs(self, instances: List[DBInstance]):
        """
        根据instance(属DBInstance类)查询数据库实例，注意这里要考虑混布的情况
        eg: Tendbcluster的中控节点和spider master节点就是混布
        """
        bk_host_ids = [instance.bk_host_id for instance in instances]

        # 获得基本的instance_objs
        instance_objs = super()._get_instance_objs(instances)
        cluster_type = instance_objs[0].cluster.first().cluster_type

        # 如果是Tendbcluster，则中控节点混布，需补充
        if cluster_type == ClusterType.TenDBCluster:
            controller_instances = list(
                ProxyInstance.objects.select_related("machine")
                .prefetch_related("cluster")
                .filter(
                    Q(machine__bk_host_id__in=bk_host_ids)
                    & Q(tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_MASTER.value)
                )
            )
            # 覆写port为admin port
            for instance in controller_instances:
                instance.port = instance.admin_port
            instance_objs.extend(controller_instances)

        return instance_objs
