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
from dataclasses import dataclass, field
from typing import Any

from backend.components.mysqldtsapi.types import TargetConfig
from backend.flow.utils.mysql.dts.constants import (
    DtsLifecycleMode,
    FullLoadEngine,
    MigrateTopology,
    MigrateType,
    get_default_deploy_path,
)
from backend.flow.utils.mysql.dts.context import DtsHostSpec, MysqlDtsDeploySubflowInput


@dataclass
class SyncScope:
    do_dbs: list[str] = field(default_factory=list)
    ignore_dbs: list[str] = field(default_factory=list)
    do_tables: list[dict] = field(default_factory=list)
    ignore_tables: list[dict] = field(default_factory=list)
    table_routes: list[dict] = field(default_factory=list)
    binlog_filters: list[dict] = field(default_factory=list)


@dataclass
class MyloaderSpec:
    backup_id: str = ""
    backup_source: str = "remote"
    myloader_path: str = ""
    myloader_dir: str = ""
    threads: int = 16
    regex: str = ""
    sourcedb: str = ""
    tablelist: str = ""
    setnames: str = ""
    defaultsfile: str = ""
    extraargs: str = ""
    dest_worker_ip: str = ""
    shard_id: int | None = None


@dataclass
class DtsTaskConfig:
    task_mode: str = "all"
    enable_validator: bool = False
    shard_mode: str = ""
    on_duplicate: str = "replace"
    meta_schema: str = "dm_meta"
    ignore_checking_items: list[str] = field(default_factory=list)
    full_migrate: dict = field(default_factory=dict)
    incr_migrate: dict = field(default_factory=dict)
    full_load_engine: str = FullLoadEngine.BUILTIN.value
    myloader: MyloaderSpec | None = None


@dataclass
class SourceSpec:
    cluster_id: int
    source_name: str
    sync_scope: SyncScope
    source_instance_id: int | None = None
    source_instance_role: str | None = None
    source_host: str | None = None
    myloader: MyloaderSpec | None = None


@dataclass
class DtsTaskSpec:
    task_name: str
    target_cluster_id: int
    sources: list[SourceSpec]
    target_config: TargetConfig | None = None
    sync_scope_merged: list[dict] = field(default_factory=list)
    dts_task_config: DtsTaskConfig = field(default_factory=DtsTaskConfig)


@dataclass
class DtsMigratePlan:
    topology: str
    migrate_type: str
    dts_cluster_id: int | None
    dts_lifecycle: str
    auto_deploy_dts: bool
    deploy_subflow_inp: MysqlDtsDeploySubflowInput | None
    cleanup_after_migrate: bool
    recycle_dts_hosts: bool
    dts_task_config: DtsTaskConfig
    task_specs: list[DtsTaskSpec]
    worker_count_required: int
    bk_biz_id: int = 0
    bk_cloud_id: int = 0


def _parse_sync_scope(raw: dict | None) -> SyncScope:
    raw = raw or {}
    return SyncScope(
        do_dbs=raw.get("do_dbs", []),
        ignore_dbs=raw.get("ignore_dbs", []),
        do_tables=raw.get("do_tables", []),
        ignore_tables=raw.get("ignore_tables", []),
        table_routes=raw.get("table_routes", []),
        binlog_filters=raw.get("binlog_filters", []),
    )


def _parse_myloader_spec(raw: dict | None) -> MyloaderSpec | None:
    if not raw or not isinstance(raw, dict):
        return None
    return MyloaderSpec(
        backup_id=raw.get("backup_id", "") or "",
        backup_source=raw.get("backup_source", "remote") or "remote",
        myloader_path=raw.get("myloader_path", "") or "",
        myloader_dir=raw.get("myloader_dir") or raw.get("directory") or "",
        threads=int(raw.get("threads", 16) or 16),
        regex=raw.get("regex", "") or "",
        sourcedb=raw.get("sourcedb", "") or "",
        tablelist=raw.get("tablelist", "") or "",
        setnames=raw.get("setnames", "") or "",
        defaultsfile=raw.get("defaultsfile") or raw.get("defaults_file") or "",
        extraargs=raw.get("extraargs") or raw.get("extra_args") or "",
        dest_worker_ip=raw.get("dest_worker_ip", "") or "",
        shard_id=raw.get("shard_id"),
    )


