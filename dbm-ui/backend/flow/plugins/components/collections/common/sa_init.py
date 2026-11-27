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
from pipeline.component_framework.component import Component

from backend import env
from backend.components.sops.client import BkSopsApi
from backend.constants import DEFAULT_BK_CLOUD_ID
from backend.flow.plugins.components.collections.common.base_service import BkSopsService
from backend.flow.plugins.components.collections.common.sops_ip import format_sops_ip


def is_direct_zone_host(host: dict) -> bool:
    """判断主机是否在直连管控区域(bk_cloud_id == 0)，缺失或非法值按直连处理，保持历史行为"""
    try:
        cloud_id = int(host.get("bk_cloud_id"))
    except (TypeError, ValueError):
        cloud_id = DEFAULT_BK_CLOUD_ID
    return cloud_id == DEFAULT_BK_CLOUD_ID


class SaInit(BkSopsService):
    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        trans_data = data.get_one_of_inputs("trans_data")
        bk_biz_id = kwargs["bk_biz_id"]

        # 从上下文中获取ip列表，标准运维要求管控区域非 0 时使用「管控区域:IP」格式
        iplist = [format_sops_ip(ip, DEFAULT_BK_CLOUD_ID) for ip in kwargs["ips"]]
        if isinstance(trans_data, dict) and trans_data.get("hosts"):
            # 非直连管控区域(bk_cloud_id != 0)的主机不下发sa初始化，编排期传入的 kwargs["ips"] 已按直连过滤
            direct_hosts = [host for host in trans_data["hosts"] if is_direct_zone_host(host)]
            iplist = [format_sops_ip(host["ip"], DEFAULT_BK_CLOUD_ID) for host in direct_hosts] + iplist

        # 过滤后没有直连区域主机时，不创建标准运维任务，直接结束调度避免空跑
        if not iplist:
            self.log_info(_("无直连管控区域主机，跳过SA初始化"))
            data.outputs.task_id = None
            self.finish_schedule()
            return True

        param = {
            "template_id": env.SA_INIT_TEMPLATE_ID,
            "bk_biz_id": bk_biz_id,
            "template_source": "common",
            "name": _("SA初始化"),
            "flow_type": "common",
            "constants": {
                "${biz_cc_id}": bk_biz_id,
                "${job_ip_list}": "\n".join(iplist),
                "${bk_biz_id}": bk_biz_id,
                "${job_account}": kwargs.get("account_name", "root"),
            },
        }
        rpdata = BkSopsApi.create_task(param)
        task_id = rpdata["task_id"]
        task_url = rpdata.get("task_url", "")
        # start task
        self.log_info(f"job url: {task_url}")
        param = {"bk_biz_id": bk_biz_id, "task_id": task_id}
        BkSopsApi.start_task(param)
        data.outputs.task_id = task_id
        return True

    def _schedule(self, data, parent_data, callback_data=None) -> bool:
        # 兜底：跳过场景下 task_id 为空，直接结束调度，避免拿空任务ID去查询标准运维
        task_id = data.get_one_of_outputs("task_id")
        if not task_id:
            self.log_info(_("无SA初始化任务，结束调度"))
            self.finish_schedule()
            return True
        return super()._schedule(data, parent_data, callback_data)


class SaInitComponent(Component):
    name = _("sa初始化")
    code = "sa_machine_init"
    bound_service = SaInit
