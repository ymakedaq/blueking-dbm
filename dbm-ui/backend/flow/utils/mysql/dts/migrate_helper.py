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

from backend.components.mysqldtsapi.types import (
    BinlogFilterRuleEntry,
    CreateSourceRequest,
    CreateTaskRequest,
    FullMigrateConfig,
    IncrMigrateConfig,
    Source,
    SourceConfig,
    SourceConfItem,
    TableMigrateRule,
    TableMigrateSource,
    TableMigrateTarget,
    TargetConfig,
    TargetDBConfig,
    TargetSpiderConfig,
    TargetSpiderShard,
    Task,
)
from backend.db_meta.enums import ClusterType, InstanceRole, TenDBClusterSpiderRole
from backend.db_meta.models import Cluster, ProxyInstance, StorageInstance
from backend.db_services.dbbase.constants import IP_PORT_DIVIDER
from backend.flow.utils.mysql.dts.constants import MigrateType
from backend.flow.utils.mysql.dts.migrate_credentials import DtsGrantTarget
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, DtsTaskSpec, SourceSpec, SyncScope


def resolve_source_endpoint(source_spec: SourceSpec, cluster: Cluster) -> tuple[str, int]:
    if source_spec.source_host:
        if IP_PORT_DIVIDER in source_spec.source_host:
            ip, port = source_spec.source_host.split(IP_PORT_DIVIDER, 1)
            return ip, int(port)
        return source_spec.source_host, 3306
    if source_spec.source_instance_id:
        ins = StorageInstance.objects.get(id=source_spec.source_instance_id, cluster=cluster)
        return ins.machine.ip, ins.port
    if source_spec.source_instance_role:
        ins = cluster.storageinstance_set.get(instance_role=source_spec.source_instance_role)
        return ins.machine.ip, ins.port
    slave_qs = cluster.storageinstance_set.filter(instance_role=InstanceRole.BACKEND_SLAVE)
    if slave_qs.filter(is_stand_by=True).exists():
        ins = slave_qs.filter(is_stand_by=True).first()
    else:
        ins = slave_qs.first()
    if not ins:
        raise ValueError(_("集群 {} 未找到可用的源实例").format(cluster.id))
    return ins.machine.ip, ins.port


def _append_grant_target(targets: dict[str, DtsGrantTarget], cluster: Cluster, ip: str, port: int):
    address = "{}{}{}".format(ip, IP_PORT_DIVIDER, port)
    targets[address] = DtsGrantTarget(
        bk_cloud_id=cluster.bk_cloud_id,
        address=address,
        cluster_id=cluster.id,
    )


def _collect_target_grant_endpoints(cluster: Cluster, migrate_type: str) -> list[tuple[str, int]]:
    endpoints: list[tuple[str, int]] = []
    if migrate_type == MigrateType.HA_TO_CLUSTER.value or cluster.cluster_type == ClusterType.TenDBCluster.value:
        for proxy in cluster.proxyinstance_set.filter(
            tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_MASTER
        ):
            endpoints.append((proxy.machine.ip, proxy.port))
        # tdbctl 管理端口通常为 spider.port + 1000
        for proxy in cluster.proxyinstance_set.filter(
            tendbclusterspiderext__spider_role__in=[
                TenDBClusterSpiderRole.SPIDER_CTL,
                TenDBClusterSpiderRole.SPIDER_MASTER,
            ]
        ):
            if getattr(proxy, "admin_port", 0):
                endpoints.append((proxy.machine.ip, proxy.admin_port))
            else:
                endpoints.append((proxy.machine.ip, proxy.port + 1000))
        for storage in cluster.storageinstance_set.filter(instance_role=InstanceRole.REMOTE_MASTER):
            endpoints.append((storage.machine.ip, storage.port))
        if not endpoints:
            proxy = cluster.proxyinstance_set.first()
            if proxy:
                endpoints.append((proxy.machine.ip, proxy.port))
    else:
        master = cluster.storageinstance_set.get(instance_role=InstanceRole.BACKEND_MASTER)
        endpoints.append((master.machine.ip, master.port))
    return endpoints