def _copy_myloader_spec(spec: MyloaderSpec | None) -> MyloaderSpec | None:
    if spec is None:
        return None
    return MyloaderSpec(
        backup_id=spec.backup_id,
        backup_source=spec.backup_source,
        myloader_path=spec.myloader_path,
        myloader_dir=spec.myloader_dir,
        threads=spec.threads,
        regex=spec.regex,
        sourcedb=spec.sourcedb,
        tablelist=spec.tablelist,
        setnames=spec.setnames,
        defaultsfile=spec.defaultsfile,
        extraargs=spec.extraargs,
        dest_worker_ip=spec.dest_worker_ip,
        shard_id=spec.shard_id,
    )


def _parse_dts_task_config(raw: dict | None) -> DtsTaskConfig:
    raw = raw or {}
    return DtsTaskConfig(
        task_mode=raw.get("task_mode", "all"),
        enable_validator=raw.get("enable_validator", False),
        shard_mode=raw.get("shard_mode", ""),
        on_duplicate=raw.get("on_duplicate", "replace"),
        meta_schema=raw.get("meta_schema", "dm_meta"),
        ignore_checking_items=raw.get("ignore_checking_items", []),
        full_migrate=raw.get("full_migrate", {}),
        incr_migrate=raw.get("incr_migrate", {}),
        full_load_engine=raw.get("full_load_engine", FullLoadEngine.BUILTIN.value),
        myloader=_parse_myloader_spec(raw.get("myloader")),
    )


def _parse_deploy_subflow_inp(details: dict[str, Any]) -> MysqlDtsDeploySubflowInput | None:
    """从单据 details 解析自动部署入参。"""
    if details.get("deploy_subflow_inp") and isinstance(details["deploy_subflow_inp"], MysqlDtsDeploySubflowInput):
        return details["deploy_subflow_inp"]

    raw = details.get("deploy_subflow") or details.get("deploy_subflow_inp")
    if not raw or not isinstance(raw, dict):
        return None

    cluster_name = (
        raw.get("cluster_name") or details.get("cluster_name") or f"dts-migrate-{details.get('ticket_id', 0)}"
    )
    master_hosts = [
        DtsHostSpec(ip=h["ip"], bk_cloud_id=h["bk_cloud_id"], name=h.get("name")) for h in raw.get("master_hosts", [])
    ]
    worker_hosts = [
        DtsHostSpec(ip=h["ip"], bk_cloud_id=h["bk_cloud_id"], name=h.get("name")) for h in raw.get("worker_hosts", [])
    ]
    if not master_hosts or not worker_hosts:
        return None
    return MysqlDtsDeploySubflowInput(
        root_id=raw.get("root_id", ""),
        bk_biz_id=int(raw.get("bk_biz_id") or details.get("bk_biz_id", 0)),
        bk_cloud_id=int(raw.get("bk_cloud_id") or details.get("bk_cloud_id", 0)),
        cluster_name=cluster_name,
        master_hosts=master_hosts,
        worker_hosts=worker_hosts,
        deploy_path=raw.get("deploy_path") or get_default_deploy_path(cluster_name),
        master_ha=bool(raw.get("master_ha", False)),
        # 介质默认取最新包，不由单据指定
        creator=raw.get("creator", ""),
    )


def _parse_source_spec(raw: dict, default_name: str, task_myloader: MyloaderSpec | None = None) -> SourceSpec:
    myloader = _parse_myloader_spec(raw.get("myloader"))
    if myloader is None:
        myloader = _copy_myloader_spec(task_myloader)
    return SourceSpec(
        cluster_id=raw["cluster_id"],
        source_name=raw.get("source_name", default_name),
        sync_scope=_parse_sync_scope(raw.get("sync_scope")),
        source_instance_id=raw.get("source_instance_id"),
        source_instance_role=raw.get("source_instance_role"),
        source_host=raw.get("source_host"),
        myloader=myloader,
    )


