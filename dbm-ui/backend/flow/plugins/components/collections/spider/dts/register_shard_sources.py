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
import json
import logging

from django.utils.translation import gettext as _
from pipeline.component_framework.component import Component
from redis.exceptions import LockError, RedisError

from backend.components import MySQLDTSApi
from backend.components.mysqldtsapi.redact import redact_passwords
from backend.db_meta.models import Cluster
from backend.flow.plugins.components.collections.common.base_service import BaseService
from backend.flow.utils.mysql.dts.migrate_helper import build_create_source_request
from backend.flow.utils.mysql.dts.migrate_plan import dts_task_spec_from_dict
from backend.flow.utils.mysql.dts.source_helper import require_dump_from_echo
from backend.utils.redis import RedisConn

logger = logging.getLogger("flow")


class SpiderDtsRegisterShardSourcesService(BaseService):
    """按分片批量注册 spider-shard Source，并把分片与空闲 Worker 一对一绑定。

    Worker 绑定在运行时完成：本单部署的 Worker 此时已注册（ensure_cluster 之后），
    复用已有集群时通过 list_workers 只挑未被其他 source 占用的空闲 Worker。
    重试幂等：已存在的 source 名跳过创建。
    """

    def _resolve_free_workers(self, master_addr: str, bk_cloud_id: int) -> list[str]:
        resp = MySQLDTSApi.list_workers(master_addr, bk_cloud_id=int(bk_cloud_id))
        free_workers = sorted(
            item.name for item in (resp.data or []) if item.name and not (item.bound_source_name or "")
        )
        return free_workers

    def _resolve_existing_names(self, master_addr: str, bk_cloud_id: int) -> set[str]:
        list_resp = MySQLDTSApi.list_sources(master_addr, bk_cloud_id=int(bk_cloud_id))
        return {item.source_name for item in (list_resp.data or []) if item.source_name}

    @staticmethod
    def _source_bind_lock_key(master_addr: str, bk_cloud_id: int) -> str:
        return "dts:source_bind:{}:{}".format(bk_cloud_id, master_addr)

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs")
        trans_data = data.get_one_of_inputs("trans_data")
        master_addr = kwargs.get("master_addr") or trans_data.migrate_context.master_addr
        bk_cloud_id = kwargs.get("bk_cloud_id")
        if bk_cloud_id is None:
            bk_cloud_id = trans_data.migrate_context.bk_cloud_id
        if not master_addr:
            self.log_error(_("DTS master_addr 为空"))
            return False
        if bk_cloud_id is None:
            self.log_error(_("DTS bk_cloud_id 为空"))
            return False
        task_spec = dts_task_spec_from_dict(kwargs["task_spec"])
        dts_user = kwargs.get("dts_user") or trans_data.migrate_context.dts_user
        dts_password = kwargs.get("dts_password") or trans_data.migrate_context.dts_password
        if not dts_user or not dts_password:
            self.log_error(_("DTS 迁移临时账号未创建，请先执行 prepare_user / AddUser 步骤"))
            return False

        sources = sorted(task_spec.sources, key=lambda item: (item.shard_index is None, item.shard_index or 0))
        if not sources:
            self.log_error(_("task_spec 无 sources，无法注册分片 Source"))
            return False
        if any(source.shard_index is None for source in sources):
            self.log_error(_("存在未展开分片的 source，TenDBCluster 源必须先按分片展开"))
            return False

        migrate_type = kwargs.get("migrate_type") or ""
        task_mode = (task_spec.dts_task_config.task_mode if task_spec.dts_task_config else "") or "all"
        full_load_engine = ""
        if task_spec.dts_task_config:
            full_load_engine = task_spec.dts_task_config.full_load_engine or ""

        registered = []
        target_cluster = Cluster.objects.get(id=task_spec.target_cluster_id)
        lock_key = self._source_bind_lock_key(master_addr, bk_cloud_id)
        try:
            with RedisConn.lock(lock_key, timeout=900, blocking_timeout=60):
                try:
                    existing_names = self._resolve_existing_names(master_addr, bk_cloud_id)
                except Exception as exc:  # pylint: disable=broad-except
                    # 已有 source 列表是唯一权威事实源：查不到就属于「未知」，不能当作「未注册」继续 create。
                    # 此时本次尚未产生任何写入，直接失败交由上层重试，避免重做已注册的 source。
                    self.log_error(_("查询已有 source 列表失败，无法确认注册状态: {}").format(exc))
                    return False

                # 重试时已注册的 source 已占用 Worker，空闲 Worker 只需覆盖尚未注册的分片
                pending = [source for source in sources if source.source_name not in existing_names]
                free_workers = self._resolve_free_workers(master_addr, bk_cloud_id) if pending else []
                if len(free_workers) < len(pending):
                    self.log_error(
                        _("空闲 DTS Worker 数量({}) 少于待注册分片数({})，无法一一绑定").format(len(free_workers), len(pending))
                    )
                    return False

                bind_idx = 0
                for source_spec in sources:
                    if source_spec.source_name in existing_names:
                        self.log_info(_("source {} 已存在，跳过注册").format(source_spec.source_name))
                        registered.append(source_spec.source_name)
                        continue
                    cluster = Cluster.objects.get(id=source_spec.cluster_id)
                    request = build_create_source_request(
                        source_spec,
                        cluster,
                        user=dts_user,
                        password=dts_password,
                        worker_name=free_workers[bind_idx],
                        target_cluster=target_cluster,
                        migrate_type=migrate_type,
                        task_mode=task_mode,
                        full_load_engine=full_load_engine,
                    )
                    bind_idx += 1
                    self.log_info(
                        _("注册分片 Source {} shard={}/{} worker={} (源集群={}, 目标集群={})").format(
                            source_spec.source_name,
                            source_spec.shard_index,
                            source_spec.shard_count,
                            request.worker_name or "",
                            cluster.id,
                            target_cluster.id,
                        )
                    )
                    resp = MySQLDTSApi.create_source(master_addr, request, bk_cloud_id=int(bk_cloud_id))
                    self.log_info(
                        _("创建 source 的返回结果: {}").format(
                            json.dumps(redact_passwords(resp.model_dump(mode="json")), ensure_ascii=False)
                        )
                    )
                    require_dump_from_echo(request.source.dump_from, resp.dump_from)
                    source_name = resp.source_name or source_spec.source_name
                    registered.append(source_name)
                    self.log_info(_("注册 DTS Source 成功: {}").format(source_name))
        except (LockError, RedisError) as exc:
            self.log_error(_("获取 DTS source/worker 绑定锁失败: {}").format(exc))
            return False

        existing = list(trans_data.migrate_context.registered_source_names or [])
        existing.extend(registered)
        trans_data.migrate_context.registered_source_names = existing
        data.outputs["trans_data"] = trans_data
        data.outputs.registered_source_names = registered
        return True


class SpiderDtsRegisterShardSourcesComponent(Component):
    name = __name__
    code = "spider_dts_register_shard_sources"
    bound_service = SpiderDtsRegisterShardSourcesService
