"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

import copy
import logging.config
from dataclasses import asdict
from datetime import datetime
from typing import Dict, Optional

from django.utils import timezone
from django.utils.translation import ugettext as _

from backend.configuration.constants import DBType
from backend.constants import IP_PORT_DIVIDER
from backend.db_meta.enums import ClusterType
from backend.db_meta.models import Cluster
from backend.db_services.mysql.fixpoint_rollback.handlers import FixPointRollbackHandler
from backend.flow.engine.bamboo.scene.common.builder import Builder, SubBuilder
from backend.flow.engine.bamboo.scene.common.get_file_list import GetFileList
from backend.flow.engine.bamboo.scene.mysql.common.common_sub_flow import (
    build_surrounding_apps_sub_flow,
    install_mysql_in_cluster_sub_flow,
)
from backend.flow.engine.bamboo.scene.mysql.common.get_master_config import get_instance_config
from backend.flow.engine.bamboo.scene.spider.common.common_sub_flow import remote_migrate_switch_sub_flow
from backend.flow.engine.bamboo.scene.spider.common.exceptions import TendbGetBackupInfoFailedException
from backend.flow.engine.bamboo.scene.spider.spider_remote_node_migrate import remote_node_uninstall_sub_flow
from backend.flow.plugins.components.collections.common.download_backup_client import DownloadBackupClientComponent
from backend.flow.plugins.components.collections.common.pause import PauseComponent
from backend.flow.plugins.components.collections.mysql.clear_machine import MySQLClearMachineComponent
from backend.flow.plugins.components.collections.mysql.exec_actuator_script import ExecuteDBActuatorScriptComponent
from backend.flow.plugins.components.collections.mysql.trans_flies import TransFileComponent
from backend.flow.plugins.components.collections.spider.spider_db_meta import SpiderDBMetaComponent
from backend.flow.utils.common_act_dataclass import DownloadBackupClientKwargs
from backend.flow.utils.mysql.common.mysql_cluster_info import get_version_and_charset
from backend.flow.utils.mysql.mysql_act_dataclass import (
    ClearMachineKwargs,
    DBMetaOPKwargs,
    DownloadMediaKwargs,
    ExecActuatorKwargs,
)
from backend.flow.utils.mysql.mysql_act_playload import MysqlActPayload
from backend.flow.utils.mysql.mysql_context_dataclass import ClusterInfoContext
from backend.flow.utils.spider.spider_db_meta import SpiderDBMeta
from backend.flow.utils.spider.tendb_cluster_info import get_master_slave_recover_info
from backend.ticket.builders.common.constants import MySQLBackupSource

logger = logging.getLogger("flow")


