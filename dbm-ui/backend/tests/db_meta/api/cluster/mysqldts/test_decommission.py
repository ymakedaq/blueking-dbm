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
from unittest.mock import MagicMock, patch

import pytest

from backend.db_meta.api.cluster.mysqldts.decommission import decommission
from backend.db_meta.enums import (
    AccessLayer,
    ClusterEntryType,
    ClusterPhase,
    ClusterStatus,
    ClusterType,
    InstanceInnerRole,
    InstancePhase,
    InstanceRole,
    InstanceStatus,
    MachineType,
)
from backend.db_meta.models import (
    BKCity,
    Cluster,
    ClusterEntry,
    Machine,
    MysqlDtsCluster,
    ProxyInstance,
    StorageInstance,
)
from backend.db_meta.models.mysql_dts import MysqlDtsClusterStatus
from backend.flow.utils.mysql.dts.constants import MYSQL_DTS_MASTER_PORT, MYSQL_DTS_WORKER_PORT
from backend.tests.mock_data import constant
from backend.tests.mock_data.components import cc

pytestmark = pytest.mark.django_db

TEST_BK_CLOUD_ID = 0
TEST_CLUSTER_NAME = "dts-make-test-01"
TEST_IMMUTE_DOMAIN = f"{TEST_CLUSTER_NAME}.dts.db"
TEST_MASTER_ADDR = f"{cc.NORMAL_IP}:{MYSQL_DTS_MASTER_PORT}"


@pytest.fixture
def dts_cluster_meta():
    """构造一套最小可用的 DTS 集群元数据（Master + Worker 分机）。"""
    bk_city = BKCity.objects.first()
    master_machine = Machine.objects.create(
        ip=cc.NORMAL_IP,
        bk_biz_id=constant.BK_BIZ_ID,
        machine_type=MachineType.MYSQL_DTS_MASTER.value,
        bk_city=bk_city,
        access_layer=AccessLayer.PROXY.value,
        bk_host_id=100001,
        bk_cloud_id=TEST_BK_CLOUD_ID,
        cluster_type=ClusterType.MySQLDTS.value,
    )
    worker_machine = Machine.objects.create(
        ip=cc.NORMAL_IP2,
        bk_biz_id=constant.BK_BIZ_ID,
        machine_type=MachineType.MYSQL_DTS_WORKER.value,
        bk_city=bk_city,
        access_layer=AccessLayer.STORAGE.value,
        bk_host_id=100002,
        bk_cloud_id=TEST_BK_CLOUD_ID,
        cluster_type=ClusterType.MySQLDTS.value,
    )
    proxy = ProxyInstance.objects.create(
        machine=master_machine,
        port=MYSQL_DTS_MASTER_PORT,
        admin_port=MYSQL_DTS_MASTER_PORT + 1000,
        bk_biz_id=constant.BK_BIZ_ID,
        access_layer=AccessLayer.PROXY.value,
        machine_type=MachineType.MYSQL_DTS_MASTER.value,
        cluster_type=ClusterType.MySQLDTS.value,
        status=InstanceStatus.RUNNING.value,
    )
    storage = StorageInstance.objects.create(
        machine=worker_machine,
        port=MYSQL_DTS_WORKER_PORT,
        bk_biz_id=constant.BK_BIZ_ID,
        access_layer=AccessLayer.STORAGE.value,
        machine_type=MachineType.MYSQL_DTS_WORKER.value,
        instance_role=InstanceRole.MYSQL_DTS_WORKER_MASTER.value,
        instance_inner_role=InstanceInnerRole.MASTER.value,
        cluster_type=ClusterType.MySQLDTS.value,
        status=InstanceStatus.RUNNING.value,
        phase=InstancePhase.ONLINE.value,
        is_stand_by=True,
    )
    cluster = Cluster.objects.create(
        bk_biz_id=constant.BK_BIZ_ID,
        name=TEST_CLUSTER_NAME,
        alias=TEST_CLUSTER_NAME,
        cluster_type=ClusterType.MySQLDTS.value,
        db_module_id=0,
        immute_domain=TEST_IMMUTE_DOMAIN,
        phase=ClusterPhase.ONLINE.value,
        status=ClusterStatus.NORMAL.value,
        bk_cloud_id=TEST_BK_CLOUD_ID,
    )
    cluster.proxyinstance_set.add(proxy)
    cluster.storageinstance_set.add(storage)
    entry = ClusterEntry.objects.create(
        cluster=cluster,
        cluster_entry_type=ClusterEntryType.DNS.value,
        entry=TEST_MASTER_ADDR,
    )
    entry.proxyinstance_set.add(proxy)
    dts_cluster = MysqlDtsCluster.objects.create(
        name=TEST_CLUSTER_NAME,
        bk_biz_id=constant.BK_BIZ_ID,
        bk_cloud_id=TEST_BK_CLOUD_ID,
        cluster_id=cluster.id,
        status=MysqlDtsClusterStatus.RUNNING.value,
        master_nodes=[{"ip": cc.NORMAL_IP, "bk_cloud_id": TEST_BK_CLOUD_ID}],
        worker_nodes=[{"ip": cc.NORMAL_IP2, "bk_cloud_id": TEST_BK_CLOUD_ID}],
        master_addr=TEST_MASTER_ADDR,
        deploy_path=f"/data/dts/{TEST_CLUSTER_NAME}",
    )
    return {
        "cluster": cluster,
        "dts_cluster": dts_cluster,
        "proxy": proxy,
        "storage": storage,
        "master_machine": master_machine,
        "worker_machine": worker_machine,
        "entry": entry,
    }


