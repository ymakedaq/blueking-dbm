# -*- coding: utf-8 -*-
from unittest.mock import patch

from django.test import SimpleTestCase

from backend.flow.utils.mysql.dts.constants import (
    MYSQL_DTS_MIGRATE_USER_PREFIX,
    MYSQL_DTS_VERIFY_MAX_RETRIES,
    MigrateTopology,
)
from backend.flow.utils.mysql.dts.context import DtsHostSpec
from backend.flow.utils.mysql.dts.deploy_helper import build_master_node_name, group_deploy_hosts, render_master_config
from backend.flow.utils.mysql.dts.migrate_credentials import generate_dts_migrate_credentials
from backend.flow.utils.mysql.dts.migrate_helper import _build_table_migrate_rules
from backend.flow.utils.mysql.dts.migrate_plan import SyncScope, build_migrate_plan


class MigratePlanTest(SimpleTestCase):
    def test_build_one_to_one_plan(self):
        details = {
            "migrate_topology": MigrateTopology.ONE_TO_ONE.value,
            "one_to_one": {
                "src_info": {"cluster_id": 1, "source_name": "src-1"},
                "dst_info": {"cluster_id": 2},
            },
        }
        plan = build_migrate_plan(details)
        self.assertEqual(len(plan.task_specs), 1)
        self.assertEqual(plan.task_specs[0].sources[0].cluster_id, 1)
        self.assertEqual(plan.task_specs[0].target_cluster_id, 2)

    def test_build_many_to_one_plan(self):
        details = {
            "migrate_topology": MigrateTopology.MANY_TO_ONE.value,
            "many_to_one": {
                "src_infos": [{"cluster_id": 1}, {"cluster_id": 2}],
                "dst_info": {"cluster_id": 10},
            },
        }
        plan = build_migrate_plan(details)
        self.assertEqual(len(plan.task_specs), 1)
        self.assertEqual(len(plan.task_specs[0].sources), 2)
        self.assertEqual(plan.worker_count_required, 3)

    def test_parse_deploy_subflow(self):
        details = {
            "migrate_topology": MigrateTopology.ONE_TO_ONE.value,
            "auto_deploy_dts": True,
            "bk_biz_id": 100,
            "bk_cloud_id": 0,
            "deploy_subflow": {
                "cluster_name": "dts-test",
                "master_hosts": [{"ip": "127.0.0.1", "bk_cloud_id": 0}],
                "worker_hosts": [{"ip": "127.0.0.2", "bk_cloud_id": 0}],
            },
            "one_to_one": {
                "src_info": {"cluster_id": 1},
                "dst_info": {"cluster_id": 2},
            },
        }
        plan = build_migrate_plan(details)
        self.assertIsNotNone(plan.deploy_subflow_inp)
        self.assertEqual(plan.deploy_subflow_inp.cluster_name, "dts-test")
        self.assertEqual(len(plan.deploy_subflow_inp.worker_hosts), 1)


