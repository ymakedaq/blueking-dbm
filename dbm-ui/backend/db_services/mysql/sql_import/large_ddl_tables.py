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
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from backend.components.sql_import.client import SQLSimulationApi
from backend.db_meta.enums import InstanceInnerRole
from backend.db_meta.models import Cluster
from backend.db_report.models import MysqlDbTableSize
from backend.db_services.mysql.remote_service.handlers import RemoteServiceHandler
from backend.db_services.mysql.sql_import.constants import CACHE_SEMANTIC_DATA_FIELD
from backend.db_services.mysql.sql_import.exceptions import SQLImportBaseException
from backend.flow.engine.bamboo.engine import BambooEngine
from backend.flow.models import FlowNode
from backend.ticket.constants import TicketType
from backend.ticket.models import Ticket
from backend.utils.cache import cache

logger = logging.getLogger("root")

# 超过该字节数才算大表（严格大于）
LARGE_DDL_TABLE_SIZE_BYTES = 100 * 1024 * 1024
_SIZE_LOOKBACK_HOURS = 48
_SIZE_ROLE_FALLBACKS = (
    InstanceInnerRole.SLAVE.value,
    InstanceInnerRole.ORPHAN.value,
    InstanceInnerRole.MASTER.value,
)
_WILDCARD_MARKERS = ("%", "*", "?")
_SQL_TYPE_ORDER = ("alter_table", "drop_table", "truncate")
_DDL_GROUPS = (
    ("alter_tables", "alters", "alter_table"),
    ("drop_tables", "tables", "drop_table"),
    ("truncate_tables", "tables", "truncate"),
)
_SQL_IMPORT_TICKET_TYPES = frozenset(
    {
        TicketType.MYSQL_IMPORT_SQLFILE.value,
        TicketType.MYSQL_FORCE_IMPORT_SQLFILE.value,
        TicketType.TENDBCLUSTER_IMPORT_SQLFILE.value,
        TicketType.TENDBCLUSTER_FORCE_IMPORT_SQLFILE.value,
    }
)

# (file_name, parsed_db_name, table_name) -> sql_types
DdlRefKey = Tuple[str, str, str]
TablePair = Tuple[str, str]


def query_large_ddl_tables(
    cluster_ids: List[int],
    path: str,
    execute_objects: List[Dict],
    *,
    min_size_bytes: int = LARGE_DDL_TABLE_SIZE_BYTES,
) -> List[Dict]:
    """
    根据 SQL 变更提单入参，返回 ALTER / DROP / TRUNCATE 涉及且表大小超过阈值的大表。

    粒度是 (cluster, db_name, table_name, file_name)，不跨文件合并。
    """
    sql_files = _collect_sql_files(execute_objects)
    if not cluster_ids or not sql_files:
        return []

    summary = _parse_file_statement(path, sql_files)
    refs = _collect_ddl_refs(summary)
    if not refs:
        return []

    results: List[Dict] = []
    for cluster in _load_clusters(cluster_ids):
        results.extend(
            _query_cluster_large_tables(
                cluster=cluster,
                refs=refs,
                execute_objects=execute_objects,
                min_size_bytes=min_size_bytes,
            )
        )
    return results


def query_large_ddl_tables_by_ticket(
    ticket_id: int,
    *,
    min_size_bytes: int = LARGE_DDL_TABLE_SIZE_BYTES,
) -> List[Dict]:
    """
    根据 SQL 导入单据 ID 查询此次变更中 ALTER / DROP / TRUNCATE 涉及的大表。
    入参从单据 details 取 path / cluster_ids / execute_objects；缺省时回退语义执行 global_data。
    """
    ticket = _get_sql_import_ticket(ticket_id)
    cluster_ids, path, execute_objects = _extract_sql_import_params(ticket)
    return query_large_ddl_tables(
        cluster_ids,
        path,
        execute_objects,
        min_size_bytes=min_size_bytes,
    )


def _get_sql_import_ticket(ticket_id: int) -> Ticket:
    try:
        ticket = Ticket.objects.get(id=ticket_id)
    except Ticket.DoesNotExist:
        raise SQLImportBaseException(_("SQL导入单据 {} 不存在").format(ticket_id))
    if ticket.ticket_type not in _SQL_IMPORT_TICKET_TYPES:
        raise SQLImportBaseException(_("单据 {} 类型 {} 不是 SQL 导入单据").format(ticket_id, ticket.ticket_type))
    return ticket


