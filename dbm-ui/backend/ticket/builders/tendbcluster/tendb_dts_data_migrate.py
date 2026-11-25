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

from django.utils.translation import gettext as gettext_runtime
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from backend.components import MySQLDTSApi
from backend.configuration.constants import DBType
from backend.db_meta.enums import ClusterType, TenDBClusterSpiderRole
from backend.db_meta.models import Cluster, MysqlDtsCluster
from backend.flow.engine.controller.spider import SpiderController
from backend.flow.utils.mysql.dts.constants import FullLoadEngine, MigrateType
from backend.flow.utils.mysql.dts.migrate_plan import infer_dts_resource_intent
from backend.flow.utils.spider.dts.constants import CLUSTER_TO_CLUSTER_SHARD_MODE, CLUSTER_TO_CLUSTER_TASK_MODE
from backend.flow.utils.spider.dts.shard_helper import validate_shard_topology
from backend.iam_app.dataclass.actions import ActionEnum
from backend.ticket import builders
from backend.ticket.builders.mysql.dts.mysql_dts_tickets import (
    DtsMigrateFlowBuilder,
    DtsMigrateFlowParamBuilder,
    DtsMigrateResourceParamBuilder,
    MysqlMigrateBaseDetailSerializer,
    _raw_ticket_details,
    _validate_infos_rename_dest_landing,
    _validate_rename_one_to_one,
    _validate_rename_routes,
    _validate_same_name_sync_scope_contract,
)
from backend.ticket.constants import TicketType

logger = logging.getLogger("root")

_ROUTE_WILDCARD_CHARS = ("*", "%", "?")


def _row_label(idx: int, has_infos: bool) -> str:
    return "infos[{}]".format(idx) if has_infos else "migrate"


def _iter_plan_source_cluster_ids(plan):
    for spec in plan.task_specs:
        for source in spec.sources:
            yield spec, source, source.cluster_id
        yield spec, None, spec.target_cluster_id


def _validate_cluster_types_and_shards(plan, label: str) -> dict[int, int]:
    """源/目标必须都是 TenDBCluster；源分片号从 0 连续、每片 remote master + 运行中 remote slave、有 SPIDER_MASTER。

    返回 {cluster_id: 分片数}。
    """
    shard_counts: dict[int, int] = {}
    cluster_ids: list[int] = []
    seen: set[int] = set()
    for _spec, source, cluster_id in _iter_plan_source_cluster_ids(plan):
        if cluster_id in seen:
            continue
        seen.add(cluster_id)
        cluster_ids.append(cluster_id)
    clusters = {cluster.id: cluster for cluster in Cluster.objects.filter(id__in=cluster_ids)}
    for cluster_id in cluster_ids:
        cluster = clusters.get(cluster_id)
        if cluster is None:
            raise serializers.ValidationError(gettext_runtime("{} 集群 {} 不存在").format(label, cluster_id))
        if cluster.cluster_type != ClusterType.TenDBCluster.value:
            raise serializers.ValidationError(
                gettext_runtime("{} 集群 {} 类型为 {}，TenDBCluster DTS 迁移要求源/目标均为 TenDBCluster").format(
                    label, cluster_id, cluster.cluster_type
                )
            )
    # 分片拓扑与 Spider Master 只校验源端（目标端由任务写入，不导出、不加锁）
    for _spec, source, cluster_id in _iter_plan_source_cluster_ids(plan):
        if source is None or cluster_id in shard_counts:
            continue
        cluster = clusters[cluster_id]
        try:
            shard_count = validate_shard_topology(cluster)
        except ValueError as exc:
            raise serializers.ValidationError("{} {}".format(label, str(exc))) from exc
        if not cluster.proxyinstance_set.filter(
            tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_MASTER
        ).exists():
            raise serializers.ValidationError(
                gettext_runtime("{} 源集群 {} 未找到 SPIDER_MASTER，切换加锁需要至少一个 Spider Master").format(label, cluster_id)
            )
        shard_counts[cluster_id] = shard_count
    return shard_counts