class SyncScopeMappingTest(SimpleTestCase):
    """L1：S1–S7 sync_scope → table_migrate_rule 映射验收。"""

    def _dump_rules(self, scenario_id: str, scope: SyncScope, rules):
        payload = [
            {
                "source": r.source.model_dump(),
                "target": r.target.model_dump() if r.target else None,
            }
            for r in rules
        ]
        print(f"[DTS-UT][{scenario_id}] RULES {payload}")

    def test_s1_do_dbs_partial_database(self):
        scope = SyncScope(do_dbs=["dts_ut_db_a", "dts_ut_db_b"])
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S1", scope, rules)
        self.assertEqual(len(rules), 2)
        self.assertEqual({r.source.schema for r in rules}, {"dts_ut_db_a", "dts_ut_db_b"})
        self.assertTrue(all(r.source.table == "*" for r in rules))

    def test_s2_do_tables_partial_table(self):
        scope = SyncScope(do_tables=[{"db": "dts_ut_db_c", "table": "t1"}])
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S2", scope, rules)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source.schema, "dts_ut_db_c")
        self.assertEqual(rules[0].source.table, "t1")

    def test_s3_full_db_wildcard_route(self):
        scope = SyncScope(
            table_routes=[{"source_db": "dts_ut_db_full", "source_table": "*"}],
        )
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S3", scope, rules)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source.schema, "dts_ut_db_full")
        self.assertEqual(rules[0].source.table, "*")

    def test_s4_rename_table(self):
        scope = SyncScope(
            table_routes=[
                {
                    "source_db": "dts_ut_db_r",
                    "source_table": "t_old",
                    "target_db": "dts_ut_db_r",
                    "target_table": "t_new",
                }
            ],
        )
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S4", scope, rules)
        self.assertEqual(len(rules), 1)
        self.assertIsNotNone(rules[0].target)
        self.assertEqual(rules[0].target.schema, "dts_ut_db_r")
        self.assertEqual(rules[0].target.table, "t_new")

    def test_s5_rename_database(self):
        scope = SyncScope(
            table_routes=[
                {
                    "source_db": "dts_ut_src",
                    "source_table": "t1",
                    "target_db": "dts_ut_dst",
                    "target_table": "t1",
                }
            ],
        )
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S5", scope, rules)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source.schema, "dts_ut_src")
        self.assertEqual(rules[0].target.schema, "dts_ut_dst")

    def test_s6_ignore_dbs_whitelist_subtract(self):
        scope = SyncScope(do_dbs=["dts_ut_db_a", "dts_ut_db_b"], ignore_dbs=["dts_ut_db_b"])
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S6", scope, rules)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source.schema, "dts_ut_db_a")

    def test_s7_empty_scope_yields_no_rules(self):
        scope = SyncScope()
        rules = _build_table_migrate_rules("src-ut", scope)
        self._dump_rules("S7", scope, rules)
        self.assertEqual(rules, [])

    def test_build_task_rejects_empty_rules(self):
        from backend.components.mysqldtsapi.types import TargetConfig
        from backend.flow.utils.mysql.dts.constants import DtsLifecycleMode, MigrateTopology, MigrateType
        from backend.flow.utils.mysql.dts.migrate_helper import build_dts_task_request
        from backend.flow.utils.mysql.dts.migrate_plan import DtsMigratePlan, DtsTaskConfig, DtsTaskSpec, SourceSpec

        plan = DtsMigratePlan(
            topology=MigrateTopology.ONE_TO_ONE.value,
            migrate_type=MigrateType.HA_TO_HA.value,
            dts_cluster_id=None,
            dts_lifecycle=DtsLifecycleMode.USE_EXISTING.value,
            auto_deploy_dts=False,
            deploy_subflow_inp=None,
            cleanup_after_migrate=False,
            recycle_dts_hosts=False,
            dts_task_config=DtsTaskConfig(),
            task_specs=[],
            worker_count_required=1,
        )
        task_spec = DtsTaskSpec(
            task_name="reject-empty",
            target_cluster_id=0,
            sources=[SourceSpec(cluster_id=0, source_name="src-1", sync_scope=SyncScope())],
            target_config=TargetConfig(host="127.0.0.1", port=3306, user="u", password="p", cluster_type="mysql"),
        )
        with self.assertRaises(ValueError):
            build_dts_task_request(plan, task_spec, user="u", password="p")

    def test_do_dbs_to_table_migrate_rules(self):
        scope = SyncScope(do_dbs=["db_a", "db_b"], ignore_dbs=["db_b"])
        rules = _build_table_migrate_rules("src-1", scope)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source.source_name, "src-1")
        self.assertEqual(rules[0].source.schema, "db_a")
        self.assertEqual(rules[0].source.table, "*")

    def test_table_routes_preferred(self):
        scope = SyncScope(
            do_dbs=["db_a"],
            table_routes=[{"source_db": "db_x", "source_table": "t1", "target_db": "db_y"}],
        )
        rules = _build_table_migrate_rules("src-1", scope)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].source.schema, "db_x")