def _extract_sql_import_params(ticket: Ticket) -> Tuple[List[int], str, List[Dict]]:
    details = ticket.details or {}
    cluster_ids = list(details.get("cluster_ids") or [])
    path = str(details.get("path") or "")
    execute_objects = list(details.get("execute_objects") or [])
    if cluster_ids and path and execute_objects:
        return cluster_ids, path, execute_objects

    root_id = details.get("root_id")
    if not root_id:
        raise SQLImportBaseException(_("单据 {} 缺少 SQL 导入入参 path/cluster_ids/execute_objects").format(ticket.id))
    semantic = _load_semantic_details(str(root_id))
    cluster_ids = cluster_ids or list(semantic.get("cluster_ids") or [])
    path = path or str(semantic.get("path") or "")
    execute_objects = execute_objects or list(semantic.get("execute_objects") or [])
    if not cluster_ids or not path or not execute_objects:
        raise SQLImportBaseException(_("单据 {} 无法从语义执行 {} 还原 SQL 导入入参").format(ticket.id, root_id))
    return cluster_ids, path, execute_objects


def _load_semantic_details(root_id: str) -> Dict[str, Any]:
    first_node = FlowNode.objects.filter(root_id=root_id).first()
    if first_node:
        try:
            details = BambooEngine(root_id=root_id).get_node_input_data(node_id=first_node.node_id).data["global_data"]
            if isinstance(details, dict):
                return details
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(_("读取语义执行 {} pipeline 入参失败: {}").format(root_id, e))
    cached = cache.get(CACHE_SEMANTIC_DATA_FIELD.format(root_id=root_id))
    return cached if isinstance(cached, dict) else {}


def _collect_sql_files(execute_objects: Optional[List[Dict]]) -> List[str]:
    files: List[str] = []
    seen: Set[str] = set()
    for obj in execute_objects or []:
        for name in obj.get("sql_files") or []:
            if name and name not in seen:
                seen.add(name)
                files.append(name)
    return files


def _parse_file_statement(path: str, files: List[str]) -> Dict[str, Any]:
    try:
        result = SQLSimulationApi.parse_file_statement(
            params={"path": path, "files": files, "include_sql_text": False},
        )
    except Exception as e:  # pylint: disable=broad-except
        raise SQLImportBaseException(_("解析 SQL 文件语句失败: {}").format(e))
    return _unwrap_parse_result(result)


def _unwrap_parse_result(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        raise SQLImportBaseException(_("解析 SQL 文件语句返回格式非法"))
    if _looks_like_statement_summary(result):
        return result
    data = result.get("data")
    if isinstance(data, dict) and _looks_like_statement_summary(data):
        return data
    raise SQLImportBaseException(_("解析 SQL 文件语句返回格式非法"))


def _looks_like_statement_summary(payload: Dict[str, Any]) -> bool:
    return any(key in payload for key in ("command_counts", "alter_tables", "drop_tables", "truncate_tables"))


def _collect_ddl_refs(summary: Dict[str, Any]) -> Dict[DdlRefKey, Set[str]]:
    refs: Dict[DdlRefKey, Set[str]] = {}
    for group_key, item_key, sql_type in _DDL_GROUPS:
        for file_group in summary.get(group_key) or []:
            file_name = str(file_group.get("file_name") or "")
            if not file_name:
                continue
            for item in file_group.get(item_key) or []:
                table_name = str(item.get("table_name") or "")
                if not table_name:
                    continue
                parsed_db = str(item.get("db_name") or "")
                key = (file_name, parsed_db, table_name)
                refs.setdefault(key, set()).add(sql_type)
    return refs


def _load_clusters(cluster_ids: List[int]) -> List[Cluster]:
    clusters = list(Cluster.objects.filter(id__in=cluster_ids))
    found_ids = {c.id for c in clusters}
    for cluster_id in cluster_ids:
        if cluster_id not in found_ids:
            logger.warning(_("集群 {} 不存在，跳过大表查询").format(cluster_id))
    # 保持入参 cluster_ids 顺序
    cluster_map = {c.id: c for c in clusters}
    return [cluster_map[cid] for cid in cluster_ids if cid in cluster_map]


def _query_cluster_large_tables(
    cluster: Cluster,
    refs: Dict[DdlRefKey, Set[str]],
    execute_objects: List[Dict],
    min_size_bytes: int,
) -> List[Dict]:
    expand_cache: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[str]] = {}
    needed: List[Tuple[str, str, str, List[str]]] = []
    pairs: Set[TablePair] = set()
    for (file_name, parsed_db, table_name), sql_types in refs.items():
        real_dbs = _resolve_real_dbs(cluster, file_name, parsed_db, execute_objects, expand_cache)
        ordered_types = _ordered_sql_types(sql_types)
        for db_name in real_dbs:
            needed.append((file_name, db_name, table_name, ordered_types))
            pairs.add((db_name, table_name))

    if not pairs:
        return []

    sizes = _query_table_sizes(cluster.immute_domain, pairs)
    results: List[Dict] = []
    for file_name, db_name, table_name, sql_types in needed:
        table_size = sizes.get((db_name, table_name))
        if table_size is None or table_size <= min_size_bytes:
            continue
        results.append(
            {
                "cluster_id": cluster.id,
                "cluster_domain": cluster.immute_domain,
                "db_name": db_name,
                "table_name": table_name,
                "table_size": table_size,
                "sql_types": sql_types,
                "file_name": file_name,
            }
        )
    return results


