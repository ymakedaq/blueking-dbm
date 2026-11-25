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
from backend.flow.utils.mysql.dts.migrate_credentials import (
    DtsGrantTarget,
    generate_dts_migrate_credentials,
    resolve_dts_grant_hosts,
)
from backend.flow.utils.mysql.dts.migrate_helper import (
    _append_grant_target,
    _collect_target_grant_specs,
    resolve_dump_endpoint,
    resolve_source_endpoint,
)
from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan

# 切换加锁 + 校验需要连源集群 Spider；SPIDER_SLAVE 只读不锁，不授权
_SPIDER_GRANT_ROLES = (TenDBClusterSpiderRole.SPIDER_MASTER, TenDBClusterSpiderRole.SPIDER_MNT)


def _append_spider_grant_targets(targets: dict, cluster: Cluster) -> None:
    """源 TenDBCluster 的 SPIDER_MASTER / SPIDER_MNT 授权点（cutover 加锁、临时账号回收）。"""
    proxies = cluster.proxyinstance_set.filter(tendbclusterspiderext__spider_role__in=_SPIDER_GRANT_ROLES)
    for proxy in proxies:
        # Spider 是 MariaDB，不授 BACKUP_ADMIN
        _append_grant_target(targets, cluster, proxy.machine.ip, proxy.port, allow_backup_admin=False)


def collect_spider_migrate_grant_targets(plan: DtsMigratePlan) -> list[DtsGrantTarget]:
    """cluster_to_cluster 迁移需创建 DTS 临时账号的实例集合。

    源端（TenDBCluster）：每个分片 remote master（binlog）+ remote slave（全量导出）+ SPIDER_MASTER / SPIDER_MNT；
    目标端：复用 mysql/dts 的目标授权点（Spider master / Primary tdbctl / remote master）。
    """
    targets: dict[str, DtsGrantTarget] = {}
    for task_spec in plan.task_specs:
        engine = task_spec.dts_task_config.full_load_engine if task_spec.dts_task_config else ""
        source_cluster_ids: list[int] = []
        for source_spec in task_spec.sources:
            cluster = Cluster.objects.get(id=source_spec.cluster_id)
            if cluster.cluster_type != ClusterType.TenDBCluster.value:
                raise ValueError(_("cluster_to_cluster 迁移的源集群 {} 不是 TenDBCluster").format(cluster.id))
            if source_spec.cluster_id not in source_cluster_ids:
                source_cluster_ids.append(source_spec.cluster_id)
            ip, port = resolve_source_endpoint(source_spec, cluster, engine)
            _append_grant_target(targets, cluster, ip, port)
            dump = resolve_dump_endpoint(source_spec, cluster, engine)
            if dump:
                _append_grant_target(targets, cluster, dump[0], dump[1])
        for cluster_id in source_cluster_ids:
            _append_spider_grant_targets(targets, Cluster.objects.get(id=cluster_id))
        target_cluster = Cluster.objects.get(id=task_spec.target_cluster_id)
        for spec in _collect_target_grant_specs(target_cluster, plan.migrate_type):
            _append_grant_target(
                targets,
                target_cluster,
                spec.ip,
                spec.port,
                allow_backup_admin=spec.allow_backup_admin,
                primary_tdbctl=spec.primary_tdbctl,
            )
    return list(targets.values())


def resolve_cluster_to_cluster_temp_account_for_pipeline(
    plan: DtsMigratePlan,
) -> tuple[str, str, list[str], list[DtsGrantTarget]]:
    """建流期解析授权并生成 cluster_to_cluster 临时账号（与 dts-task-clean 同源）。"""
    grant_hosts = resolve_dts_grant_hosts(plan)
    grant_targets = collect_spider_migrate_grant_targets(plan)
    if not grant_hosts:
        raise ValueError(_("未解析到 DTS Worker IP，拒绝使用 %% 授权，请先确保 DTS 集群已部署或填写 deploy"))
    if not grant_targets:
        raise ValueError(_("未找到需要授权的迁移实例"))
    dts_user, dts_password = generate_dts_migrate_credentials()
    return dts_user, dts_password, grant_hosts, grant_targets