def _validate_task_config_contract(plan, label: str) -> None:
    """task_mode 固定 all、全量固定 builtin、不允许 shard_mode / myloader。"""
    configs = [plan.dts_task_config] + [spec.dts_task_config for spec in plan.task_specs]
    for cfg in configs:
        if cfg is None:
            continue
        if cfg.task_mode and cfg.task_mode != CLUSTER_TO_CLUSTER_TASK_MODE:
            raise serializers.ValidationError(
                gettext_runtime("{} TenDBCluster 迁移只支持 task_mode={}，实际为 {}").format(
                    label, CLUSTER_TO_CLUSTER_TASK_MODE, cfg.task_mode
                )
            )
        if cfg.full_load_engine and cfg.full_load_engine != FullLoadEngine.BUILTIN.value:
            raise serializers.ValidationError(
                gettext_runtime("{} TenDBCluster 迁移只支持 full_load_engine={}，实际为 {}").format(
                    label, FullLoadEngine.BUILTIN.value, cfg.full_load_engine
                )
            )
        if (cfg.shard_mode or "").strip():
            raise serializers.ValidationError(
                gettext_runtime("{} TenDBCluster 迁移不支持传入 shard_mode，后端固定 {}").format(
                    label, CLUSTER_TO_CLUSTER_SHARD_MODE
                )
            )
        if cfg.myloader is not None:
            raise serializers.ValidationError(
                gettext_runtime("{} TenDBCluster 迁移不支持 myloader，全量导入请使用 builtin").format(label)
            )
    for spec in plan.task_specs:
        for source in spec.sources:
            if source.myloader is not None:
                raise serializers.ValidationError(
                    gettext_runtime("{} source 的 myloader 参数仅 full_load.engine=myloader 时有效，当前不支持").format(label)
                )


def _plan_worker_requirement(plan, shard_counts: dict[int, int]) -> int:
    """每个分片绑定一个 Worker：按源集群分片数求和。"""
    total = 0
    for spec in plan.task_specs:
        for source in spec.sources:
            total += shard_counts.get(source.cluster_id, 0)
    return total


def _count_free_workers(dts_cluster: MysqlDtsCluster) -> int:
    """统计 DTS 集群未被绑定的 Worker（复用模式容量判定，不自动扩容）。"""
    resp = MySQLDTSApi.list_workers(dts_cluster.master_addr, bk_cloud_id=int(dts_cluster.bk_cloud_id))
    return len([item for item in (resp.data or []) if item.name and not (item.bound_source_name or "")])


