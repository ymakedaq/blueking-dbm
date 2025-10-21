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


class MarkRollbackStatusService(BaseService):
    """
    标记回档执行成功状态
    用于在子流程成功执行时，将状态写入trans_data
    """

    def _execute(self, data, parent_data) -> bool:
        trans_data = data.get_one_of_inputs("trans_data")

        # 标记回档执行成功
        trans_data.rollback_status = "success"
        data.outputs.trans_data = trans_data

        self.log_info(_("回档执行成功，标记状态为success"))
        return True


class MarkRollbackStatusComponent(Component):
    name = __name__
    code = "mark_rollback_status"
    bound_service = MarkRollbackStatusService
