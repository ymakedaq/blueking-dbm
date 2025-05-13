import copy
from datetime import datetime
from typing import Dict, Optional

from django.db.models import Q
from django.utils.crypto import get_random_string

from backend.configuration.constants import DBType
from backend.db_meta.enums import ClusterType, InstanceInnerRole
from backend.db_meta.models import Cluster
from backend.db_package.models import Package
from backend.flow.consts import MediumEnum
from backend.flow.engine.bamboo.scene.common.builder import Builder
from backend.flow.engine.bamboo.scene.mysql.common.get_master_config import get_cluster_config
from backend.flow.engine.bamboo.scene.mysql.mysql_rollback_data_sub_flow import rollback_remote_and_backupid
from backend.flow.engine.bamboo.scene.mysql.mysql_single_apply_flow import MySQLSingleApplyFlow
from backend.flow.engine.bamboo.scene.mysql.mysql_single_destroy_flow import MySQLSingleDestroyFlow
from backend.flow.utils.mysql.common.mysql_cluster_info import get_version_and_charset
from backend.flow.utils.mysql.mysql_context_dataclass import SingleApplyManualContext


class MySQLRollbackExerciseFlow(object):
    """
    mysql 回档演习流程
    """

    def __init__(self, root_id: str, data: Optional[Dict]):
        """
        @param root_id : 任务流程定义的root_id
        @param data : 单据传递过来的参数列表，是dict格式
        {
            "ticket_type": "MYSQL_ROLLBACK_EXERCISE",
            "exercise_cluster_id": 1,
            "backup_id": "xxx",
            "rollback_ip": "127.0.0.1",    // 回档的演练的ip
            "bk_biz_id": 123,  // 回档到哪个业务下
            "backupinfo": {}
        }
        """
        self.root_id = root_id
        self.ticket_data = data
        self.data = {}
        self.rollback_port = 20000
        self.rollback_ip = self.ticket_data["rollback_ip"]
        self.rollback_to_bk_biz_id = self.ticket_data["bk_biz_id"]

    def run(self):
        pipeline = Builder(
            root_id=self.root_id,
            data=copy.deepcopy(self.ticket_data),
        )
        cluster_class = Cluster.objects.get(id=self.ticket_data["exercise_cluster_id"])
        filters = Q(
            cluster__cluster_type=ClusterType.TenDBSingle.value, instance_inner_role=InstanceInnerRole.ORPHAN.value
        )
        filters = filters | Q(
            cluster__cluster_type=ClusterType.TenDBHA.value, instance_inner_role=InstanceInnerRole.MASTER.value
        )
        master = cluster_class.storageinstance_set.get(filters)
        self.data = copy.deepcopy(self.ticket_data)
        self.data["bk_cloud_id"] = cluster_class.bk_cloud_id
        self.data["db_module_id"] = cluster_class.db_module_id
        self.data["time_zone"] = cluster_class.time_zone
        self.data["created_by"] = self.ticket_data["created_by"]
        self.data["module"] = cluster_class.db_module_id
        self.data["ticket_type"] = self.ticket_data["ticket_type"]
        self.data["uid"] = self.ticket_data["uid"]
        self.data["city"] = cluster_class.region
        self.data["package"] = Package.get_latest_package(
            version=cluster_class.major_version, pkg_type=MediumEnum.MySQL, db_type=DBType.MySQL
        ).name
        self.data["charset"], self.data["db_version"] = get_version_and_charset(
            cluster_class.bk_biz_id,
            db_module_id=self.data["db_module_id"],
            cluster_type=cluster_class.cluster_type,
        )
        install_ticket = copy.deepcopy(self.data)
        datetime_str = datetime.strftime(datetime.now(), "%Y%m%d%H%M%S%f")
        cluster_name = "{}-{}".format(cluster_class.name, datetime_str)
        if len(cluster_name) > 48:
            cluster_name = get_random_string(24)
        master_domain = "rollback.{}.dba.db".format(cluster_name)
        install_ticket["start_mysql_port"] = self.rollback_port
        install_ticket["inst_num"] = 1
        install_ticket["ticket_type"] = self.ticket_data["ticket_type"]
        sql = """show global variables where Variable_name in ('sql_mode','max_allowed_packet','lower_case_table_names',
        'innodb_strict_mode','max_heap_table_size','tmp_table_size','character_set_server','collation_server',
        'default_storage_engine','default-storage-engine')"""
        old_instance_configs = get_cluster_config(cluster_class, query_cmds=sql)
        install_ticket["apply_infos"] = [
            {
                "new_ip": self.data["rollback_ip"],
                "old_instance_configs": {str(master.port): old_instance_configs},
                "clusters": [{"name": cluster_name, "master": master_domain}],
            }
        ]
        # 初始化安装mysql
        pipeline.add_sub_pipeline(
            MySQLSingleApplyFlow(root_id=self.root_id, data=install_ticket).deploy_mysql_single_flow()
        )
        mycluster = {
            "bk_cloud_id": cluster_class.bk_cloud_id,
            "databases": "*",
            "tables": "*",
            "databases_ignore": "",
            "tables_ignore": "",
            "charset": self.data["charset"],
            "change_master": False,
            "cluster_type": cluster_class.cluster_type,
            "file_target_path": "/data/dbbak/{}/{}".format(self.root_id, master.port),
            "skip_local_exists": True,
            "backupinfo": self.data["backupinfo"],
            "rollback_ip": self.rollback_ip,
            "rollback_port": self.rollback_port,
        }
        # 回档备份文件
        pipeline.add_sub_pipeline(
            sub_flow=rollback_remote_and_backupid(
                root_id=self.root_id, ticket_data=copy.deepcopy(self.data), cluster_info=mycluster
            )
        )
        # 回档成功,回收资源
        pipeline.add_sub_pipeline(
            MySQLSingleDestroyFlow(root_id=self.root_id, data=install_ticket).destroy_mysql_single_subflow(
                ip=self.rollback_ip,
                port=self.rollback_port,
                bk_cloud_id=cluster_class.bk_cloud_id,
                domain=master_domain,
                bk_biz_id=self.rollback_to_bk_biz_id,
            )
        )
        # run pipeline
        pipeline.run_pipeline(init_trans_data_class=SingleApplyManualContext())
