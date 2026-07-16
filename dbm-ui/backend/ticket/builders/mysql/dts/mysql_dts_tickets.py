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
from rest_framework import serializers

from backend.db_meta.enums import ClusterType
from backend.flow.engine.controller.mysql import MySQLController
from backend.flow.utils.mysql.dts.constants import FullLoadEngine, MigrateTopology, get_default_deploy_path
from backend.flow.utils.mysql.dts.migrate_plan import build_migrate_plan
from backend.ticket import builders
from backend.ticket.builders.mysql.base import BaseMySQLTicketFlowBuilder
from backend.ticket.constants import TicketType


class DtsHostSpecSerializer(serializers.Serializer):
    ip = serializers.CharField(help_text=_("主机IP"))
    bk_cloud_id = serializers.IntegerField(help_text=_("云区域ID"))
    name = serializers.CharField(required=False, allow_blank=True, help_text=_("节点名称"))


class SyncScopeSerializer(serializers.Serializer):
    do_dbs = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    ignore_dbs = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    do_tables = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    ignore_tables = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    table_routes = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    binlog_filters = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class MyloaderSpecSerializer(serializers.Serializer):
    """myloader 全量导入参数（可选；未传时由 Flow 按默认路径组装）。"""

    backup_id = serializers.CharField(required=False, allow_blank=True, help_text=_("指定备份 ID（可选，默认取最新逻辑全备）"))
    backup_source = serializers.CharField(
        required=False, allow_blank=True, default="remote", help_text=_("备份源: remote | local")
    )
    myloader_path = serializers.CharField(required=False, allow_blank=True, help_text=_("myloader 可执行文件路径（可选）"))
    myloader_dir = serializers.CharField(required=False, allow_blank=True, help_text=_("全备落盘目录（可选，默认由 Flow 下发）"))
    # 兼容旧字段名
    directory = serializers.CharField(required=False, allow_blank=True, help_text=_("备份目录别名，等同 myloader_dir"))
    threads = serializers.IntegerField(required=False, default=16, min_value=1, help_text=_("并发线程数"))
    regex = serializers.CharField(required=False, allow_blank=True, help_text=_("库表过滤 regex（可选）"))
    sourcedb = serializers.CharField(required=False, allow_blank=True, help_text=_("--source-db（可选）"))
    tablelist = serializers.CharField(required=False, allow_blank=True, help_text=_("--tables-list（可选）"))
    setnames = serializers.CharField(required=False, allow_blank=True, help_text=_("--set-names（可选）"))
    defaultsfile = serializers.CharField(required=False, allow_blank=True, help_text=_("defaults-file 路径（可选）"))
    defaults_file = serializers.CharField(
        required=False, allow_blank=True, help_text=_("defaults-file 别名，等同 defaultsfile")
    )
    extraargs = serializers.CharField(required=False, allow_blank=True, help_text=_("额外参数（可选）"))
    extra_args = serializers.CharField(required=False, allow_blank=True, help_text=_("额外参数别名，等同 extraargs"))
    dest_worker_ip = serializers.CharField(required=False, allow_blank=True, help_text=_("全备下发目标 DTS Worker IP（可选）"))
    shard_id = serializers.IntegerField(required=False, allow_null=True, help_text=_("TenDBCluster 分片 ID（可选）"))


class SourceInfoSerializer(serializers.Serializer):
    cluster_id = serializers.IntegerField(help_text=_("源集群ID"))
    source_name = serializers.CharField(required=False, allow_blank=True)
    sync_scope = SyncScopeSerializer(required=False)
    source_instance_id = serializers.IntegerField(required=False)
    source_instance_role = serializers.CharField(required=False, allow_blank=True)
    source_host = serializers.CharField(required=False, allow_blank=True)
    myloader = MyloaderSpecSerializer(required=False, help_text=_("该源的 myloader 参数（可选）"))


class TargetInfoSerializer(serializers.Serializer):
    cluster_id = serializers.IntegerField(help_text=_("目标集群ID"))
    task_name = serializers.CharField(required=False, allow_blank=True)
    sync_scope = SyncScopeSerializer(required=False)


