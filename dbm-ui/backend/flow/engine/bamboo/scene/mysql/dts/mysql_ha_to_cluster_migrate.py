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
from typing import Dict, Optional

from django.utils.translation import gettext as _

from backend.flow.engine.bamboo.scene.common.builder import Builder
from backend.flow.engine.bamboo.scene.mysql.dts.mysql_dts_migrate_subflow import mysql_dts_migrate_subflow
from backend.flow.utils.mysql.dts.constants import MigrateType
from backend.flow.utils.mysql.dts.context import MysqlDtsMigrateSubflowInput, MysqlDtsTransData
from backend.flow.utils.mysql.dts.migrate_plan import build_migrate_plan

logger = logging.getLogger("flow")


class MysqlHaToClusterMigrateFlow:
    """TenDBHA → TenDBCluster 数据迁移 Flow。"""

    def __init__(self, root_id: str, data: Optional[Dict]):
        self.root_id = root_id
        self.data = data

    def run_flow(self):
        migrate_plan = self.data.get("migrate_plan") or build_migrate_plan(self.data)
        migrate_plan.migrate_type = MigrateType.HA_TO_CLUSTER.value
        pipeline = Builder(root_id=self.root_id, data=self.data)
        migrate_inp = MysqlDtsMigrateSubflowInput(
            root_id=self.root_id,
            bk_biz_id=int(self.data["bk_biz_id"]),
            ticket_id=int(self.data.get("ticket_id", 0)),
            migrate_plan=migrate_plan,
            creator=self.data.get("created_by", ""),
        )
        pipeline.add_sub_pipeline(
            mysql_dts_migrate_subflow(migrate_inp).build_sub_process(sub_name=_("HA 到 Cluster 数据迁移"))
        )
        pipeline.run_pipeline(init_trans_data_class=MysqlDtsTransData())