def collect_migrate_grant_targets(plan: DtsMigratePlan) -> list[DtsGrantTarget]:
    """收集迁移链路需在哪些 MySQL 实例上创建 DTS 临时账号。"""
    targets: dict[str, DtsGrantTarget] = {}
    for task_spec in plan.task_specs:
        for source_spec in task_spec.sources:
            cluster = Cluster.objects.get(id=source_spec.cluster_id)
            ip, port = resolve_source_endpoint(source_spec, cluster)
            _append_grant_target(targets, cluster, ip, port)
        target_cluster = Cluster.objects.get(id=task_spec.target_cluster_id)
        for ip, port in _collect_target_grant_endpoints(target_cluster, plan.migrate_type):
            _append_grant_target(targets, target_cluster, ip, port)
    return list(targets.values())


def _table_item_schema_table(item) -> tuple[str, str]:
    """兼容 do_tables/ignore_tables 的 dict 或 'db.table' 字符串。"""
    if isinstance(item, dict):
        schema = item.get("db") or item.get("schema") or item.get("dbname") or "*"
        table = item.get("table") or item.get("tablename") or "*"
        return schema, table
    if isinstance(item, str) and "." in item:
        schema, table = item.split(".", 1)
        return schema, table
    if isinstance(item, str):
        return "*", item
    return "*", "*"


def _build_table_migrate_rules(source_name: str, sync_scope: SyncScope) -> list[TableMigrateRule]:
    """将 sync_scope 转为 DTS table_migrate_rule。

    优先使用显式 table_routes；否则由 do_dbs/do_tables 生成白名单规则。
    ignore_dbs/ignore_tables 一期通过不生成对应规则实现（仅白名单模式）。
    """
    rules: list[TableMigrateRule] = []
    for route in sync_scope.table_routes:
        rules.append(
            TableMigrateRule(
                source=TableMigrateSource(
                    source_name=route.get("source_name", source_name),
                    schema=route.get("source_db_pattern", route.get("source_db", "")),
                    table=route.get("source_table_pattern", route.get("source_table", "")),
                ),
                target=TableMigrateTarget(
                    schema=route.get("target_db"),
                    table=route.get("target_table"),
                ),
            )
        )
    if rules:
        return rules

    ignore_db_set = set(sync_scope.ignore_dbs or [])
    ignore_table_set = set()
    for item in sync_scope.ignore_tables or []:
        schema, table = _table_item_schema_table(item)
        ignore_table_set.add((schema, table))

    for db_name in sync_scope.do_dbs or []:
        if db_name in ignore_db_set:
            continue
        if ("*", "*") in ignore_table_set or (db_name, "*") in ignore_table_set:
            continue
        rules.append(
            TableMigrateRule(
                source=TableMigrateSource(source_name=source_name, schema=db_name, table="*"),
            )
        )

    for item in sync_scope.do_tables or []:
        schema, table = _table_item_schema_table(item)
        if schema in ignore_db_set:
            continue
        if (schema, table) in ignore_table_set or (schema, "*") in ignore_table_set:
            continue
        rules.append(
            TableMigrateRule(
                source=TableMigrateSource(source_name=source_name, schema=schema, table=table),
            )
        )
    return rules


def _build_binlog_filter_rules(sync_scope: SyncScope) -> dict[str, BinlogFilterRuleEntry]:
    rules = {}
    for item in sync_scope.binlog_filters:
        name = item.get("name", "")
        if not name:
            continue
        rules[name] = BinlogFilterRuleEntry(
            ignore_event=item.get("ignore_events", []),
            ignore_sql=item.get("ignore_sql", []),
        )
    return rules


def build_create_source_request(
    source_spec: SourceSpec,
    cluster: Cluster,
    *,
    user: str,
    password: str,
    worker_name: str | None = None,
) -> CreateSourceRequest:
    host, port = resolve_source_endpoint(source_spec, cluster)
    cluster_type = "mysql"
    if cluster.cluster_type == ClusterType.TenDBCluster.value:
        cluster_type = "spider"
    source = Source(
        source_name=source_spec.source_name,
        host=host,
        port=port,
        user=user,
        password=password,
        enable_gtid=True,
        enable=True,
        cluster_type=cluster_type,
    )
    return CreateSourceRequest(source=source, worker_name=worker_name)


def _build_ha_target_config(cluster: Cluster, user: str, password: str) -> TargetConfig:
    master = cluster.storageinstance_set.get(instance_role=InstanceRole.BACKEND_MASTER)
    return TargetConfig(
        host=master.machine.ip,
        port=master.port,
        user=user,
        password=password,
        cluster_type="mysql",
    )


