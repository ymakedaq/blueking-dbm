# -*- coding: utf-8 -*-
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
