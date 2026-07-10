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
from typing import List, Optional

from django.db import transaction
from django.utils.translation import gettext as _

from backend.db_meta.enums import ClusterPhase, ClusterStatus
from backend.db_meta.models import Cluster, MysqlDtsCluster
from backend.db_meta.models.mysql_dts import MysqlDtsClusterStatus

logger = logging.getLogger("root")


@transaction.atomic
def decommission(
    dts_cluster_id: int,
    recycle_hosts: bool = True,
    target_hosts: Optional[List[dict]] = None,
    updater: str = "",
):
    dts_cluster = MysqlDtsCluster.objects.get(id=dts_cluster_id)
    dts_cluster.status = MysqlDtsClusterStatus.DESTROYED.value
    dts_cluster.updater = updater
    dts_cluster.save(update_fields=["status", "updater", "update_at"])

    if dts_cluster.cluster_id:
        cluster = Cluster.objects.filter(id=dts_cluster.cluster_id).first()
        if cluster:
            cluster.phase = ClusterPhase.OFFLINE.value
            cluster.status = ClusterStatus.ABNORMAL.value
            cluster.updater = updater
            cluster.save(update_fields=["phase", "status", "updater", "update_at"])

    if recycle_hosts:
        ips = _collect_host_ips(dts_cluster, target_hosts)
        logger.info(_("回收 DTS 主机到资源池: {}").format(ips))
    logger.info(_("MySQL DTS 集群下线完成: id={}").format(dts_cluster_id))


def _collect_host_ips(dts_cluster: MysqlDtsCluster, target_hosts: Optional[List[dict]]) -> list[str]:
    if target_hosts:
        return [h["ip"] for h in target_hosts]
    ips = set()
    for node in dts_cluster.master_nodes + dts_cluster.worker_nodes:
        ips.add(node["ip"])
    return list(ips)
