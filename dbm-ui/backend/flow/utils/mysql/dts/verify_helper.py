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
from urllib.parse import urlparse

from django.utils.translation import gettext as _


def extract_ip_from_addr(addr: str) -> str:
    """从 DTS OpenAPI addr 中提取 IP。

    OpenAPI 实际返回形如 ``http://127.0.0.1:18401``（带 scheme 的 peer-url），
    也可能是 ``127.0.0.1:18301`` / ``[::1]:18301``。不能简单 rsplit(':')，
    否则会把 ``http://127.0.0.1`` 当成 IP，导致与期望 IP 永远匹配不上。
    """
    if not addr:
        return ""
    raw = addr.strip()
    if "://" not in raw:
        # 无 scheme：host:port / [ipv6]:port / bare host
        raw = f"//{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    return host.strip("[]")


def format_api_nodes(api_items: list) -> str:
    parts = []
    for item in api_items or []:
        name = getattr(item, "name", "") or ""
        addr = getattr(item, "addr", "") or ""
        alive = getattr(item, "alive", None)
        parts.append(f"{name}@{addr}(alive={alive})")
    return ", ".join(parts) if parts else _("(空)")


def match_nodes(api_items: list, expected_nodes: list[dict], role: str) -> None:
    """校验期望节点均已出现在 OpenAPI 列表中；不匹配时抛出带明细的 ValueError。"""
    if not expected_nodes:
        return

    api_ips = {extract_ip_from_addr(getattr(item, "addr", "") or "") for item in api_items}
    api_ips.discard("")
    expected_ips = [node.get("ip") for node in expected_nodes if node.get("ip")]
    missing = [ip for ip in expected_ips if ip not in api_ips]
    if not missing:
        return

    raise ValueError(
        _("{} 节点未全部注册: 缺失={}, 期望={}, 实际注册={}, 解析到的 IP={}").format(
            role,
            missing,
            expected_ips,
            format_api_nodes(api_items),
            sorted(api_ips),
        )
    )
