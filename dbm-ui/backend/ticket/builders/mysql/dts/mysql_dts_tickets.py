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
from django.utils.translation import gettext as gettext_runtime
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from backend.db_meta.enums import ClusterType
from backend.flow.engine.controller.mysql import MySQLController
from backend.flow.utils.mysql.dts.constants import (
    DtsLifecycleMode,
    FullLoadEngine,
    MigrateTopology,
    get_default_deploy_path,
)
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
    """myloader 全量导入参数（full_load.engine=myloader 时使用）。"""

    backup_id = serializers.CharField(required=False, allow_blank=True, help_text=_("指定备份 ID（可选，默认取最新逻辑全备）"))
    backup_source = serializers.CharField(
        required=False, allow_blank=True, default="remote", help_text=_("备份源: remote | local")
    )
    myloader_path = serializers.CharField(required=False, allow_blank=True, help_text=_("myloader 可执行文件路径（可选）"))
    myloader_dir = serializers.CharField(required=False, allow_blank=True, help_text=_("全备落盘目录（可选，默认由 Flow 下发）"))
    threads = serializers.IntegerField(required=False, default=16, min_value=1, help_text=_("并发线程数"))
    regex = serializers.CharField(required=False, allow_blank=True, help_text=_("库表过滤 regex（可选）"))
    sourcedb = serializers.CharField(required=False, allow_blank=True, help_text=_("--source-db（可选）"))
    tablelist = serializers.CharField(required=False, allow_blank=True, help_text=_("--tables-list（可选）"))
    setnames = serializers.CharField(required=False, allow_blank=True, help_text=_("--set-names（可选）"))
    defaultsfile = serializers.CharField(required=False, allow_blank=True, help_text=_("defaults-file 路径（可选）"))
    extraargs = serializers.CharField(required=False, allow_blank=True, help_text=_("额外参数（可选）"))
    dest_worker_ip = serializers.CharField(
        required=False, allow_blank=True, help_text=_("全备下发目标 DTS Worker IP（可选）")
    )
    shard_id = serializers.IntegerField(required=False, allow_null=True, help_text=_("TenDBCluster 分片 ID（可选）"))


class MigrateSourceSerializer(serializers.Serializer):
    cluster_id = serializers.IntegerField(help_text=_("源集群ID"))
    source_name = serializers.CharField(required=False, allow_blank=True, help_text=_("DTS Source 名称（可选）"))
    sync_scope = SyncScopeSerializer(required=False, help_text=_("库表同步范围"))
    source_instance_id = serializers.IntegerField(required=False, help_text=_("指定源实例 ID（可选）"))
    source_instance_role = serializers.CharField(required=False, allow_blank=True, help_text=_("指定源实例角色（可选）"))
    source_host = serializers.CharField(required=False, allow_blank=True, help_text=_("指定源地址 ip:port（可选）"))
    myloader = MyloaderSpecSerializer(required=False, help_text=_("该源的 myloader 参数（可选）"))


class MigrateTargetSerializer(serializers.Serializer):
    cluster_id = serializers.IntegerField(help_text=_("目标集群ID"))
    task_name = serializers.CharField(required=False, allow_blank=True, help_text=_("任务名（一对多时可用）"))
    sync_scope = SyncScopeSerializer(required=False, help_text=_("目标侧库表范围覆盖（可选）"))


class MigrateOneToOneSerializer(serializers.Serializer):
    task_name = serializers.CharField(required=False, allow_blank=True, help_text=_("DTS 任务名"))
    source = MigrateSourceSerializer(help_text=_("源集群"))
    target = MigrateTargetSerializer(help_text=_("目标集群"))


class MigrateManyToOneSerializer(serializers.Serializer):
    task_name = serializers.CharField(required=False, allow_blank=True, help_text=_("DTS 任务名"))
    sources = serializers.ListSerializer(child=MigrateSourceSerializer(), help_text=_("多个源集群"))
    target = MigrateTargetSerializer(help_text=_("目标集群"))


class MigrateOneToManySerializer(serializers.Serializer):
    source = MigrateSourceSerializer(help_text=_("源集群"))
    targets = serializers.ListSerializer(child=MigrateTargetSerializer(), help_text=_("多个目标集群"))