class OneToOneSpecSerializer(serializers.Serializer):
    src_info = SourceInfoSerializer()
    dst_info = TargetInfoSerializer()
    task_name = serializers.CharField(required=False, allow_blank=True)


class ManyToOneSpecSerializer(serializers.Serializer):
    src_infos = serializers.ListSerializer(child=SourceInfoSerializer())
    dst_info = TargetInfoSerializer()
    task_name = serializers.CharField(required=False, allow_blank=True)


class OneToManySpecSerializer(serializers.Serializer):
    src_info = SourceInfoSerializer()
    dst_infos = serializers.ListSerializer(child=TargetInfoSerializer())


class DtsTaskConfigSerializer(serializers.Serializer):
    task_mode = serializers.CharField(default="all")
    enable_validator = serializers.BooleanField(default=False)
    shard_mode = serializers.CharField(required=False, allow_blank=True, default="")
    on_duplicate = serializers.CharField(required=False, allow_blank=True, default="replace")
    meta_schema = serializers.CharField(required=False, allow_blank=True, default="dm_meta")
    ignore_checking_items = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    full_migrate = serializers.DictField(required=False, default=dict)
    incr_migrate = serializers.DictField(required=False, default=dict)
    full_load_engine = serializers.ChoiceField(
        choices=FullLoadEngine.get_choices(),
        required=False,
        default=FullLoadEngine.BUILTIN.value,
        help_text=_("全量导入引擎: builtin | myloader"),
    )
    myloader = MyloaderSpecSerializer(required=False, help_text=_("task 级 myloader 默认参数（可选）"))


class DeploySubflowSerializer(serializers.Serializer):
    cluster_name = serializers.CharField(required=False, allow_blank=True)
    bk_cloud_id = serializers.IntegerField(required=False)
    master_hosts = serializers.ListSerializer(child=DtsHostSpecSerializer())
    worker_hosts = serializers.ListSerializer(child=DtsHostSpecSerializer())
    deploy_path = serializers.CharField(required=False, allow_blank=True)
    master_ha = serializers.BooleanField(default=False)


class MysqlMigrateBaseDetailSerializer(serializers.Serializer):
    migrate_topology = serializers.ChoiceField(choices=MigrateTopology.get_choices())
    dts_cluster_id = serializers.IntegerField(required=False)
    auto_deploy_dts = serializers.BooleanField(default=False)
    dts_lifecycle = serializers.CharField(required=False, allow_blank=True)
    cleanup_after_migrate = serializers.BooleanField(required=False)
    recycle_dts_hosts = serializers.BooleanField(default=True)
    dts_task_config = DtsTaskConfigSerializer(required=False)
    deploy_subflow = DeploySubflowSerializer(required=False)
    one_to_one = OneToOneSpecSerializer(required=False)
    many_to_one = ManyToOneSpecSerializer(required=False)
    one_to_many = OneToManySpecSerializer(required=False)

    def validate(self, attrs):
        topology = attrs["migrate_topology"]
        topology_fields = {
            MigrateTopology.ONE_TO_ONE.value: "one_to_one",
            MigrateTopology.MANY_TO_ONE.value: "many_to_one",
            MigrateTopology.ONE_TO_MANY.value: "one_to_many",
        }
        field_name = topology_fields[topology]
        if not attrs.get(field_name):
            raise serializers.ValidationError(_("拓扑 {} 必须填写 {} 字段").format(topology, field_name))
        if not attrs.get("dts_cluster_id") and attrs.get("auto_deploy_dts") and not attrs.get("deploy_subflow"):
            raise serializers.ValidationError(_("自动部署 DTS 时必须填写 deploy_subflow"))
        attrs["migrate_plan"] = build_migrate_plan({**attrs, "bk_biz_id": self.context.get("bk_biz_id", 0)})
        return attrs


