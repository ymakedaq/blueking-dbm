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

from backend.db_meta.enums import ClusterType, TenDBClusterSpiderRole
from backend.db_meta.models import Cluster
from backend.flow.utils.mysql.dts.constants import FullLoadEngine, MigrateType
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, build_migrate_plans
from backend.flow.utils.spider.dts.constants import CLUSTER_TO_CLUSTER_SHARD_MODE, CLUSTER_TO_CLUSTER_TASK_MODE
from backend.flow.utils.spider.dts.shard_helper import expand_tendbcluster_source_specs, validate_shard_topology


def _force_task_config(plan: DtsMigratePlan) -> None:
    """cluster_to_cluster 平台侧固定值：task_mode=all、builtin 全量、shard_mode=pessimistic。"""
    for cfg in [plan.dts_task_config] + [spec.dts_task_config for spec in plan.task_specs]:
        if cfg.task_mode and cfg.task_mode != CLUSTER_TO_CLUSTER_TASK_MODE:
            raise ValueError(
                _("TenDBCluster 迁移只支持 task_mode={}，实际为 {}").format(CLUSTER_TO_CLUSTER_TASK_MODE, cfg.task_mode)
            )
        if cfg.full_load_engine and cfg.full_load_engine != FullLoadEngine.BUILTIN.value:
            raise ValueError(
                _("TenDBCluster 迁移只支持 full_load_engine={}，实际为 {}").format(
                    FullLoadEngine.BUILTIN.value, cfg.full_load_engine
                )
            )
        if (cfg.shard_mode or "").strip():
            raise ValueError(_("TenDBCluster 迁移不支持传入 shard_mode，后端固定 {}").format(CLUSTER_TO_CLUSTER_SHARD_MODE))
        if cfg.myloader is not None:
            raise ValueError(_("TenDBCluster 迁移不支持 myloader"))
        cfg.task_mode = CLUSTER_TO_CLUSTER_TASK_MODE
        cfg.full_load_engine = FullLoadEngine.BUILTIN.value
        cfg.shard_mode = CLUSTER_TO_CLUSTER_SHARD_MODE


def _validate_source_spider_master(cluster: Cluster) -> None:
    if not cluster.proxyinstance_set.filter(
        tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_MASTER
    ).exists():
        raise ValueError(_("源集群 {} 未找到 SPIDER_MASTER，切换加锁需要至少一个 Spider Master").format(cluster.id))


def _expand_plan_shard_sources(plan: DtsMigratePlan) -> None:
    """每个 TenDBCluster 源展开为一分片一 Source，并把 Worker 需求置为分片总数。"""
    worker_required = 0
    for task_spec in plan.task_specs:
        target_cluster = Cluster.objects.filter(id=task_spec.target_cluster_id).first()
        if target_cluster is None or target_cluster.cluster_type != ClusterType.TenDBCluster.value:
            raise ValueError(
                _("cluster_to_cluster 迁移的目标集群 {} 不存在或不是 TenDBCluster").format(task_spec.target_cluster_id)
            )
        expanded_sources = []
        for source in task_spec.sources:
            if source.myloader is not None:
                raise ValueError(_("TenDBCluster 迁移不支持 myloader（source {}）").format(source.source_name))
            cluster = Cluster.objects.get(id=source.cluster_id)
            if cluster.cluster_type != ClusterType.TenDBCluster.value:
                raise ValueError(_("cluster_to_cluster 迁移的源集群 {} 不是 TenDBCluster").format(cluster.id))
            if source.cluster_id == task_spec.target_cluster_id:
                raise ValueError(_("源集群与目标集群不能相同（当前均为 {}）").format(source.cluster_id))
            validate_shard_topology(cluster)
            _validate_source_spider_master(cluster)
            expanded = expand_tendbcluster_source_specs(source, task_spec.dts_task_config)
            if not expanded:
                raise ValueError(_("源集群 {} 分片展开结果为空").format(cluster.id))
            expanded_sources.extend(expanded)
        task_spec.sources = expanded_sources
        worker_required += len(expanded_sources)
    if worker_required <= 0:
        raise ValueError(_("未解析到任何分片 Source"))
    plan.worker_count_required = max(plan.worker_count_required, worker_required)


def _finalize_spider_plan(plan: DtsMigratePlan) -> DtsMigratePlan:
    plan.migrate_type = MigrateType.CLUSTER_TO_CLUSTER.value
    _force_task_config(plan)
    _expand_plan_shard_sources(plan)
    return plan


def build_spider_migrate_plans(
    ticket_details: dict,
    *,
    require_task_name: bool = True,
) -> list[DtsMigratePlan]:
    """多行 infos → N 个展开分片后的 DtsMigratePlan。"""
    plans = build_migrate_plans(ticket_details, require_task_name=require_task_name)
    return [_finalize_spider_plan(plan) for plan in plans]


def resolve_spider_migrate_plans_from_ticket_data(data: dict) -> list[DtsMigratePlan]:
    """从 Flow ticket_data 解析 cluster_to_cluster plan 列表（含分片展开），并清理缓存的 plan 键。"""
    from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, dts_migrate_plan_from_dict

    data.pop("migrate_plan", None)
    raw_plans = data.pop("migrate_plans", None)
    if isinstance(raw_plans, list) and raw_plans:
        return [item if isinstance(item, DtsMigratePlan) else dts_migrate_plan_from_dict(item) for item in raw_plans]
    return build_spider_migrate_plans(data)