def _validate_worker_capacity(attrs: dict, plans: list, has_infos: bool) -> None:
    """Worker 容量：本单部署机器数 / 资源申请数 / 复用集群空闲数 ≥ 分片数，不足直接拒单。

    多行复用同一个 DTS 集群时各行并行注册 Source，空闲 Worker 按集群累计需求判定。
    """
    rows = attrs.get("infos") or [attrs]
    reuse_required: dict[int, int] = {}
    reuse_labels: dict[int, list[str]] = {}
    reuse_clusters: dict[int, MysqlDtsCluster] = {}
    for idx, (row, plan) in enumerate(zip(rows, plans)):
        label = _row_label(idx, has_infos)
        shard_counts = _validate_cluster_types_and_shards(plan, label)
        required = _plan_worker_requirement(plan, shard_counts)
        if required <= 0:
            continue
        resource = row.get("dts_resource") or {}
        try:
            intent = infer_dts_resource_intent(resource)
        except ValueError as exc:
            raise serializers.ValidationError("{} {}".format(label, str(exc))) from exc
        if intent.dts_cluster_id:
            dts_cluster = MysqlDtsCluster.objects.filter(id=intent.dts_cluster_id).first()
            if dts_cluster is None:
                raise serializers.ValidationError(
                    gettext_runtime("{} DTS 集群 {} 不存在").format(label, intent.dts_cluster_id)
                )
            reuse_clusters[dts_cluster.id] = dts_cluster
            reuse_required[dts_cluster.id] = reuse_required.get(dts_cluster.id, 0) + required
            reuse_labels.setdefault(dts_cluster.id, []).append(label)
            continue
        deploy = resource.get("deploy") or {}
        worker_hosts = deploy.get("worker_hosts") or []
        if worker_hosts:
            if len(worker_hosts) < required:
                raise serializers.ValidationError(
                    gettext_runtime("{} deploy.worker_hosts 数量({}) 少于源分片数({})，一台机器一个 Worker").format(
                        label, len(worker_hosts), required
                    )
                )
            continue
        # 资源池模式：规格内 count 需覆盖分片数
        worker_spec = ((row.get("resource_spec") or {}).get("worker")) or {}
        count = int(worker_spec.get("count") or 0)
        if count < required:
            raise serializers.ValidationError(
                gettext_runtime("{} resource_spec.worker.count({}) 少于源分片数({})，每个分片需绑定一个 Worker").format(
                    label, count, required
                )
            )

    for dts_cluster_id, required in reuse_required.items():
        dts_cluster = reuse_clusters[dts_cluster_id]
        label = ",".join(reuse_labels[dts_cluster_id])
        try:
            free_workers = _count_free_workers(dts_cluster)
        except Exception as exc:  # pylint: disable=broad-except
            raise serializers.ValidationError(
                gettext_runtime("{} 查询 DTS 集群 {} 的 Worker 状态失败: {}").format(label, dts_cluster.id, exc)
            ) from exc
        if free_workers < required:
            raise serializers.ValidationError(
                gettext_runtime("{} DTS 集群 {} 可用 Worker 数({}) 少于源分片数({})，请先扩容 DTS Worker 或改用本单部署").format(
                    label, dts_cluster.id, free_workers, required
                )
            )


def _write_back_cluster_to_cluster_types(attrs: dict, plans: list) -> None:
    """details 顶层与 infos 行写入 migrate_type=cluster_to_cluster（inner 预占与建流统一读取）。"""
    attrs["migrate_type"] = MigrateType.CLUSTER_TO_CLUSTER.value
    infos = attrs.get("infos") or []
    for idx in range(len(infos)):
        if idx < len(plans):
            infos[idx]["migrate_type"] = MigrateType.CLUSTER_TO_CLUSTER.value


def _validate_cluster_to_cluster_contracts(attrs: dict, plans: list) -> None:
    has_infos = bool(attrs.get("infos"))
    for idx, plan in enumerate(plans):
        label = _row_label(idx, has_infos)
        _validate_task_config_contract(plan, label)
    _validate_worker_capacity(attrs, plans, has_infos)
    _write_back_cluster_to_cluster_types(attrs, plans)


def _validate_rename_route_contract(plans: list) -> None:
    """TenDBCluster 库改名契约：仅库级真实改名，source_db / target_db 必须是具体库名（不带通配）。"""
    for plan in plans:
        for spec in plan.task_specs:
            for source in spec.sources:
                for route in source.sync_scope.table_routes or []:
                    if (route.source_name or "").strip():
                        raise serializers.ValidationError(
                            gettext_runtime("改名路由不支持填写 source_name（自动按分片生成），当前为 {}").format(route.source_name)
                        )
                    if (route.source_db_pattern or "").strip():
                        raise serializers.ValidationError(
                            gettext_runtime("TenDBCluster 库改名不支持 source_db_pattern，请填写具体 source_db")
                        )
                    if (route.source_table_pattern or "").strip():
                        raise serializers.ValidationError(
                            gettext_runtime("TenDBCluster 库改名不支持 source_table_pattern，仅支持库级改名")
                        )
                    if (route.source_table or "").strip():
                        raise serializers.ValidationError(gettext_runtime("TenDBCluster 库改名不支持 source_table，仅支持库级改名"))
                    if (route.target_table or "").strip():
                        raise serializers.ValidationError(gettext_runtime("TenDBCluster 库改名不支持 target_table，仅支持库级改名"))
                    source_db = (route.source_db or "").strip()
                    if not source_db or any(char in source_db for char in _ROUTE_WILDCARD_CHARS):
                        raise serializers.ValidationError(
                            gettext_runtime("改名路由的 source_db 必须是不带通配符（* % ?）的具体库名，当前为 {}").format(route.source_db)
                        )
                    target_db = (route.target_db or "").strip()
                    if not target_db or any(char in target_db for char in _ROUTE_WILDCARD_CHARS):
                        raise serializers.ValidationError(
                            gettext_runtime("改名路由的 target_db 必须是不带通配符（* % ?）的具体库名，当前为 {}").format(route.target_db)
                        )


