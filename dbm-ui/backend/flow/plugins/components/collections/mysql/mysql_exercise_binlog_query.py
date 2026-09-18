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

from backend.db_report.mysql_backup.handers import MySQLBackupHandler
from backend.flow.engine.bamboo.scene.mysql.common.get_local_backup import check_binlog_missing
from backend.flow.plugins.components.collections.common.base_service import BaseService
from backend.flow.plugins.components.collections.mysql.mysql_download_backupfile import MySQLDownloadBackupfile
from backend.utils.time import str2datetime

logger = logging.getLogger("flow")


def _write_rollback_error(data, trans_data, err_text: str):
    if trans_data is not None:
        trans_data.rollback_error_info = {"error_logs": err_text}
        data.outputs["trans_data"] = trans_data


class MysqlExerciseBinlogQueryService(BaseService):
    """运行时查询演练窗口 binlog，失败只出状态码，不抛异常。"""

    def _fail(self, data, trans_data, err_text: str) -> bool:
        self.log_error(err_text)
        _write_rollback_error(data, trans_data, err_text)
        data.outputs.binlog_query_code = 1
        return False

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs") or {}
        trans_data = data.get_one_of_inputs("trans_data")
        backup_info = kwargs.get("backup_info") or {}
        backup_time = kwargs.get("backup_time")
        rollback_time = kwargs.get("rollback_time")
        cluster_id = kwargs.get("cluster_id")

        try:
            start_time = str2datetime(backup_time)
            end_time = str2datetime(rollback_time)
        except Exception as exc:
            return self._fail(data, trans_data, _("演练 binlog 窗口时间解析失败: {}").format(str(exc)))

        try:
            handler = MySQLBackupHandler(cluster_id=cluster_id, backup_id=backup_info.get("backup_id"))
            binlog_result = handler.get_binlog_for_rollback(backup_info, start_time, end_time)
        except Exception as exc:
            return self._fail(data, trans_data, _("查询演练窗口 binlog 失败: {}").format(str(exc)))

        query_error = binlog_result.get("query_binlog_error")
        if query_error:
            return self._fail(data, trans_data, str(query_error))

        binlog_files = binlog_result.get("binlog_files_list") or []
        if not binlog_files:
            return self._fail(data, trans_data, _("窗口内未查询到 binlog 文件"))

        missing_files, check_ok = check_binlog_missing(binlog_files)
        if not check_ok:
            return self._fail(data, trans_data, _("binlog 文件不连续，缺失: {}").format(missing_files))

        trans_data.binlog_task_ids = binlog_result.get("binlog_task_ids") or []
        trans_data.binlog_files_list = binlog_files
        trans_data.binlog_start_file = binlog_result.get("binlog_start_file")
        trans_data.binlog_start_pos = binlog_result.get("binlog_start_pos")
        data.outputs["trans_data"] = trans_data
        data.outputs.binlog_query_code = 0
        self.log_info(_("查询演练窗口 binlog 成功，文件数 {}，起始 {}").format(len(binlog_files), trans_data.binlog_start_file))
        return True


class MysqlExerciseBinlogQueryComponent(Component):
    name = __name__
    code = "mysql_exercise_binlog_query"
    bound_service = MysqlExerciseBinlogQueryService


class MysqlExerciseBinlogDownloadService(MySQLDownloadBackupfile):
    """运行时从 trans_data 读取 task_ids，复用备份系统下载接口。"""

    def _fail_download(self, data, trans_data, err_text: str) -> bool:
        self.log_error(err_text)
        _write_rollback_error(data, trans_data, err_text)
        data.outputs.binlog_download_code = 1
        return False

    def _execute(self, data, parent_data) -> bool:
        kwargs = data.get_one_of_inputs("kwargs") or {}
        trans_data = data.get_one_of_inputs("trans_data")
        task_ids = getattr(trans_data, "binlog_task_ids", None) or []
        if not task_ids:
            return self._fail_download(data, trans_data, _("未找到 binlog_task_ids"))
        kwargs["task_ids"] = task_ids
        ok = super()._execute(data, parent_data)
        if not ok:
            return self._fail_download(data, trans_data, _("演练 binlog 下载发起失败"))
        return True

    def _schedule(self, data, parent_data, callback_data=None):
        result = super()._schedule(data, parent_data, callback_data)
        if getattr(self, "interval", None):
            return result
        data.outputs.binlog_download_code = 0 if result else 1
        if not result:
            trans_data = data.get_one_of_inputs("trans_data")
            _write_rollback_error(data, trans_data, _("演练 binlog 下载失败"))
        return result


class MysqlExerciseBinlogDownloadComponent(Component):
    name = __name__
    code = "mysql_exercise_binlog_download"
    bound_service = MysqlExerciseBinlogDownloadService