class MysqlDtsClusterApplyDetailSerializer(serializers.Serializer):
    cluster_name = serializers.CharField(help_text=_("DTS集群名称"))
    bk_cloud_id = serializers.IntegerField(help_text=_("云区域ID"))
    master_hosts = serializers.ListSerializer(child=DtsHostSpecSerializer())
    worker_hosts = serializers.ListSerializer(child=DtsHostSpecSerializer())
    deploy_path = serializers.CharField(required=False, allow_blank=True)
    master_ha = serializers.BooleanField(default=False)


class MysqlDtsClusterDestroyDetailSerializer(serializers.Serializer):
    dts_cluster_id = serializers.IntegerField(help_text=_("DTS集群ID"))
    force_destroy = serializers.BooleanField(default=False)
    recycle_hosts = serializers.BooleanField(default=True)
    clean_data_dir = serializers.BooleanField(default=True)


class MysqlDtsClusterApplyFlowParamBuilder(builders.FlowParamBuilder):
    controller = MySQLController.mysql_dts_cluster_apply_scene

    def format_ticket_data(self):
        if not self.ticket_data.get("deploy_path"):
            self.ticket_data["deploy_path"] = get_default_deploy_path(self.ticket_data["cluster_name"])


@builders.BuilderFactory.register(TicketType.MYSQL_DTS_CLUSTER_APPLY, is_apply=True, cluster_type=ClusterType.MySQLDTS)
class MysqlDtsClusterApplyFlowBuilder(BaseMySQLTicketFlowBuilder):
    serializer = MysqlDtsClusterApplyDetailSerializer
    inner_flow_builder = MysqlDtsClusterApplyFlowParamBuilder
    inner_flow_name = _("MySQL DTS 集群部署")


class MysqlDtsClusterDestroyFlowParamBuilder(builders.FlowParamBuilder):
    controller = MySQLController.mysql_dts_cluster_destroy_scene


@builders.BuilderFactory.register(TicketType.MYSQL_DTS_CLUSTER_DESTROY, is_recycle=True)
class MysqlDtsClusterDestroyFlowBuilder(BaseMySQLTicketFlowBuilder):
    serializer = MysqlDtsClusterDestroyDetailSerializer
    inner_flow_builder = MysqlDtsClusterDestroyFlowParamBuilder
    inner_flow_name = _("MySQL DTS 集群销毁")


class MysqlHaToHaMigrateFlowParamBuilder(builders.FlowParamBuilder):
    controller = MySQLController.mysql_ha_to_ha_migrate_scene

    def format_ticket_data(self):
        self.ticket_data["ticket_id"] = self.ticket.id
        if "migrate_plan" not in self.ticket_data:
            self.ticket_data["migrate_plan"] = build_migrate_plan(self.ticket_data)


@builders.BuilderFactory.register(TicketType.MYSQL_HA_TO_HA_MIGRATE)
class MysqlHaToHaMigrateFlowBuilder(BaseMySQLTicketFlowBuilder):
    serializer = MysqlMigrateBaseDetailSerializer
    inner_flow_builder = MysqlHaToHaMigrateFlowParamBuilder
    inner_flow_name = _("MySQL HA到HA数据迁移")


class MysqlHaToClusterMigrateFlowParamBuilder(builders.FlowParamBuilder):
    controller = MySQLController.mysql_ha_to_cluster_migrate_scene

    def format_ticket_data(self):
        self.ticket_data["ticket_id"] = self.ticket.id
        self.ticket_data["migrate_type"] = "ha_to_cluster"
        if "migrate_plan" not in self.ticket_data:
            self.ticket_data["migrate_plan"] = build_migrate_plan(self.ticket_data)


@builders.BuilderFactory.register(TicketType.MYSQL_HA_TO_CLUSTER_MIGRATE)
class MysqlHaToClusterMigrateFlowBuilder(BaseMySQLTicketFlowBuilder):
    serializer = MysqlMigrateBaseDetailSerializer
    inner_flow_builder = MysqlHaToClusterMigrateFlowParamBuilder
    inner_flow_name = _("MySQL HA到Cluster数据迁移")
