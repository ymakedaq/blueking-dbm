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
from datetime import datetime
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from backend.db_meta.enums import InstanceInnerRole
from backend.db_services.mysql.sql_import import large_ddl_tables as mod
from backend.db_services.mysql.sql_import.exceptions import SQLImportBaseException

OVER_SIZE = mod.LARGE_DDL_TABLE_SIZE_BYTES + 1
EQ_SIZE = mod.LARGE_DDL_TABLE_SIZE_BYTES


def _cluster(cluster_id=1, domain="gamedb.example.db", bk_biz_id=100):
    cluster = MagicMock()
    cluster.id = cluster_id
    cluster.immute_domain = domain
    cluster.bk_biz_id = bk_biz_id
    return cluster


def _size_qs(rows):
    qs = MagicMock()
    qs.values.return_value.annotate.return_value.order_by.return_value = rows
    return qs


def _parse_summary(alter=None, drop=None, truncate=None):
    return {
        "command_counts": {},
        "alter_tables": alter or [],
        "drop_tables": drop or [],
        "truncate_tables": truncate or [],
    }


class TestQueryLargeDdlTables(SimpleTestCase):
    def setUp(self):
        self.cluster = _cluster()
        self.execute_objects = [{"sql_files": ["a.sql"], "dbnames": ["db1"], "ignore_dbnames": []}]
        self.parse_patcher = patch.object(mod.SQLSimulationApi, "parse_file_statement")
        self.cluster_patcher = patch.object(mod.Cluster.objects, "filter", return_value=[self.cluster])
        self.size_patcher = patch.object(mod.MysqlDbTableSize.objects, "filter")
        self.remote_patcher = patch.object(mod, "RemoteServiceHandler")
        self.mock_parse = self.parse_patcher.start()
        self.cluster_patcher.start()
        self.mock_filter = self.size_patcher.start()
        self.mock_remote = self.remote_patcher.start()
        self.addCleanup(self.parse_patcher.stop)
        self.addCleanup(self.cluster_patcher.stop)
        self.addCleanup(self.size_patcher.stop)
        self.addCleanup(self.remote_patcher.stop)

    def _slave_rows(self, *rows):
        def _filter(*args, **kwargs):
            if kwargs.get("instance_role") == InstanceInnerRole.SLAVE.value:
                return _size_qs(list(rows))
            return _size_qs([])

        self.mock_filter.side_effect = _filter

    def test_alter_drop_truncate_over_threshold(self):
        hour = timezone.now()
        self.mock_parse.return_value = _parse_summary(
            alter=[{"file_name": "a.sql", "alters": [{"db_name": "db1", "table_name": "t1"}]}],
            drop=[{"file_name": "a.sql", "tables": [{"db_name": "db1", "table_name": "t2"}]}],
            truncate=[{"file_name": "a.sql", "tables": [{"db_name": "db1", "table_name": "t3"}]}],
        )
        self._slave_rows(
            {"database_name": "db1", "table_name": "t1", "dteventtimehour": hour, "table_size": OVER_SIZE},
            {"database_name": "db1", "table_name": "t2", "dteventtimehour": hour, "table_size": OVER_SIZE},
            {"database_name": "db1", "table_name": "t3", "dteventtimehour": hour, "table_size": OVER_SIZE},
        )
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", self.execute_objects)
        tables = {(item["table_name"], item["file_name"], tuple(item["sql_types"])) for item in result}
        self.assertEqual(
            tables,
            {
                ("t1", "a.sql", ("alter_table",)),
                ("t2", "a.sql", ("drop_table",)),
                ("t3", "a.sql", ("truncate",)),
            },
        )
        self.assertTrue(all(item["table_size"] == OVER_SIZE for item in result))
        self.mock_remote.assert_not_called()

    def test_equal_threshold_and_missing_report_skipped(self):
        hour = timezone.now()
        self.mock_parse.return_value = _parse_summary(
            alter=[
                {
                    "file_name": "a.sql",
                    "alters": [
                        {"db_name": "db1", "table_name": "eq_tbl"},
                        {"db_name": "db1", "table_name": "missing_tbl"},
                    ],
                }
            ]
        )
        self._slave_rows(
            {"database_name": "db1", "table_name": "eq_tbl", "dteventtimehour": hour, "table_size": EQ_SIZE},
        )
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", self.execute_objects)
        self.assertEqual(result, [])

    def test_empty_db_name_uses_exact_execute_dbnames(self):
        hour = timezone.now()
        self.mock_parse.return_value = _parse_summary(
            alter=[{"file_name": "a.sql", "alters": [{"db_name": "", "table_name": "t1"}]}]
        )
        self._slave_rows(
            {"database_name": "db1", "table_name": "t1", "dteventtimehour": hour, "table_size": OVER_SIZE},
        )
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", self.execute_objects)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["db_name"], "db1")
        self.assertEqual(result[0]["file_name"], "a.sql")
        self.mock_remote.assert_not_called()

    def test_wildcard_dbnames_expand_via_drs(self):
        hour = timezone.now()
        handler = MagicMock()
        handler.show_database_with_pattern.return_value = ["db_log1", "db_log2"]
        self.mock_remote.return_value = handler
        self.mock_parse.return_value = _parse_summary(
            alter=[{"file_name": "a.sql", "alters": [{"db_name": "", "table_name": "t1"}]}]
        )
        self._slave_rows(
            {"database_name": "db_log1", "table_name": "t1", "dteventtimehour": hour, "table_size": OVER_SIZE},
            {"database_name": "db_log2", "table_name": "t1", "dteventtimehour": hour, "table_size": OVER_SIZE},
        )
        execute_objects = [{"sql_files": ["a.sql"], "dbnames": ["db_log%"], "ignore_dbnames": []}]
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", execute_objects)
        self.assertEqual([item["db_name"] for item in result], ["db_log1", "db_log2"])
        handler.show_database_with_pattern.assert_called_once_with(1, ["db_log%"], [])
        slave_kwargs = self.mock_filter.call_args_list[0][1]
        self.assertEqual(set(slave_kwargs["database_name__in"]), {"db_log1", "db_log2"})
        self.assertNotIn("database_name__like", slave_kwargs)

    def test_same_table_two_files_two_rows(self):
        hour = timezone.now()
        self.mock_parse.return_value = _parse_summary(
            alter=[
                {"file_name": "a.sql", "alters": [{"db_name": "db1", "table_name": "t1"}]},
                {"file_name": "b.sql", "alters": [{"db_name": "db1", "table_name": "t1"}]},
            ]
        )
        self._slave_rows(
            {"database_name": "db1", "table_name": "t1", "dteventtimehour": hour, "table_size": OVER_SIZE},
        )
        execute_objects = [
            {"sql_files": ["a.sql", "b.sql"], "dbnames": ["db1"], "ignore_dbnames": []},
        ]
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", execute_objects)
        self.assertEqual(len(result), 2)
        self.assertEqual({item["file_name"] for item in result}, {"a.sql", "b.sql"})

    def test_same_file_alter_and_truncate_merge_sql_types(self):
        hour = timezone.now()
        self.mock_parse.return_value = _parse_summary(
            alter=[{"file_name": "a.sql", "alters": [{"db_name": "db1", "table_name": "t1"}]}],
            truncate=[{"file_name": "a.sql", "tables": [{"db_name": "db1", "table_name": "t1"}]}],
        )
        self._slave_rows(
            {"database_name": "db1", "table_name": "t1", "dteventtimehour": hour, "table_size": OVER_SIZE},
        )
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", self.execute_objects)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sql_types"], ["alter_table", "truncate"])

    def test_tendbcluster_latest_hour_uses_summed_size(self):
        """ORM 已按同小时分片 Sum；这里验证取最近小时且超过阈值才返回。"""
        newer = timezone.make_aware(datetime(2026, 8, 20, 10, 0, 0))
        older = timezone.make_aware(datetime(2026, 8, 20, 9, 0, 0))
        self.mock_parse.return_value = _parse_summary(
            alter=[{"file_name": "a.sql", "alters": [{"db_name": "account_transvr", "table_name": "t1"}]}]
        )
        self._slave_rows(
            {
                "database_name": "account_transvr",
                "table_name": "t1",
                "dteventtimehour": newer,
                "table_size": 150 * 1024 * 1024,
            },
            {
                "database_name": "account_transvr",
                "table_name": "t1",
                "dteventtimehour": older,
                "table_size": 10,
            },
        )
        execute_objects = [{"sql_files": ["a.sql"], "dbnames": ["account_transvr"], "ignore_dbnames": []}]
        result = mod.query_large_ddl_tables([1], "mysql/sqlfile/100", execute_objects)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["table_size"], 150 * 1024 * 1024)

    def test_parse_failure_raises(self):
        self.mock_parse.side_effect = RuntimeError("sim down")
        with self.assertRaises(SQLImportBaseException):
            mod.query_large_ddl_tables([1], "mysql/sqlfile/100", self.execute_objects)

    @patch.object(mod, "query_large_ddl_tables", return_value=[{"file_name": "a.sql"}])
    @patch.object(mod.Ticket.objects, "get")
    def test_by_ticket_reads_details(self, mock_get, mock_query):
        ticket = MagicMock()
        ticket.id = 19826
        ticket.ticket_type = "MYSQL_IMPORT_SQLFILE"
        ticket.details = {
            "cluster_ids": [1],
            "path": "mysql/sqlfile/100",
            "execute_objects": self.execute_objects,
        }
        mock_get.return_value = ticket
        result = mod.query_large_ddl_tables_by_ticket(19826)
        self.assertEqual(result, [{"file_name": "a.sql"}])
        mock_query.assert_called_once_with(
            [1],
            "mysql/sqlfile/100",
            self.execute_objects,
            min_size_bytes=mod.LARGE_DDL_TABLE_SIZE_BYTES,
        )

    @patch.object(mod, "query_large_ddl_tables", return_value=[])
    @patch.object(mod, "_load_semantic_details")
    @patch.object(mod.Ticket.objects, "get")
    def test_by_ticket_fallback_semantic(self, mock_get, mock_semantic, mock_query):
        ticket = MagicMock()
        ticket.id = 19826
        ticket.ticket_type = "TENDBCLUSTER_IMPORT_SQLFILE"
        ticket.details = {"root_id": "root-1"}
        mock_get.return_value = ticket
        mock_semantic.return_value = {
            "cluster_ids": [2],
            "path": "mysql/sqlfile/200",
            "execute_objects": self.execute_objects,
        }
        mod.query_large_ddl_tables_by_ticket(19826)
        mock_semantic.assert_called_once_with("root-1")
        mock_query.assert_called_once_with(
            [2],
            "mysql/sqlfile/200",
            self.execute_objects,
            min_size_bytes=mod.LARGE_DDL_TABLE_SIZE_BYTES,
        )

    @patch.object(mod.Ticket.objects, "get", side_effect=mod.Ticket.DoesNotExist)
    def test_by_ticket_not_found(self, mock_get):
        with self.assertRaises(SQLImportBaseException):
            mod.query_large_ddl_tables_by_ticket(999)

    @patch.object(mod.Ticket.objects, "get")
    def test_by_ticket_wrong_type(self, mock_get):
        ticket = MagicMock()
        ticket.ticket_type = "MYSQL_HA_APPLY"
        mock_get.return_value = ticket
        with self.assertRaises(SQLImportBaseException):
            mod.query_large_ddl_tables_by_ticket(1)