def _ordered_sql_types(sql_types: Set[str]) -> List[str]:
    return [sql_type for sql_type in _SQL_TYPE_ORDER if sql_type in sql_types]


def _resolve_real_dbs(
    cluster: Cluster,
    file_name: str,
    parsed_db: str,
    execute_objects: List[Dict],
    expand_cache: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[str]],
) -> List[str]:
    if parsed_db:
        return [parsed_db]

    real_dbs: List[str] = []
    seen: Set[str] = set()
    matched = False
    for obj in execute_objects or []:
        if file_name not in (obj.get("sql_files") or []):
            continue
        matched = True
        for db_name in _dbs_from_execute_object(cluster, obj, expand_cache):
            if db_name and db_name not in seen:
                seen.add(db_name)
                real_dbs.append(db_name)

    if not matched:
        logger.warning(_("文件 {} 未出现在 execute_objects.sql_files 中，跳过空库名表").format(file_name))
    elif not real_dbs:
        logger.warning(_("集群 {} 文件 {} 未能解析出真实库名，跳过大表查询").format(cluster.id, file_name))
    return real_dbs


def _dbs_from_execute_object(
    cluster: Cluster,
    obj: Dict,
    expand_cache: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[str]],
) -> List[str]:
    dbnames = [str(name) for name in (obj.get("dbnames") or []) if name]
    ignore_dbnames = [str(name) for name in (obj.get("ignore_dbnames") or []) if name]
    if not dbnames:
        return []
    if _has_wildcard(dbnames) or _has_wildcard(ignore_dbnames):
        return _expand_db_patterns(cluster, dbnames, ignore_dbnames, expand_cache)
    ignore_set = set(ignore_dbnames)
    return [name for name in dbnames if name not in ignore_set]


def _has_wildcard(names: Iterable[str]) -> bool:
    return any(any(marker in name for marker in _WILDCARD_MARKERS) for name in names)


def _expand_db_patterns(
    cluster: Cluster,
    dbnames: List[str],
    ignore_dbnames: List[str],
    expand_cache: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[str]],
) -> List[str]:
    cache_key = (tuple(dbnames), tuple(ignore_dbnames))
    if cache_key in expand_cache:
        return expand_cache[cache_key]
    try:
        databases = RemoteServiceHandler(bk_biz_id=cluster.bk_biz_id).show_database_with_pattern(
            cluster.id, dbnames, ignore_dbnames
        )
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(_("集群 {} 按通配扩库失败 dbs={} ignore={}: {}").format(cluster.id, dbnames, ignore_dbnames, e))
        databases = []
    real_dbs = [str(name) for name in (databases or []) if name]
    if not real_dbs:
        logger.warning(_("集群 {} 通配库名未匹配到真实 database: dbs={}").format(cluster.id, dbnames))
    expand_cache[cache_key] = real_dbs
    return real_dbs


def _query_table_sizes(cluster_domain: str, pairs: Set[TablePair]) -> Dict[TablePair, int]:
    if not cluster_domain or not pairs:
        return {}
    start_time, base_time = _lookback_window()
    db_names = list({pair[0] for pair in pairs})
    table_names = list({pair[1] for pair in pairs})
    merged: Dict[TablePair, int] = {}
    for role in _SIZE_ROLE_FALLBACKS:
        try:
            role_sizes = _sizes_for_role(
                cluster_domain=cluster_domain,
                db_names=db_names,
                table_names=table_names,
                pairs=pairs,
                instance_role=role,
                start_time=start_time,
                base_time=base_time,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(_("查询表容量失败 cluster_domain={} role={}: {}").format(cluster_domain, role, e))
            continue
        for pair, size in role_sizes.items():
            if pair not in merged:
                merged[pair] = size
        if len(merged) == len(pairs):
            break
    return merged


def _lookback_window():
    base_time = timezone.now()
    return base_time - timedelta(hours=_SIZE_LOOKBACK_HOURS), base_time


def _sizes_for_role(
    *,
    cluster_domain: str,
    db_names: List[str],
    table_names: List[str],
    pairs: Set[TablePair],
    instance_role: str,
    start_time,
    base_time,
) -> Dict[TablePair, int]:
    qs = (
        MysqlDbTableSize.objects.filter(
            cluster_domain=cluster_domain,
            instance_role=instance_role,
            dteventtimehour__gte=start_time,
            dteventtimehour__lte=base_time,
            database_name__in=db_names,
            table_name__in=table_names,
        )
        .values("database_name", "table_name", "dteventtimehour")
        .annotate(table_size=Sum("table_size"))
        .order_by("database_name", "table_name", "-dteventtimehour")
    )
    seen: Set[TablePair] = set()
    sizes: Dict[TablePair, int] = {}
    for item in qs:
        pair = (item["database_name"], item["table_name"])
        if pair in seen or pair not in pairs:
            continue
        seen.add(pair)
        if item.get("table_size") is None:
            continue
        sizes[pair] = int(item["table_size"])
    return sizes
