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
from pipeline.component_framework.component import Component

from backend.flow.plugins.components.collections.common.base_service import BaseService

logger = logging.getLogger("flow")


class CheckRollbackStatusService(BaseService):
    """
    检查回档执行状态
    如果trans_data中已有rollback_status（说明子流程成功），则保持原状态
    如果没有rollback_status（说明子流程失败），则设置为failed
    此组件始终返回成功，确保流程能继续执行
    """

    def _execute(self, data, parent_data) -> bool:
        trans_data = data.get_one_of_inputs("trans_data")

        # 检查是否已有rollback_status
        if hasattr(trans_data, "rollback_status") and trans_data.rollback_status == "success":
            # 子流程成功执行，状态已被标记
            self.log_info(_("检测到回档执行成功标记，状态为success"))
            status = "success"
        else:
            # 子流程失败或未标记，设置为failed
            self.log_warning(_("未检测到回档成功标记，判定为失败，状态设置为failed"))
            status = "failed"

        # 将最终状态写入trans_data
        trans_data.rollback_status = status
        data.outputs.trans_data = trans_data

        # 同时将状态直接写入outputs，供条件网关使用
        data.outputs.rollback_status = status

        # 始终返回True，确保流程继续
        return True


class CheckRollbackStatusComponent(Component):
    name = __name__
    code = "check_rollback_status"
    bound_service = CheckRollbackStatusService
