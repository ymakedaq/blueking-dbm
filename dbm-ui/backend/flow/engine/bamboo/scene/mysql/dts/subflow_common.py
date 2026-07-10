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
from backend.flow.utils.mysql.dts.constants import (
    MYSQL_DTS_MASTER_PORT,
    MYSQL_DTS_WORKER_PORT,
    get_default_deploy_path,
)
from backend.flow.utils.mysql.dts.context import DtsHostSpec
from backend.flow.utils.mysql.dts.deploy_helper import (
    DeployedNodeInfo,
    build_master_addr,
    build_master_node_name,
    build_worker_node_name,
    render_master_config,
    render_worker_config,
)


def resolve_deploy_path(cluster_name: str, deploy_path: str = "") -> str:
    return deploy_path or get_default_deploy_path(cluster_name)


def hosts_to_exec_targets(hosts: list[DtsHostSpec]) -> list[dict]:
    return [{"ip": host.ip, "bk_cloud_id": host.bk_cloud_id} for host in hosts]


def build_master_nodes(hosts: list[DtsHostSpec], master_ha: bool = False) -> list[dict]:
    nodes = []
    peer_addrs = [f"{host.ip}:{MYSQL_DTS_MASTER_PORT}" for host in hosts]
    for idx, host in enumerate(hosts, start=1):
        name = host.name or build_master_node_name(idx)
        nodes.append(
            DeployedNodeInfo(
                ip=host.ip,
                bk_cloud_id=host.bk_cloud_id,
                name=name,
                port=MYSQL_DTS_MASTER_PORT,
                role="master",
            ).to_dict()
        )
    return nodes, peer_addrs


def build_worker_nodes(
    hosts: list[DtsHostSpec],
    existing_workers: list[dict] | None = None,
    name_offset: int = 0,
) -> list[dict]:
    nodes = []
    for idx, host in enumerate(hosts):
        name = host.name or build_worker_node_name(existing_workers or [], index_offset=idx + name_offset)
        nodes.append(
            DeployedNodeInfo(
                ip=host.ip,
                bk_cloud_id=host.bk_cloud_id,
                name=name,
                port=MYSQL_DTS_WORKER_PORT,
                role="worker",
            ).to_dict()
        )
    return nodes


def master_config_file(node_name: str) -> str:
    return f"{node_name}.toml"


def worker_config_file(node_name: str) -> str:
    return f"{node_name}.toml"


__all__ = [
    "build_master_addr",
    "build_master_nodes",
    "build_worker_nodes",
    "hosts_to_exec_targets",
    "master_config_file",
    "render_master_config",
    "render_worker_config",
    "resolve_deploy_path",
    "worker_config_file",
]