class ProbeSourceEnableGtidTest(SimpleTestCase):
    """探测源/目标 gtid_mode → enable_gtid（双方都 ON 才开）。"""

    @staticmethod
    def _rpc_resp(value: str | None, *, error: str = ""):
        rows = []
        if value is not None:
            rows = [{"Variable_name": "gtid_mode", "Value": value}]
        return [
            {
                "error_msg": error,
                "cmd_results": [{"error_msg": "", "table_data": rows}] if not error else [],
            }
        ]

    @patch("backend.flow.utils.mysql.dts.migrate_helper.DRSApi.rpc")
    def test_gtid_on(self, mock_rpc):
        from backend.flow.utils.mysql.dts.migrate_helper import probe_instance_gtid_enabled

        mock_rpc.return_value = self._rpc_resp("ON")
        self.assertTrue(probe_instance_gtid_enabled(host="127.0.0.1", port=3306, bk_cloud_id=0))

    @patch("backend.flow.utils.mysql.dts.migrate_helper.DRSApi.rpc")
    def test_gtid_off(self, mock_rpc):
        from backend.flow.utils.mysql.dts.migrate_helper import probe_instance_gtid_enabled

        mock_rpc.return_value = self._rpc_resp("OFF")
        self.assertFalse(probe_instance_gtid_enabled(host="127.0.0.1", port=3306, bk_cloud_id=0))

    @patch("backend.flow.utils.mysql.dts.migrate_helper.DRSApi.rpc")
    def test_gtid_variable_missing(self, mock_rpc):
        from backend.flow.utils.mysql.dts.migrate_helper import probe_instance_gtid_enabled

        mock_rpc.return_value = self._rpc_resp(None)
        self.assertFalse(probe_instance_gtid_enabled(host="127.0.0.1", port=3306, bk_cloud_id=0))

    @patch("backend.flow.utils.mysql.dts.migrate_helper.DRSApi.rpc")
    def test_gtid_probe_exception_defaults_false(self, mock_rpc):
        from backend.flow.utils.mysql.dts.migrate_helper import probe_instance_gtid_enabled

        mock_rpc.side_effect = RuntimeError("drs down")
        self.assertFalse(probe_instance_gtid_enabled(host="127.0.0.1", port=3306, bk_cloud_id=0))

    @patch("backend.flow.utils.mysql.dts.migrate_helper.probe_instance_gtid_enabled")
    def test_decide_requires_source_and_target_both_on(self, mock_probe):
        from types import SimpleNamespace

        from backend.flow.utils.mysql.dts.migrate_helper import decide_enable_gtid

        # source ON, first target ON, second target OFF → False
        mock_probe.side_effect = [True, True, False]
        source_cluster = SimpleNamespace(id=1, bk_cloud_id=0)
        target_cluster = SimpleNamespace(id=2, bk_cloud_id=0, cluster_type="tendbha")
        master = SimpleNamespace(machine=SimpleNamespace(ip="127.0.0.2"), port=3306)
        target_cluster.storageinstance_set = SimpleNamespace(
            filter=lambda **kwargs: SimpleNamespace(first=lambda: master)
        )

        with patch(
            "backend.flow.utils.mysql.dts.migrate_helper._collect_target_gtid_probe_endpoints",
            return_value=[("127.0.0.2", 3306, 0), ("127.0.0.3", 3306, 0)],
        ):
            self.assertFalse(
                decide_enable_gtid(
                    source_host="127.0.0.1",
                    source_port=3306,
                    source_cluster=source_cluster,
                    target_cluster=target_cluster,
                    migrate_type="ha_to_ha",
                )
            )

    @patch("backend.flow.utils.mysql.dts.migrate_helper.probe_instance_gtid_enabled")
    def test_decide_both_on(self, mock_probe):
        from types import SimpleNamespace

        from backend.flow.utils.mysql.dts.migrate_helper import decide_enable_gtid

        mock_probe.return_value = True
        source_cluster = SimpleNamespace(id=1, bk_cloud_id=0)
        target_cluster = SimpleNamespace(id=2, bk_cloud_id=0)
        with patch(
            "backend.flow.utils.mysql.dts.migrate_helper._collect_target_gtid_probe_endpoints",
            return_value=[("127.0.0.2", 3306, 0)],
        ):
            self.assertTrue(
                decide_enable_gtid(
                    source_host="127.0.0.1",
                    source_port=3306,
                    source_cluster=source_cluster,
                    target_cluster=target_cluster,
                    migrate_type="ha_to_ha",
                )
            )

    @patch("backend.flow.utils.mysql.dts.migrate_helper.probe_instance_gtid_enabled")
    def test_decide_no_target_cluster_false(self, mock_probe):
        from types import SimpleNamespace

        from backend.flow.utils.mysql.dts.migrate_helper import decide_enable_gtid

        mock_probe.return_value = True
        source_cluster = SimpleNamespace(id=1, bk_cloud_id=0)
        self.assertFalse(
            decide_enable_gtid(
                source_host="127.0.0.1",
                source_port=3306,
                source_cluster=source_cluster,
                target_cluster=None,
                migrate_type="ha_to_ha",
            )
        )


class MigrateCredentialsTest(SimpleTestCase):
    def test_generate_dts_migrate_credentials(self):
        user, password = generate_dts_migrate_credentials()
        self.assertTrue(user.startswith(MYSQL_DTS_MIGRATE_USER_PREFIX))
        self.assertGreaterEqual(len(password), 16)

    def test_verify_max_retries_defined(self):
        self.assertGreaterEqual(MYSQL_DTS_VERIFY_MAX_RETRIES, 1)


class DeployHelperTest(SimpleTestCase):
    def test_group_deploy_hosts_colocated(self):
        master_hosts = [DtsHostSpec(ip="127.0.0.1", bk_cloud_id=0)]
        worker_hosts = [DtsHostSpec(ip="127.0.0.1", bk_cloud_id=0)]
        plan = group_deploy_hosts(master_hosts, worker_hosts)
        self.assertEqual(len(plan.colocated_hosts), 1)
        self.assertEqual(len(plan.master_only_hosts), 0)
        self.assertEqual(len(plan.worker_only_hosts), 0)

    def test_render_master_config(self):
        content = render_master_config(
            deploy_path="/data/dts/test/",
            node_name=build_master_node_name(1),
            advertise_ip="127.0.0.1",
        )
        self.assertIn("dm-master-1", content)
        self.assertIn("/data/dts/test/", content)
        self.assertIn("master-addr", content)
        self.assertIn("peer-urls", content)
        self.assertIn("log-file", content)
        self.assertNotIn("[log]", content)
        self.assertNotIn("[security]", content)