class TenDBClusterDtsMigrateDetailSerializer(MysqlMigrateBaseDetailSerializer):
    """TenDBCluster → TenDBCluster 同名迁移入参校验。"""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        _validate_same_name_sync_scope_contract(_raw_ticket_details(self, attrs))
        plans = self.context.get("migrate_plans") or [self.context["migrate_plan"]]
        _validate_cluster_to_cluster_contracts(attrs, plans)
        return attrs


class TenDBClusterDtsRenameMigrateDetailSerializer(MysqlMigrateBaseDetailSerializer):
    """TenDBCluster → TenDBCluster 库改名迁移：one_to_one + 库级真实改名路由。"""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        _validate_rename_one_to_one(attrs)
        plans = self.context.get("migrate_plans") or [self.context["migrate_plan"]]
        for plan in plans:
            _validate_rename_routes(plan)
        _validate_rename_route_contract(plans)
        if attrs.get("infos"):
            _validate_infos_rename_dest_landing(plans)
        _validate_cluster_to_cluster_contracts(attrs, plans)
        return attrs


class TenDBClusterDtsMigrateFlowParamBuilder(DtsMigrateFlowParamBuilder):
    controller = SpiderController.tendbcluster_dts_data_migrate_scene
    migrate_type = MigrateType.CLUSTER_TO_CLUSTER.value


class TenDBClusterDtsMigrateFlowBaseBuilder(DtsMigrateFlowBuilder):
    """TenDBCluster DTS 迁移单据：patch / 资源申请复用 MySQL DTS，group 归 TenDBCluster。"""

    group = DBType.TenDBCluster.value
    cluster_types = [ClusterType.TenDBCluster.value]


@builders.BuilderFactory.register(
    TicketType.TENDBCLUSTER_DTS_DATA_MIGRATE, iam=ActionEnum.TENDBCLUSTER_DTS_DATA_MIGRATE, is_apply=True
)
class TenDBClusterDtsMigrateFlowBuilder(TenDBClusterDtsMigrateFlowBaseBuilder):
    serializer = TenDBClusterDtsMigrateDetailSerializer
    inner_flow_builder = TenDBClusterDtsMigrateFlowParamBuilder
    inner_flow_name = _("TenDB Cluster DTS 数据迁移")
    resource_batch_apply_builder = DtsMigrateResourceParamBuilder
    validator = SpiderController.tendbcluster_dts_data_migrate_scene.validator


@builders.BuilderFactory.register(
    TicketType.TENDBCLUSTER_DTS_DATA_MIGRATE_RENAME, iam=ActionEnum.TENDBCLUSTER_DTS_DATA_MIGRATE, is_apply=True
)
class TenDBClusterDtsRenameMigrateFlowBuilder(TenDBClusterDtsMigrateFlowBaseBuilder):
    serializer = TenDBClusterDtsRenameMigrateDetailSerializer
    inner_flow_builder = TenDBClusterDtsMigrateFlowParamBuilder
    inner_flow_name = _("TenDB Cluster DTS 库改名迁移")
    resource_batch_apply_builder = DtsMigrateResourceParamBuilder
    validator = SpiderController.tendbcluster_dts_data_migrate_scene.validator