def _resolve_spider_master(cluster: Cluster) -> ProxyInstance:
    spider_master = cluster.proxyinstance_set.filter(
        tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_MASTER
    ).first()
    if not spider_master:
        spider_master = cluster.proxyinstance_set.first()
    if not spider_master:
        raise ValueError(_("集群 {} 未找到 Spider Master").format(cluster.id))
    return spider_master


def _resolve_tdbctl_endpoint(cluster: Cluster, spider_master: ProxyInstance) -> tuple[str, int]:
    """解析 tdbctl 连接点：优先 SPIDER_CTL，否则用 spider_master.admin_port / port+1000。"""
    ctl = cluster.proxyinstance_set.filter(
        tendbclusterspiderext__spider_role=TenDBClusterSpiderRole.SPIDER_CTL
    ).first()
    if ctl:
        port = ctl.admin_port or ctl.port
        return ctl.machine.ip, port

    # 与 Cluster.tendbcluster_ctl_primary_address 一致：中控端口 = spider.port + 1000
    try:
        address = cluster.tendbcluster_ctl_primary_address()
        ip, port = address.split(IP_PORT_DIVIDER, 1)
        return ip, int(port)
    except Exception:  # pylint: disable=broad-except
        port = spider_master.admin_port or (spider_master.port + 1000)
        return spider_master.machine.ip, port


def _build_cluster_target_config(cluster: Cluster, user: str, password: str) -> TargetConfig:
    spider_master = _resolve_spider_master(cluster)
    tdbctl_host, tdbctl_port = _resolve_tdbctl_endpoint(cluster, spider_master)
    remote_masters = cluster.storageinstance_set.filter(instance_role=InstanceRole.REMOTE_MASTER)
    shards = [
        TargetSpiderShard(host=ins.machine.ip, port=ins.port, user=user, password=password) for ins in remote_masters
    ]
    spider_cfg = TargetSpiderConfig(
        tdbctl=TargetDBConfig(
            host=tdbctl_host,
            port=tdbctl_port,
            user=user,
            password=password,
        ),
        mode="proxy",
        shards=shards,
    )
    return TargetConfig(
        host=spider_master.machine.ip,
        port=spider_master.port,
        user=user,
        password=password,
        cluster_type="spider",
        spider=spider_cfg,
    )


def build_target_config(target_cluster_id: int, migrate_type: str, user: str, password: str) -> TargetConfig:
    cluster = Cluster.objects.get(id=target_cluster_id)
    if migrate_type == MigrateType.HA_TO_CLUSTER.value or cluster.cluster_type == ClusterType.TenDBCluster.value:
        return _build_cluster_target_config(cluster, user, password)
    return _build_ha_target_config(cluster, user, password)


def build_dts_task_request(
    plan: DtsMigratePlan,
    task_spec: DtsTaskSpec,
    *,
    user: str,
    password: str,
) -> CreateTaskRequest:
    source_conf = [SourceConfItem(source_name=src.source_name) for src in task_spec.sources]
    table_rules: list[TableMigrateRule] = []
    binlog_filters: dict[str, BinlogFilterRuleEntry] = {}
    for src in task_spec.sources:
        table_rules.extend(_build_table_migrate_rules(src.source_name, src.sync_scope))
        binlog_filters.update(_build_binlog_filter_rules(src.sync_scope))

    target_cfg = task_spec.target_config
    if not target_cfg or not target_cfg.host:
        target_cfg = build_target_config(task_spec.target_cluster_id, plan.migrate_type, user, password)

    cfg = task_spec.dts_task_config
    task = Task(
        name=task_spec.task_name,
        task_mode=cfg.task_mode,
        shard_mode=cfg.shard_mode or "",
        on_duplicate=cfg.on_duplicate,
        meta_schema=cfg.meta_schema,
        ignore_checking_items=cfg.ignore_checking_items,
        target_config=target_cfg,
        source_config=SourceConfig(
            source_conf=source_conf,
            full_migrate_conf=FullMigrateConfig(**cfg.full_migrate) if cfg.full_migrate else None,
            incr_migrate_conf=IncrMigrateConfig(**cfg.incr_migrate) if cfg.incr_migrate else None,
        ),
        table_migrate_rule=table_rules,
        binlog_filter_rule=binlog_filters,
    )
    return CreateTaskRequest(task=task)