def _build_one_to_one_plan(details: dict[str, Any]) -> DtsMigratePlan:
    spec = details["one_to_one"]
    task_cfg = _parse_dts_task_config(details.get("dts_task_config"))
    src = _parse_source_spec(spec["src_info"], "source-1", task_myloader=task_cfg.myloader)
    task_spec = DtsTaskSpec(
        task_name=spec.get("task_name", "migrate-task-1"),
        target_cluster_id=spec["dst_info"]["cluster_id"],
        sources=[src],
        dts_task_config=task_cfg,
    )
    return _wrap_plan(details, [task_spec], worker_count=2)


def _build_many_to_one_plan(details: dict[str, Any]) -> DtsMigratePlan:
    spec = details["many_to_one"]
    task_cfg = _parse_dts_task_config(details.get("dts_task_config"))
    sources = [
        _parse_source_spec(src, f"source-{idx + 1}", task_myloader=task_cfg.myloader)
        for idx, src in enumerate(spec["src_infos"])
    ]
    task_spec = DtsTaskSpec(
        task_name=spec.get("task_name", "migrate-task-1"),
        target_cluster_id=spec["dst_info"]["cluster_id"],
        sources=sources,
        dts_task_config=task_cfg,
    )
    return _wrap_plan(details, [task_spec], worker_count=len(sources) + 1)


def _build_one_to_many_plan(details: dict[str, Any]) -> DtsMigratePlan:
    spec = details["one_to_many"]
    src_info = spec["src_info"]
    task_cfg = _parse_dts_task_config(details.get("dts_task_config"))
    task_specs = []
    for idx, dst in enumerate(spec["dst_infos"]):
        src = _parse_source_spec(
            {**src_info, "source_name": f"source-{idx + 1}"},
            f"source-{idx + 1}",
            task_myloader=task_cfg.myloader,
        )
        task_specs.append(
            DtsTaskSpec(
                task_name=dst.get("task_name", f"migrate-task-{idx + 1}"),
                target_cluster_id=dst["cluster_id"],
                sources=[src],
                dts_task_config=task_cfg,
            )
        )
    return _wrap_plan(details, task_specs, worker_count=len(task_specs) + 1)


def _wrap_plan(details: dict[str, Any], task_specs: list[DtsTaskSpec], worker_count: int) -> DtsMigratePlan:
    dts_lifecycle = details.get("dts_lifecycle")
    if not dts_lifecycle:
        if details.get("dts_cluster_id"):
            dts_lifecycle = DtsLifecycleMode.USE_EXISTING.value
        elif details.get("auto_deploy_dts"):
            dts_lifecycle = DtsLifecycleMode.DEPLOY_EPHEMERAL.value
        else:
            dts_lifecycle = DtsLifecycleMode.USE_EXISTING.value
    return DtsMigratePlan(
        topology=details["migrate_topology"],
        migrate_type=details.get("migrate_type", MigrateType.HA_TO_HA.value),
        dts_cluster_id=details.get("dts_cluster_id"),
        dts_lifecycle=dts_lifecycle,
        auto_deploy_dts=details.get("auto_deploy_dts", False),
        deploy_subflow_inp=_parse_deploy_subflow_inp(details),
        cleanup_after_migrate=details.get(
            "cleanup_after_migrate", dts_lifecycle == DtsLifecycleMode.DEPLOY_EPHEMERAL.value
        ),
        recycle_dts_hosts=details.get("recycle_dts_hosts", True),
        dts_task_config=_parse_dts_task_config(details.get("dts_task_config")),
        task_specs=task_specs,
        worker_count_required=max(worker_count, details.get("worker_count_required", worker_count)),
        bk_biz_id=details.get("bk_biz_id", 0),
        bk_cloud_id=details.get("bk_cloud_id", 0),
    )


def build_migrate_plan(ticket_details: dict[str, Any]) -> DtsMigratePlan:
    topology = ticket_details["migrate_topology"]
    builders = {
        MigrateTopology.ONE_TO_ONE.value: _build_one_to_one_plan,
        MigrateTopology.MANY_TO_ONE.value: _build_many_to_one_plan,
        MigrateTopology.ONE_TO_MANY.value: _build_one_to_many_plan,
    }
    builder = builders.get(topology)
    if not builder:
        raise ValueError(f"unsupported topology: {topology}")
    return builder(ticket_details)