class TestMysqlDtsDecommission:
    @patch("backend.db_meta.api.cluster.mysqldts.decommission.CcManage")
    def test_decommission_hard_deletes_cluster_and_frees_domain(self, mock_cc_manage, dts_cluster_meta):
        mock_cc_manage.return_value = MagicMock()
        cluster_id = dts_cluster_meta["cluster"].id
        dts_id = dts_cluster_meta["dts_cluster"].id
        proxy_id = dts_cluster_meta["proxy"].id
        storage_id = dts_cluster_meta["storage"].id
        master_machine_id = dts_cluster_meta["master_machine"].id
        worker_machine_id = dts_cluster_meta["worker_machine"].id

        decommission(dts_cluster_id=dts_id, recycle_hosts=True, updater="tester")

        assert not Cluster.objects.filter(id=cluster_id).exists()
        assert not Cluster.objects.filter(immute_domain=TEST_IMMUTE_DOMAIN).exists()
        assert not ClusterEntry.objects.filter(entry=TEST_MASTER_ADDR).exists()
        assert not ProxyInstance.objects.filter(id=proxy_id).exists()
        assert not StorageInstance.objects.filter(id=storage_id).exists()
        assert not Machine.objects.filter(id=master_machine_id).exists()
        assert not Machine.objects.filter(id=worker_machine_id).exists()

        dts_cluster = MysqlDtsCluster.objects.get(id=dts_id)
        assert dts_cluster.status == MysqlDtsClusterStatus.DESTROYED.value
        assert dts_cluster.cluster_id == 0

        # 同名域名可再次创建（验证唯一约束已释放）
        Cluster.objects.create(
            bk_biz_id=constant.BK_BIZ_ID,
            name=TEST_CLUSTER_NAME,
            alias=TEST_CLUSTER_NAME,
            cluster_type=ClusterType.MySQLDTS.value,
            db_module_id=0,
            immute_domain=TEST_IMMUTE_DOMAIN,
            phase=ClusterPhase.ONLINE.value,
            status=ClusterStatus.NORMAL.value,
            bk_cloud_id=TEST_BK_CLOUD_ID,
        )
        assert Cluster.objects.filter(immute_domain=TEST_IMMUTE_DOMAIN).count() == 1
