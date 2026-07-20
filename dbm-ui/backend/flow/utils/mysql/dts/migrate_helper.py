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

from django.utils.translation import gettext as _

from backend.components.db_remote_service.client import DRSApi
from backend.components.mysqldtsapi.types import (
    BinlogFilterRuleEntry,
    CreateSourceRequest,
    CreateTaskRequest,
    FullMigrateConfig,
    IncrMigrateConfig,
    MyLoaderConfig,
    Source,
    SourceConfig,
    SourceConfItem,
    SpiderInfo,
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
from backend.db_meta.models import Cluster, MysqlDtsCluster, ProxyInstance, StorageInstance
from backend.db_services.dbbase.constants import IP_PORT_DIVIDER
from backend.flow.utils.mysql.dts.constants import FullLoadEngine, MigrateType
from backend.flow.utils.mysql.dts.migrate_credentials import DtsGrantTarget
from backend.flow.utils.mysql.dts.migrate_plan import (
    DtsMigratePlan,
    DtsTaskConfig,
    DtsTaskSpec,
    MyloaderSpec,
    SourceSpec,
    SyncScope,
    copy_myloader_spec,
)

logger = logging.getLogger("flow")


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

    # TenDBCluster：Source 必须落在 Remote 存储节点，不能用 Spider 代理探测/拉 binlog
    if cluster.cluster_type == ClusterType.TenDBCluster.value:
        slave_qs = cluster.storageinstance_set.filter(instance_role=InstanceRole.REMOTE_SLAVE)
        ins = slave_qs.filter(is_stand_by=True).first() or slave_qs.first()
        if not ins:
            ins = cluster.storageinstance_set.filter(instance_role=InstanceRole.REMOTE_MASTER).first()
        if not ins:
            raise ValueError(_("集群 {} 未找到可用的 Remote 源实例").format(cluster.id))
        return ins.machine.ip, ins.port

    slave_qs = cluster.storageinstance_set.filter(instance_role=InstanceRole.BACKEND_SLAVE)
    if slave_qs.filter(is_stand_by=True).exists():
        ins = slave_qs.filter(is_stand_by=True).first()
    else:
        ins = slave_qs.first()
    if not ins:
        raise ValueError(_("集群 {} 未找到可用的源实例").format(cluster.id))
    return ins.machine.ip, ins.port


def _pick_shard_remote_instance(shard) -> StorageInstance:
    """分片连接端点：优先 Remote Slave（standby），否则 Remote Master（ejector）。"""
    receiver = shard.storage_instance_tuple.receiver
    ejector = shard.storage_instance_tuple.ejector
    if receiver is not None:
        if getattr(receiver, "is_stand_by", False):
            return receiver
        return receiver
    if ejector is None:
        raise ValueError(_("分片 {} 未找到 Remote 实例").format(shard.shard_id))
    return ejector


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

    shards = list(cluster.tendbclusterstorageset_set.all().order_by("shard_id"))
    if not shards:
        raise ValueError(_("TenDBCluster {} 无分片元数据，无法展开 Source").format(cluster.id))

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
        ins = _pick_shard_remote_instance(shard)
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


def assign_source_workers(
    sources: list[SourceSpec],
    worker_nodes: list[dict],
) -> None:
    """按 shard_index / 顺序为一对一绑定 worker_name 与 dest_worker_ip。"""
    if not sources:
        return
    if len(worker_nodes) < len(sources):
        raise ValueError(
            _("DTS Worker 数量({}) 少于 Source 数量({})，TenDBCluster 源需一分片一 Worker").format(
                len(worker_nodes), len(sources)
            )
        )
    ordered = sorted(
        enumerate(sources),
        key=lambda item: (item[1].shard_index is None, item[1].shard_index if item[1].shard_index is not None else item[0]),
    )
    for bind_idx, (unused_orig_idx, src) in enumerate(ordered):
        node = worker_nodes[bind_idx]
        name = node.get("name") or node.get("worker_name") or ""
        ip = node.get("ip") or ""
        if not name:
            raise ValueError(_("Worker 节点缺少 name，无法绑定 Source {}").format(src.source_name))
        src.worker_name = name
        if src.myloader is None:
            continue
        if not src.myloader.dest_worker_ip and ip:
            src.myloader.dest_worker_ip = ip


def resolve_dts_worker_nodes(migrate_plan: DtsMigratePlan, deployed_worker_nodes: list | None = None) -> list[dict]:
    """解析可用于绑定的 DTS Worker 节点列表。"""
    if deployed_worker_nodes:
        return list(deployed_worker_nodes)
    if migrate_plan.dts_cluster_id:
        dts_cluster = MysqlDtsCluster.objects.filter(id=migrate_plan.dts_cluster_id).first()
        if dts_cluster and dts_cluster.worker_nodes:
            return list(dts_cluster.worker_nodes)
    deploy = migrate_plan.deploy_subflow_inp
    if deploy and deploy.worker_hosts:
        return [
            {"ip": h.ip, "bk_cloud_id": h.bk_cloud_id, "name": h.name or f"worker-{idx + 1}"}
            for idx, h in enumerate(deploy.worker_hosts)
        ]
    return []


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


def probe_instance_gtid_enabled(*, host: str, port: int, bk_cloud_id: int) -> bool:
    """探测单个 MySQL 实例 gtid_mode 是否为 ON。

    低版本无该变量 / DRS 失败 → False。
    """
    address = "{}{}{}".format(host, IP_PORT_DIVIDER, port)
    try:
        resp = DRSApi.rpc(
            {
                "addresses": [address],
                "cmds": ["SHOW GLOBAL VARIABLES LIKE 'gtid_mode';"],
                "force": False,
                "bk_cloud_id": bk_cloud_id,
            }
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(_("探测实例 {} GTID 失败: {}").format(address, exc))
        return False

    if not resp:
        logger.warning(_("探测实例 {} GTID 返回为空").format(address))
        return False

    top_err = resp[0].get("error_msg") or ""
    cmd_results = resp[0].get("cmd_results") or []
    if top_err or not cmd_results:
        logger.warning(_("探测实例 {} GTID 失败: {}").format(address, top_err or _("无结果")))
        return False

    cmd_err = cmd_results[0].get("error_msg") or ""
    if cmd_err:
        logger.warning(_("探测实例 {} GTID 失败: {}").format(address, cmd_err))
        return False

    gtid_mode = ""
    for row in cmd_results[0].get("table_data") or []:
        if str(row.get("Variable_name", "")).lower() == "gtid_mode":
            gtid_mode = str(row.get("Value") or "").strip()
            break

    enabled = gtid_mode.upper() == "ON"
    logger.info(_("实例 {} gtid_mode={}").format(address, gtid_mode or _("无")))
    return enabled


# 兼容旧名
probe_source_enable_gtid = probe_instance_gtid_enabled


def _collect_target_gtid_probe_endpoints(cluster: Cluster, migrate_type: str) -> list[tuple[str, int, int]]:
    """收集目标侧用于 GTID 探测的真实 MySQL 存储端点。

    TenDBCluster 不探测 Spider（代理上 gtid_mode 不可靠），只探测 RemoteDB。
    """
    bk_cloud_id = cluster.bk_cloud_id
    endpoints: list[tuple[str, int, int]] = []
    if migrate_type == MigrateType.HA_TO_CLUSTER.value or cluster.cluster_type == ClusterType.TenDBCluster.value:
        for storage in cluster.storageinstance_set.filter(instance_role=InstanceRole.REMOTE_MASTER):
            endpoints.append((storage.machine.ip, storage.port, bk_cloud_id))
        if not endpoints:
            logger.warning(_("目标集群 {} 未找到 Remote Master，跳过目标 GTID 探测").format(cluster.id))
        return endpoints

    master = cluster.storageinstance_set.filter(instance_role=InstanceRole.BACKEND_MASTER).first()
    if master:
        endpoints.append((master.machine.ip, master.port, bk_cloud_id))
    return endpoints


def decide_enable_gtid(
    *,
    source_host: str,
    source_port: int,
    source_cluster: Cluster,
    target_cluster: Cluster | None,
    migrate_type: str = "",
) -> bool:
    """跨版本/跨架构迁移时决定 Source.enable_gtid。

    DTS 的 enable_gtid 作用在读源 binlog；但跨版本场景下若目标无 GTID、源开 GTID，
    仍建议走 binlog 位点，避免后续运维/校验假设不一致。

    规则：源端 + 目标侧所有探测点均为 gtid_mode=ON 才返回 True；任一端 OFF/探测失败 → False。
    """
    source_ok = probe_instance_gtid_enabled(host=source_host, port=source_port, bk_cloud_id=source_cluster.bk_cloud_id)
    if not source_ok:
        logger.info(_("源端 {}:{} 未开启 GTID，enable_gtid=False").format(source_host, source_port))
        return False

    if not target_cluster:
        logger.warning(_("未传入目标集群，仅源端开启 GTID，为安全起见 enable_gtid=False"))
        return False

    target_eps = _collect_target_gtid_probe_endpoints(target_cluster, migrate_type)
    if not target_eps:
        logger.warning(_("目标集群 {} 无可用 GTID 探测点，enable_gtid=False").format(target_cluster.id))
        return False

    for host, port, bk_cloud_id in target_eps:
        if not probe_instance_gtid_enabled(host=host, port=port, bk_cloud_id=bk_cloud_id):
            logger.info(_("目标端 {}:{} 未开启 GTID（跨版本/混部），enable_gtid=False").format(host, port))
            return False

    logger.info(_("源/目标均已开启 GTID（目标探测点 {} 个），enable_gtid=True").format(len(target_eps)))
    return True


def build_create_source_request(
    source_spec: SourceSpec,
    cluster: Cluster,
    *,
    user: str,
    password: str,
    worker_name: str | None = None,
    target_cluster: Cluster | None = None,
    migrate_type: str = "",
) -> CreateSourceRequest:
    host, port = resolve_source_endpoint(source_spec, cluster)
    cluster_type = "mysql"
    spider: SpiderInfo | None = None
    # TenDBCluster：仅在已填分片元数据时下发 spider-shard + SpiderInfo（由 expand helper 填充）
    # 未展开时保持兼容：cluster_type=spider、无 SpiderInfo（现网 HA 单据不会走 Cluster 源）
    if cluster.cluster_type == ClusterType.TenDBCluster.value:
        if source_spec.shard_index is not None and source_spec.shard_count is not None:
            cluster_type = "spider-shard"
            spider = SpiderInfo(
                cluster_id=source_spec.spider_cluster_id or cluster.immute_domain,
                shard_index=int(source_spec.shard_index),
                shard_count=int(source_spec.shard_count),
            )
        else:
            cluster_type = "spider"
    enable_gtid = decide_enable_gtid(
        source_host=host,
        source_port=port,
        source_cluster=cluster,
        target_cluster=target_cluster,
        migrate_type=migrate_type,
    )
    bind_worker = worker_name or source_spec.worker_name or None
    source = Source(
        source_name=source_spec.source_name,
        host=host,
        port=port,
        user=user,
        password=password,
        enable_gtid=enable_gtid,
        enable=True,
        cluster_type=cluster_type,
        spider=spider,
    )
    return CreateSourceRequest(source=source, worker_name=bind_worker)


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


def apply_myloader_dirs_to_sources(task_spec: DtsTaskSpec, dirs: dict[str, str]) -> None:
    """将下发后的 myloader_dir 写回各 SourceSpec.myloader。"""
    for src in task_spec.sources:
        path = dirs.get(src.source_name)
        if not path:
            continue
        if src.myloader is None:
            from backend.flow.utils.mysql.dts.migrate_plan import MyloaderSpec

            src.myloader = MyloaderSpec()
        src.myloader.myloader_dir = path


def _resolve_myloader_task_mode(cfg_task_mode: str) -> str:
    mode = (cfg_task_mode or "").strip()
    if mode in ("myloader", "myloader&sync"):
        return mode
    if mode == "full":
        return "myloader"
    # all / incremental / 空 → 默认全量+增量
    return "myloader&sync"


def _build_myloader_config_for_source(src, cfg) -> MyLoaderConfig:
    from backend.flow.utils.mysql.dts.constants import DEFAULT_MYLOADER_PATH

    ml = src.myloader
    if ml is None:
        raise ValueError(_("source {} 使用 myloader 时必须提供 myloader 配置").format(src.source_name))
    if not ml.myloader_dir:
        raise ValueError(_("source {} 的 myloader_dir 为空，请先完成全备下发").format(src.source_name))
    path = ml.myloader_path or DEFAULT_MYLOADER_PATH
    return MyLoaderConfig(
        myloader_path=path,
        myloader_dir=ml.myloader_dir,
        myloader_threads=ml.threads or 16,
        myloader_regex=ml.regex or "",
        myloader_sourcedb=ml.sourcedb or "",
        myloader_tablelist=ml.tablelist or "",
        myloader_setnames=ml.setnames or "",
        myloader_defaultsfile=ml.defaultsfile or "",
        myloader_extraargs=ml.extraargs or "",
    )


def build_dts_task_request(
    plan: DtsMigratePlan,
    task_spec: DtsTaskSpec,
    *,
    user: str,
    password: str,
) -> CreateTaskRequest:
    table_rules: list[TableMigrateRule] = []
    binlog_filters: dict[str, BinlogFilterRuleEntry] = {}
    for src in task_spec.sources:
        table_rules.extend(_build_table_migrate_rules(src.source_name, src.sync_scope))
        binlog_filters.update(_build_binlog_filter_rules(src.sync_scope))

    if not table_rules:
        # 引擎侧空 table_migrate_rule 等价于全库迁移，与「空 sync_scope=不同步」语义冲突，必须拦截
        raise ValueError(_("同步范围为空，拒绝创建 DTS 任务（空 table_migrate_rule 在引擎侧等价于全库迁移）"))

    target_cfg = task_spec.target_config
    if not target_cfg or not target_cfg.host:
        target_cfg = build_target_config(task_spec.target_cluster_id, plan.migrate_type, user, password)

    cfg = task_spec.dts_task_config
    use_myloader = cfg.full_load_engine == FullLoadEngine.MYLOADER.value

    if use_myloader:
        myloaders: dict[str, MyLoaderConfig] = {}
        source_conf: list[SourceConfItem] = []
        for src in task_spec.sources:
            conf_name = f"myloader-{src.source_name}"
            myloaders[conf_name] = _build_myloader_config_for_source(src, cfg)
            source_conf.append(SourceConfItem(source_name=src.source_name, myloader_config_name=conf_name))
        task_mode = _resolve_myloader_task_mode(cfg.task_mode)
        source_config = SourceConfig(
            source_conf=source_conf,
            full_migrate_conf=None,
            incr_migrate_conf=IncrMigrateConfig(**cfg.incr_migrate) if cfg.incr_migrate else None,
            myloaders=myloaders,
        )
    else:
        source_conf = [SourceConfItem(source_name=src.source_name) for src in task_spec.sources]
        task_mode = cfg.task_mode
        source_config = SourceConfig(
            source_conf=source_conf,
            full_migrate_conf=FullMigrateConfig(**cfg.full_migrate) if cfg.full_migrate else None,
            incr_migrate_conf=IncrMigrateConfig(**cfg.incr_migrate) if cfg.incr_migrate else None,
        )

    task = Task(
        name=task_spec.task_name,
        task_mode=task_mode,
        shard_mode=cfg.shard_mode or "",
        on_duplicate=cfg.on_duplicate,
        meta_schema=cfg.meta_schema,
        ignore_checking_items=cfg.ignore_checking_items,
        target_config=target_cfg,
        source_config=source_config,
        table_migrate_rule=table_rules,
        binlog_filter_rule=binlog_filters,
    )
    return CreateTaskRequest(task=task)