class TendbClusterMigrateRemoteFlow(object):
    """
    tendb cluster 后端remote节点主从成对迁移
    """

    def __init__(self, root_id: str, ticket_data: Optional[Dict]):
        """
        @param root_id : 任务流程定义的root_id
        @param ticket_data : 单据传递参数
        """
        self.root_id = root_id
        self.ticket_data = ticket_data
        self.data = {}

        # 定义备份文件存放到目标机器目录位置
        self.backup_target_path = f"/data/dbbak/{self.root_id}"

    def migrate_master_slave_flow(self):
        """
        成对迁移集群主从节点。
        元数据信息修改顺序：
        1 mysql_migrate_cluster_add_instance
        2 mysql_migrate_cluster_add_tuple
        3 mysql_migrate_cluster_switch_storage
        """
        # 构建流程
        cluster_ids = [info["cluster_id"] for info in self.ticket_data["infos"]]

        tendb_migrate_pipeline_all = Builder(
            root_id=self.root_id,
            data=copy.deepcopy(self.ticket_data),
            need_random_pass_cluster_ids=list(set(cluster_ids)),
        )

        # 按照传入的infos信息，循环拼接子流程
        tendb_migrate_pipeline_all_list = []
        for info in self.ticket_data["infos"]:
            pipeline = self._build_single_cluster_pipeline(info)
            tendb_migrate_pipeline_all_list.append(pipeline)

        # 运行流程
        tendb_migrate_pipeline_all.add_parallel_sub_pipeline(tendb_migrate_pipeline_all_list)
        tendb_migrate_pipeline_all.run_pipeline(
            init_trans_data_class=ClusterInfoContext(),
            is_drop_random_user=True,
        )

    def _build_single_cluster_pipeline(self, info):
        """构建单个集群的迁移流程"""
        self.data = self._prepare_cluster_data(info)
        cluster_class = Cluster.objects.get(id=self.data["cluster_id"])

        tendb_migrate_pipeline = SubBuilder(root_id=self.root_id, data=copy.deepcopy(self.data))
        cluster_info = self._get_cluster_info(cluster_class)

        # 获取备份信息
        backup_info = (
            self._get_backup_info(cluster_class, cluster_info)
            if self.ticket_data["backup_source"] == MySQLBackupSource.REMOTE.value
            else {}
        )

        # 更新集群信息
        cluster_info = self._update_cluster_info(cluster_info)

        # 构建各阶段子流程
        install_sub_pipeline_list = self._build_install_pipeline(cluster_class, cluster_info)
        sync_data_sub_pipeline_list = self._build_sync_data_pipeline(cluster_class, cluster_info, backup_info)
        switch_sub_pipeline_list = self._build_switch_pipeline(cluster_class, cluster_info)
        surrounding_sub_pipeline_list, re_surrounding_sub_pipeline_list = self._build_surrounding_pipeline(
            cluster_class
        )
        uninstall_svr_sub_pipeline_list = self._build_uninstall_pipeline(cluster_class)

        # 组装完整流程
        self._assemble_pipeline(
            tendb_migrate_pipeline,
            install_sub_pipeline_list,
            sync_data_sub_pipeline_list,
            surrounding_sub_pipeline_list,
            switch_sub_pipeline_list,
            re_surrounding_sub_pipeline_list,
            uninstall_svr_sub_pipeline_list,
        )

        return tendb_migrate_pipeline.build_sub_process(_("集群迁移{}").format(cluster_class.id))

    def _assemble_pipeline(
        self,
        pipeline,
        install_sub_pipeline_list,
        sync_data_sub_pipeline_list,
        surrounding_sub_pipeline_list,
        switch_sub_pipeline_list,
        re_surrounding_sub_pipeline_list,
        uninstall_svr_sub_pipeline_list,
    ):
        """组装完整流程"""
        pipeline.add_parallel_sub_pipeline(sub_flow_list=install_sub_pipeline_list)
        pipeline.add_parallel_sub_pipeline(sub_flow_list=sync_data_sub_pipeline_list)
        pipeline.add_parallel_sub_pipeline(sub_flow_list=surrounding_sub_pipeline_list)
        pipeline.add_act(act_name=_("人工确认切换"), act_component_code=PauseComponent.code, kwargs={})
        pipeline.add_parallel_sub_pipeline(sub_flow_list=switch_sub_pipeline_list)
        pipeline.add_parallel_sub_pipeline(sub_flow_list=re_surrounding_sub_pipeline_list)
        pipeline.add_act(act_name=_("人工确认卸载实例"), act_component_code=PauseComponent.code, kwargs={})
        pipeline.add_parallel_sub_pipeline(sub_flow_list=uninstall_svr_sub_pipeline_list)

    def _prepare_cluster_data(self, info, root_id, ticket_data):
        """准备集群数据"""
        data = copy.deepcopy(info)
        cluster_class = Cluster.objects.get(id=data["cluster_id"])

        data.update(
            {
                "bk_cloud_id": cluster_class.bk_cloud_id,
                "root_id": root_id,
                "uid": ticket_data["uid"],
                "created_by": ticket_data["created_by"],
                "ticket_type": ticket_data["ticket_type"],
                "bk_biz_id": cluster_class.bk_biz_id,
                "db_module_id": cluster_class.db_module_id,
                "cluster_type": cluster_class.cluster_type,
                "force": True,
            }
        )

        data["charset"], data["db_version"] = get_version_and_charset(
            bk_biz_id=cluster_class.bk_biz_id,
            db_module_id=cluster_class.db_module_id,
            cluster_type=cluster_class.cluster_type,
        )

        return data

    def _get_cluster_info(self, cluster_class, old_master_ip, old_slave_ip, charset, db_version):
        """获取集群信息"""
        cluster_info = get_master_slave_recover_info(cluster_class.id, old_master_ip, old_slave_ip)
        cluster_info["charset"] = charset
        cluster_info["db_version"] = db_version
        cluster_info["ports"] = []
        return cluster_info

    def _get_backup_info(self, cluster_class, cluster_info):
        """获取备份信息"""
        backup_handler = FixPointRollbackHandler(cluster_class.id)
        restore_time = datetime.now(timezone.utc)
        shard_list = [int(shard_id) for shard_id in cluster_info["my_shards"].keys()]
        backup_info = backup_handler.query_latest_backup_log(restore_time, shard_list=shard_list)

        if backup_info is None:
            logger.error("cluster {} backup info not exists".format(cluster_class.id))
            raise TendbGetBackupInfoFailedException(message=_("获取集群 {} 的备份信息失败".format(cluster_class.id)))

        return backup_info

    def _update_cluster_info(self, cluster_info, new_master_ip, new_slave_ip, bk_cloud_id):
        """更新集群信息"""
        for shard_id, shard in cluster_info["my_shards"].items():
            master = {
                "ip": new_master_ip,
                "port": shard["master"]["port"],
                "bk_cloud_id": bk_cloud_id,
                "instance": "{}{}{}".format(new_master_ip, IP_PORT_DIVIDER, shard["master"]["port"]),
            }

            slave = {
                "ip": new_slave_ip,
                "port": shard["slave"]["port"],
                "bk_cloud_id": bk_cloud_id,
                "instance": "{}{}{}".format(new_slave_ip, IP_PORT_DIVIDER, shard["slave"]["port"]),
            }

            cluster_info["my_shards"][shard_id]["new_slave"] = slave
            cluster_info["my_shards"][shard_id]["new_master"] = master
            cluster_info["ports"].append(shard["master"]["port"])

        return cluster_info

    def _build_install_pipeline(
        self,
        cluster_class,
        cluster_info,
        root_id,
        uid,
        new_master_ip,
        new_slave_ip,
        bk_new_master,
        bk_new_slave,
        old_master_ip,
    ):
        """构建安装流程"""
        db_config = get_instance_config(cluster_class.bk_cloud_id, old_master_ip, cluster_info["ports"])

        install_sub_pipeline = SubBuilder(root_id=root_id, data=copy.deepcopy(self.data))

        # 添加安装子流程
        install_sub_pipeline.add_sub_pipeline(
            sub_flow=install_mysql_in_cluster_sub_flow(
                uid=uid,
                root_id=root_id,
                cluster=cluster_class,
                new_mysql_list=[new_master_ip, new_slave_ip],
                install_ports=cluster_info["ports"],
                bk_host_ids=[bk_new_master["bk_host_id"], bk_new_slave["bk_host_id"]],
                db_config=db_config,
            )
        )

        # 添加元数据写入和工具安装动作
        cluster = {
            "new_master_ip": new_master_ip,
            "new_slave_ip": new_slave_ip,
            "cluster_id": cluster_class.id,
            "bk_cloud_id": cluster_class.bk_cloud_id,
            "bk_biz_id": cluster_class.bk_biz_id,
            "ports": cluster_info["ports"],
            "version": cluster_class.major_version,
        }

        install_sub_pipeline.add_act(
            act_name=_("写入初始化实例的db_meta元信息"),
            act_component_code=SpiderDBMetaComponent.code,
            kwargs=asdict(
                DBMetaOPKwargs(
                    db_meta_class_func=SpiderDBMeta.remotedb_migrate_add_install_nodes.__name__,
                    cluster=copy.deepcopy(cluster),
                    is_update_trans_data=False,
                )
            ),
        )

        install_sub_pipeline.add_act(
            act_name=_("安装backup-client工具"),
            act_component_code=DownloadBackupClientComponent.code,
            kwargs=asdict(
                DownloadBackupClientKwargs(
                    bk_cloud_id=cluster_class.bk_cloud_id,
                    bk_biz_id=int(cluster_class.bk_biz_id),
                    download_host_list=[cluster["new_master_ip"], cluster["new_slave_ip"]],
                )
            ),
        )

        # 安装临时备份程序
        exec_act_kwargs = ExecActuatorKwargs(
            cluster=cluster,
            bk_cloud_id=cluster_class.bk_cloud_id,
            cluster_type=cluster_class.cluster_type,
            get_mysql_payload_func=MysqlActPayload.get_install_tmp_db_backup_payload.__name__,
        )
        exec_act_kwargs.exec_ip = [cluster["new_master_ip"], cluster["new_slave_ip"]]
        install_sub_pipeline.add_act(
            act_name=_("安装临时备份程序"),
            act_component_code=ExecuteDBActuatorScriptComponent.code,
            kwargs=asdict(exec_act_kwargs),
        )

        return [install_sub_pipeline.build_sub_process(sub_name=_("安装remote主从节点"))]

    def _build_sync_data_pipeline(self, cluster_class, cluster_info, backup_info, root_id, ticket_data):
        """构建数据同步流程"""
        sync_data_sub_pipeline_list = []
        for shard_id, node in cluster_info["my_shards"].items():
            ins_cluster = self._prepare_sync_cluster_info(cluster_info, node, shard_id)

            sync_data_sub_pipeline = SubBuilder(root_id=root_id, data=copy.deepcopy(self.data))

            if ticket_data["backup_source"] == MySQLBackupSource.REMOTE.value:
                self._add_remote_sync_pipeline(sync_data_sub_pipeline, ins_cluster, backup_info, shard_id)
            else:
                self._add_local_sync_pipeline(sync_data_sub_pipeline, ins_cluster, node, cluster_class)

            sync_data_sub_pipeline.add_act(
                act_name=_("同步完毕,写入数据节点的主从关系"),
                act_component_code=SpiderDBMetaComponent.code,
                kwargs=asdict(
                    DBMetaOPKwargs(
                        db_meta_class_func=SpiderDBMeta.remotedb_migrate_add_storage_tuple.__name__,
                        cluster=ins_cluster,
                        is_update_trans_data=True,
                    )
                ),
            )

            sync_data_sub_pipeline_list.append(
                sync_data_sub_pipeline.build_sub_process(sub_name=_("恢复分片{}数据".format(shard_id)))
            )

        return sync_data_sub_pipeline_list

    def _build_switch_pipeline(self, cluster_class, cluster_info, root_id, uid, created_by):
        """构建切换流程"""
        shard_list = []
        for shard_id, node in cluster_info["my_shards"].items():
            shard_cluster = {
                "old_master": node["master"]["instance"],
                "old_slave": node["slave"]["instance"],
                "new_master": node["new_master"]["instance"],
                "new_slave": node["new_slave"]["instance"],
            }
            shard_list.append(shard_cluster)

        switch_sub_pipeline = SubBuilder(root_id=root_id, data=copy.deepcopy(self.data))
        switch_sub_pipeline.add_sub_pipeline(
            sub_flow=remote_migrate_switch_sub_flow(
                uid=uid,
                root_id=root_id,
                cluster=cluster_class,
                migrate_tuples=shard_list,
                created_by=created_by,
            )
        )

        cluster_info_meta = copy.deepcopy(cluster_info)
        cluster_info_meta["shards"] = cluster_info["my_shards"]
        switch_sub_pipeline.add_act(
            act_name=_("remote机器切换完毕后修改元数据指向"),
            act_component_code=SpiderDBMetaComponent.code,
            kwargs=asdict(
                DBMetaOPKwargs(
                    db_meta_class_func=SpiderDBMeta.tendb_remotedb_rebalance_switch.__name__,
                    cluster=cluster_info_meta,
                    is_update_trans_data=True,
                )
            ),
        )

        return [switch_sub_pipeline.build_sub_process(sub_name=_("切换remote node 节点"))]

    def _build_surrounding_pipeline(self, cluster_class, root_id, new_master_ip, new_slave_ip):
        """构建周边组件安装流程"""
        surrounding_sub_pipeline = SubBuilder(root_id=root_id, data=copy.deepcopy(self.data))
        surrounding_sub_pipeline.add_sub_pipeline(
            sub_flow=build_surrounding_apps_sub_flow(
                bk_cloud_id=cluster_class.bk_cloud_id,
                master_ip_list=[new_master_ip],
                slave_ip_list=[new_slave_ip],
                root_id=root_id,
                parent_global_data=copy.deepcopy(self.data),
                collect_sysinfo=True,
                cluster_type=ClusterType.TenDBCluster.value,
                is_install_backup=False,
            )
        )

        re_surrounding_sub_pipeline = SubBuilder(root_id=root_id, data=copy.deepcopy(self.data))
        re_surrounding_sub_pipeline.add_sub_pipeline(
            sub_flow=build_surrounding_apps_sub_flow(
                bk_cloud_id=cluster_class.bk_cloud_id,
                master_ip_list=[new_master_ip],
                slave_ip_list=[new_slave_ip],
                root_id=root_id,
                parent_global_data=copy.deepcopy(self.data),
                is_init=True,
                cluster_type=ClusterType.TenDBCluster.value,
            )
        )

        return (
            [surrounding_sub_pipeline.build_sub_process(sub_name=_("新机器安装周边组件"))],
            [re_surrounding_sub_pipeline.build_sub_process(sub_name=_("切换后重新安装周边组件"))],
        )

    def _build_uninstall_pipeline(self, cluster_class, old_master_ip, old_slave_ip, bk_cloud_id, ticket_data):
        """构建卸载流程"""
        uninstall_svr_sub_pipeline_list = []
        for ip in [old_master_ip, old_slave_ip]:
            uninstall_svr_sub_pipeline = SubBuilder(root_id=self.root_id, data=copy.deepcopy(ticket_data))

            # 添加卸载相关动作
            uninstall_svr_sub_pipeline.add_act(
                act_name=_("下发db-actor到节点{}".format(ip)),
                act_component_code=TransFileComponent.code,
                kwargs=asdict(
                    DownloadMediaKwargs(
                        bk_cloud_id=bk_cloud_id,
                        exec_ip=[ip],
                        file_list=GetFileList(db_type=DBType.MySQL).get_db_actuator_package(),
                    )
                ),
            )

            ins_cluster = {"uninstall_ip": ip, "cluster_id": cluster_class.id}
            uninstall_svr_sub_pipeline.add_act(
                act_name=_("整机卸载成功前删除元数据"),
                act_component_code=SpiderDBMetaComponent.code,
                kwargs=asdict(
                    DBMetaOPKwargs(
                        db_meta_class_func=SpiderDBMeta.remotedb_migrate_remove_storage.__name__,
                        cluster=ins_cluster,
                        is_update_trans_data=True,
                    )
                ),
            )

            uninstall_svr_sub_pipeline.add_act(
                act_name=_("清理机器配置"),
                act_component_code=MySQLClearMachineComponent.code,
                kwargs=asdict(
                    ClearMachineKwargs(
                        exec_ip=ip,
                        bk_cloud_id=cluster_class.bk_cloud_id,
                    )
                ),
            )

            uninstall_svr_sub_pipeline.add_sub_pipeline(
                sub_flow=remote_node_uninstall_sub_flow(
                    root_id=self.root_id, ticket_data=copy.deepcopy(ticket_data), ip=ip
                )
            )

            uninstall_svr_sub_pipeline_list.append(
                uninstall_svr_sub_pipeline.build_sub_process(sub_name=_("卸载remote节点{}").format(ip))
            )

        return uninstall_svr_sub_pipeline_list
