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
from django.utils.translation import gettext_lazy as _

from blue_krill.data_types.enum import EnumField, StrStructuredEnum

MYSQL_DTS_DEPLOY_BASE_PATH = "/data/dts"
MYSQL_DTS_MASTER_PORT = 18301
MYSQL_DTS_WORKER_PORT = 18501
MYSQL_DTS_MASTER_PEER_PORT = 18302
MYSQL_DTS_VERSION_SERIES = "latest"
MYSQL_DTS_VERIFY_RETRY_INTERVAL = 5
# 部署验收最长等待约 60s（12 * 5s）
MYSQL_DTS_VERIFY_MAX_RETRIES = 12
MYSQL_DTS_MIGRATE_USER_PREFIX = "dts_migrate_"


def get_default_deploy_path(cluster_name: str) -> str:
    return f"{MYSQL_DTS_DEPLOY_BASE_PATH}/{cluster_name}"


class DtsRegisterMode(StrStructuredEnum):
    CREATE = EnumField("create", _("create"))
    APPEND_WORKER = EnumField("append_worker", _("append_worker"))
    APPEND_MASTER = EnumField("append_master", _("append_master"))


class DtsLifecycleMode(StrStructuredEnum):
    USE_EXISTING = EnumField("use_existing", _("use_existing"))
    DEPLOY_EPHEMERAL = EnumField("deploy_ephemeral", _("deploy_ephemeral"))
    DEPLOY_PERSISTENT = EnumField("deploy_persistent", _("deploy_persistent"))


class MigrateTopology(StrStructuredEnum):
    ONE_TO_ONE = EnumField("one_to_one", _("one_to_one"))
    MANY_TO_ONE = EnumField("many_to_one", _("many_to_one"))
    ONE_TO_MANY = EnumField("one_to_many", _("one_to_many"))


class MigrateType(StrStructuredEnum):
    HA_TO_HA = EnumField("ha_to_ha", _("ha_to_ha"))
    HA_TO_CLUSTER = EnumField("ha_to_cluster", _("ha_to_cluster"))
