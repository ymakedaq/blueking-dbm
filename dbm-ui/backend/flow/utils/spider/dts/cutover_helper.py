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

from backend.db_meta.enums import TenDBClusterSpiderRole
from backend.db_meta.models import Cluster, MysqlDtsCluster
from backend.flow.utils.mysql.dts.cutover_helper import merge_task_sync_scopes
from backend.flow.utils.mysql.dts.migrate_helper import resolve_source_endpoint
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, DtsTaskSpec
from backend.flow.utils.spider.dts.constants import SPIDER_DTS_CUTOVER_LOCK_MODE
from backend.flow.utils.spider.dts.shard_helper import list_cluster_shards

# 切换锁定的 Spider 角色：SPIDER_MASTER 接写入，SPIDER_MNT 运维通道同样可写入；SPIDER_SLAVE 只读不锁
_CUTOVER_SPIDER_ROLES = (TenDBClusterSpiderRole.SPIDER_MASTER, TenDBClusterSpiderRole.SPIDER_MNT)


def build_spider_endpoints(cluster: Cluster, dts_user: str, dts_password: str) -> list[dict]:
    """源集群 SPIDER_MASTER + SPIDER_MNT 连接点（切换加锁对象）。"""
    endpoints: list[dict] = []
    proxies = cluster.proxyinstance_set.filter(tendbclusterspiderext__spider_role__in=_CUTOVER_SPIDER_ROLES).order_by(
        "id"
    )
    for proxy in proxies:
        role = proxy.tendbclusterspiderext.spider_role
        endpoints.append(
            {
                "host": proxy.machine.ip,
                "port": proxy.port,
                "role": role,
                "user": dts_user,
                "password": dts_password,
            }
        )
    if not any(item["role"] == TenDBClusterSpiderRole.SPIDER_MASTER.value for item in endpoints):
        raise ValueError(_("源集群 {} 未找到 SPIDER_MASTER，无法执行切换加锁").format(cluster.id))
    return endpoints


def build_shard_sources(task_spec: DtsTaskSpec, dts_user: str, dts_password: str) -> list[dict]:
    """task_spec.sources（已按分片展开）→ 切换 payload 的分片清单；host/port 为该分片 remote master。"""
    shard_sources: list[dict] = []
    for source_spec in task_spec.sources:
        if source_spec.shard_index is None:
            raise ValueError(_("source {} 缺少分片信息，无法组装切换 payload").format(source_spec.source_name))
        cluster = Cluster.objects.get(id=source_spec.cluster_id)
        host, port = resolve_source_endpoint(source_spec, cluster)
        shard_sources.append(
            {
                "source_name": source_spec.source_name,
                "shard_id": int(source_spec.shard_index),
                "host": host,
                "port": int(port),
                "user": dts_user,
                "password": dts_password,
            }
        )
    return shard_sources


def build_spider_dts_cutover_payload(
    *,
    root_id: str,
    master_addr: str,
    deploy_path: str,
    task_spec: DtsTaskSpec,
    dts_user: str,
    dts_password: str,
) -> dict:
    """组装下发给 dbactuator spider dts-cutover 的 payload（02 第 6.3 节，与 03 参数一一对应）。"""
    if not master_addr:
        raise ValueError(_("cutover 组装失败：master_addr 为空"))
    if not task_spec or not task_spec.sources:
        raise ValueError(_("cutover 组装失败：task_spec 无 sources"))
    if not dts_user or not dts_password:
        raise ValueError(_("cutover 组装失败：临时账号 dts_user/dts_password 为空"))
    if not deploy_path:
        raise ValueError(_("cutover 组装失败：deploy_path 为空"))
    if not task_spec.task_name:
        raise ValueError(_("cutover 组装失败：task_name 为空"))

    source_cluster_ids: list[int] = []
    for source_spec in task_spec.sources:
        if source_spec.cluster_id not in source_cluster_ids:
            source_cluster_ids.append(source_spec.cluster_id)
    # 一个任务只能包含同一源集群的全部分片（mysql-dts spider_validate），此处取第一个源集群
    source_cluster = Cluster.objects.get(id=source_cluster_ids[0])

    spider_endpoints = build_spider_endpoints(source_cluster, dts_user, dts_password)
    shard_sources = build_shard_sources(task_spec, dts_user, dts_password)
    shard_count = len(list_cluster_shards(source_cluster))
    if len(shard_sources) != shard_count:
        raise ValueError(
            _("cutover 组装失败：分片 Source 数量 {} 与源集群 {} 分片数 {} 不一致").format(
                len(shard_sources), source_cluster.id, shard_count
            )
        )

    return {
        "dts_master_addr": master_addr,
        "deploy_path": deploy_path,
        "task_name": task_spec.task_name,
        "spider_endpoints": spider_endpoints,
        "shard_sources": shard_sources,
        "sync_scope": merge_task_sync_scopes(task_spec),
        "lock_mode": SPIDER_DTS_CUTOVER_LOCK_MODE,
        "position_file_key": "{}_{}".format(root_id, task_spec.task_name),
    }


def resolve_spider_dts_deploy_path(migrate_plan: DtsMigratePlan) -> str:
    """切换位点文件目录基准：已有集群读 MysqlDtsCluster，本单部署读部署入参。"""
    from backend.flow.utils.mysql.dts.constants import get_default_deploy_path

    if migrate_plan.dts_cluster_id:
        dts_cluster = MysqlDtsCluster.objects.filter(id=migrate_plan.dts_cluster_id).first()
        if dts_cluster and dts_cluster.deploy_path:
            return dts_cluster.deploy_path
        if dts_cluster and dts_cluster.name:
            return get_default_deploy_path(dts_cluster.name)
    if migrate_plan.deploy_subflow_inp:
        if migrate_plan.deploy_subflow_inp.deploy_path:
            return migrate_plan.deploy_subflow_inp.deploy_path
        return get_default_deploy_path(migrate_plan.deploy_subflow_inp.cluster_name)
    raise ValueError(_("无法解析 DTS deploy_path"))