class MigrateSpecSerializer(serializers.Serializer):
    """迁什么：拓扑 + 源/目标。"""

    topology = serializers.ChoiceField(choices=MigrateTopology.get_choices(), help_text=_("迁移拓扑"))
    one_to_one = MigrateOneToOneSerializer(required=False)
    many_to_one = MigrateManyToOneSerializer(required=False)
    one_to_many = MigrateOneToManySerializer(required=False)

    def validate(self, attrs):
        topology = attrs["topology"]
        field_map = {
            MigrateTopology.ONE_TO_ONE.value: "one_to_one",
            MigrateTopology.MANY_TO_ONE.value: "many_to_one",
            MigrateTopology.ONE_TO_MANY.value: "one_to_many",
        }
        field_name = field_map[topology]
        if not attrs.get(field_name):
            raise serializers.ValidationError(
                gettext_runtime("拓扑 {} 必须填写 migrate.{}").format(topology, field_name)
            )
        return attrs


class DtsDeploySerializer(serializers.Serializer):
    cluster_name = serializers.CharField(required=False, allow_blank=True, help_text=_("DTS 集群名"))
    bk_cloud_id = serializers.IntegerField(required=False, help_text=_("云区域ID"))
    master_hosts = serializers.ListSerializer(child=DtsHostSpecSerializer(), help_text=_("Master 主机列表"))
    worker_hosts = serializers.ListSerializer(child=DtsHostSpecSerializer(), help_text=_("Worker 主机列表"))
    deploy_path = serializers.CharField(required=False, allow_blank=True, help_text=_("部署路径（可选）"))
    master_ha = serializers.BooleanField(default=False, help_text=_("是否 Master HA"))


class DtsResourceSerializer(serializers.Serializer):
    """DTS 集群从哪来、迁完怎么办。"""

    mode = serializers.ChoiceField(
        choices=DtsLifecycleMode.get_choices(),
        help_text=_("DTS 资源模式: use_existing | deploy_ephemeral | deploy_persistent"),
    )
    cluster_id = serializers.IntegerField(required=False, help_text=_("已有 DTS 集群 ID（mode=use_existing 时必填）"))
    deploy = DtsDeploySerializer(required=False, help_text=_("部署参数（mode=deploy_* 时必填）"))
    cleanup_after_migrate = serializers.BooleanField(
        required=False, help_text=_("迁移结束后是否清理临时 DTS（默认：ephemeral=true）")
    )
    recycle_hosts = serializers.BooleanField(required=False, default=True, help_text=_("清理时是否回收主机"))

    def validate(self, attrs):
        mode = attrs["mode"]
        if mode == DtsLifecycleMode.USE_EXISTING.value:
            if not attrs.get("cluster_id"):
                raise serializers.ValidationError(gettext_runtime("mode=use_existing 时必须填写 cluster_id"))
        elif mode in (DtsLifecycleMode.DEPLOY_EPHEMERAL.value, DtsLifecycleMode.DEPLOY_PERSISTENT.value):
            if not attrs.get("deploy"):
                raise serializers.ValidationError(gettext_runtime("mode={} 时必须填写 deploy").format(mode))
        return attrs


class FullLoadSerializer(serializers.Serializer):
    engine = serializers.ChoiceField(
        choices=FullLoadEngine.get_choices(),
        default=FullLoadEngine.BUILTIN.value,
        help_text=_("全量导入引擎: builtin | myloader"),
    )
    myloader = MyloaderSpecSerializer(required=False, help_text=_("engine=myloader 时的参数"))


class TaskSpecSerializer(serializers.Serializer):
    """任务怎么跑。"""

    task_mode = serializers.CharField(required=False, default="all", help_text=_("任务模式: all | full | incremental"))
    full_load = FullLoadSerializer(required=False, help_text=_("全量导入配置"))
    enable_validator = serializers.BooleanField(required=False, default=False, help_text=_("是否开启数据校验"))
    shard_mode = serializers.CharField(required=False, allow_blank=True, default="", help_text=_("分片模式（可选）"))
    on_duplicate = serializers.CharField(required=False, default="replace", help_text=_("冲突策略"))
    meta_schema = serializers.CharField(required=False, default="dm_meta", help_text=_("元数据库名"))
    ignore_checking_items = serializers.ListField(
        child=serializers.CharField(), required=False, default=list, help_text=_("忽略的检查项")
    )
    engine_options = serializers.DictField(
        required=False,
        default=dict,
        help_text=_("引擎透传选项，如 {full_migrate: {}, incr_migrate: {}}"),
    )


class MysqlMigrateBaseDetailSerializer(serializers.Serializer):
    """迁移单据分层入参：dts_resource + migrate + task。"""

    dts_resource = DtsResourceSerializer(help_text=_("DTS 资源（集群来源与生命周期）"))
    migrate = MigrateSpecSerializer(help_text=_("迁移拓扑与源/目标"))
    task = TaskSpecSerializer(required=False, help_text=_("任务运行参数"))

    def validate(self, attrs):
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
