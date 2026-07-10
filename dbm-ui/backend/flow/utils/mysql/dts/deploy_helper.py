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
from dataclasses import dataclass

from backend.flow.utils.mysql.dts.constants import (
    MYSQL_DTS_MASTER_PEER_PORT,
    MYSQL_DTS_MASTER_PORT,
    MYSQL_DTS_WORKER_PORT,
)
from backend.flow.utils.mysql.dts.context import DtsHostSpec, HostDeployPlan


def group_deploy_hosts(master_hosts: list[DtsHostSpec], worker_hosts: list[DtsHostSpec]) -> HostDeployPlan:
    master_ip_set = {h.ip for h in master_hosts}
    worker_ip_set = {h.ip for h in worker_hosts}
    colocated_ips = master_ip_set & worker_ip_set
    colocated_hosts = [h for h in master_hosts if h.ip in colocated_ips]
    master_only_hosts = [h for h in master_hosts if h.ip not in colocated_ips]
    worker_only_hosts = [h for h in worker_hosts if h.ip not in colocated_ips]
    return HostDeployPlan(
        colocated_hosts=colocated_hosts,
        master_only_hosts=master_only_hosts,
        worker_only_hosts=worker_only_hosts,
    )


def dedupe_hosts_by_ip(hosts: list[DtsHostSpec]) -> list[DtsHostSpec]:
    seen = set()
    result = []
    for host in hosts:
        if host.ip in seen:
            continue
        seen.add(host.ip)
        result.append(host)
    return result


def build_master_node_name(index: int) -> str:
    return f"dm-master-{index}"


def build_worker_node_name(existing_workers: list[dict], index_offset: int = 0) -> str:
    max_idx = 0
    for worker in existing_workers:
        name = worker.get("name", "")
        if name.startswith("dm-worker-"):
            try:
                max_idx = max(max_idx, int(name.split("-")[-1]))
            except ValueError:
                pass
    return f"dm-worker-{max_idx + index_offset + 1}"


def render_master_config(
    *,
    deploy_path: str,
    node_name: str,
    advertise_ip: str,
    master_ha: bool = False,
    peer_addrs: list[str] | None = None,
) -> str:
    data_dir = f"{deploy_path}/{node_name}-data"
    log_file = f"{deploy_path}/{node_name}.log"
    peer_addrs = peer_addrs or []
    initial_cluster = (
        ", ".join(f'"{addr}"' for addr in peer_addrs)
        if peer_addrs
        else f'"{advertise_ip}:{MYSQL_DTS_MASTER_PEER_PORT}"'
    )
    return f"""name = "{node_name}"
data-dir = "{data_dir}"
master-addr = "{advertise_ip}:{MYSQL_DTS_MASTER_PORT}"
advertise-addr = "{advertise_ip}:{MYSQL_DTS_MASTER_PORT}"
peer-urls = "{advertise_ip}:{MYSQL_DTS_MASTER_PEER_PORT}"
advertise-peer-urls = "{advertise_ip}:{MYSQL_DTS_MASTER_PEER_PORT}"
initial-cluster = {initial_cluster}
initial-cluster-state = "new"

[log]
level = "info"
file = "{log_file}"

[security]
ssl-ca = ""
ssl-cert = ""
ssl-key = ""
"""


def render_worker_config(
    *,
    deploy_path: str,
    node_name: str,
    advertise_ip: str,
    master_addr: str,
    join_addrs: list[str] | None = None,
) -> str:
    relay_dir = f"{deploy_path}/{node_name}-data"
    log_file = f"{deploy_path}/{node_name}.log"
    join_addrs = join_addrs or [master_addr]
    join_str = ", ".join(f'"{addr}"' for addr in join_addrs)
    return f"""name = "{node_name}"
join = [{join_str}]
worker-addr = "{advertise_ip}:{MYSQL_DTS_WORKER_PORT}"
advertise-addr = "{advertise_ip}:{MYSQL_DTS_WORKER_PORT}"
relay-dir = "{relay_dir}"

[log]
level = "info"
file = "{log_file}"

[security]
ssl-ca = ""
ssl-cert = ""
ssl-key = ""
"""


@dataclass
class DeployedNodeInfo:
    ip: str
    bk_cloud_id: int
    name: str
    port: int
    role: str

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "bk_cloud_id": self.bk_cloud_id,
            "name": self.name,
            "port": self.port,
            "role": self.role,
        }


def build_master_addr(master_nodes: list[dict]) -> str:
    if not master_nodes:
        return ""
    first = master_nodes[0]
    return f"{first['ip']}:{first.get('port', MYSQL_DTS_MASTER_PORT)}"
