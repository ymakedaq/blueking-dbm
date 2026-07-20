# -*- coding: utf-8 -*-
from django.test import SimpleTestCase

from backend.flow.utils.mysql.dts.constants import DtsLifecycleMode, FullLoadEngine, MigrateTopology
from backend.flow.utils.mysql.dts.migrate_plan import normalize_migrate_ticket_details
from backend.ticket.builders.mysql.dts.mysql_dts_tickets import MysqlMigrateBaseDetailSerializer


def _minimal_layered_details(**overrides):
    data = {
        "dts_resource": {
            "mode": DtsLifecycleMode.USE_EXISTING.value,
            "cluster_id": 1,
        },
        "migrate": {
            "topology": MigrateTopology.ONE_TO_ONE.value,
            "one_to_one": {
                "task_name": "ha2ha-1",
                "source": {"cluster_id": 100, "sync_scope": {"do_dbs": ["db_a"]}},
                "target": {"cluster_id": 200},
            },
        },
        "task": {
            "task_mode": "all",
            "full_load": {"engine": FullLoadEngine.BUILTIN.value},
        },
    }
    data.update(overrides)
    return data


class MysqlDtsTicketSerializerTest(SimpleTestCase):
    def test_migrate_serializer_builds_plan(self):
        slz = MysqlMigrateBaseDetailSerializer(data=_minimal_layered_details())
        self.assertTrue(slz.is_valid(), slz.errors)
        self.assertIn("migrate_plan", slz.validated_data)
        plan = slz.validated_data["migrate_plan"]
        self.assertEqual(plan.dts_cluster_id, 1)
        self.assertEqual(plan.task_specs[0].sources[0].cluster_id, 100)
        self.assertEqual(plan.task_specs[0].target_cluster_id, 200)

    def test_migrate_serializer_requires_topology_block(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data={
                "dts_resource": {"mode": DtsLifecycleMode.USE_EXISTING.value, "cluster_id": 1},
                "migrate": {"topology": MigrateTopology.ONE_TO_ONE.value},
            }
        )
        self.assertFalse(slz.is_valid())
        self.assertIn("one_to_one", str(slz.errors))

    def test_deploy_ephemeral_requires_deploy(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data=_minimal_layered_details(
                dts_resource={"mode": DtsLifecycleMode.DEPLOY_EPHEMERAL.value},
            )
        )
        self.assertFalse(slz.is_valid())
        self.assertIn("deploy", str(slz.errors))

    def test_use_existing_requires_cluster_id(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data=_minimal_layered_details(dts_resource={"mode": DtsLifecycleMode.USE_EXISTING.value})
        )
        self.assertFalse(slz.is_valid())
        self.assertIn("cluster_id", str(slz.errors))

    def test_myloader_full_load_maps_to_plan(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data=_minimal_layered_details(
                task={
                    "task_mode": "all",
                    "full_load": {
                        "engine": FullLoadEngine.MYLOADER.value,
                        "myloader": {"threads": 12, "dest_worker_ip": "127.0.0.2"},
                    },
                }
            )
        )
        self.assertTrue(slz.is_valid(), slz.errors)
        plan = slz.validated_data["migrate_plan"]
        self.assertEqual(plan.dts_task_config.full_load_engine, FullLoadEngine.MYLOADER.value)
        self.assertEqual(plan.task_specs[0].sources[0].myloader.threads, 12)


class NormalizeMigrateTicketDetailsTest(SimpleTestCase):
    def test_normalize_use_existing(self):
        flat = normalize_migrate_ticket_details(_minimal_layered_details())
        self.assertEqual(flat["migrate_topology"], MigrateTopology.ONE_TO_ONE.value)
        self.assertEqual(flat["dts_cluster_id"], 1)
        self.assertFalse(flat["auto_deploy_dts"])
        self.assertEqual(flat["dts_lifecycle"], DtsLifecycleMode.USE_EXISTING.value)
        self.assertEqual(flat["one_to_one"]["src_info"]["cluster_id"], 100)
        self.assertEqual(flat["one_to_one"]["dst_info"]["cluster_id"], 200)
        self.assertEqual(flat["dts_task_config"]["full_load_engine"], FullLoadEngine.BUILTIN.value)

    def test_normalize_deploy_ephemeral(self):
        details = _minimal_layered_details(
            dts_resource={
                "mode": DtsLifecycleMode.DEPLOY_EPHEMERAL.value,
                "deploy": {
                    "cluster_name": "dts-test",
                    "bk_cloud_id": 0,
                    "master_hosts": [{"ip": "127.0.0.2", "bk_cloud_id": 0}],
                    "worker_hosts": [{"ip": "127.0.0.3", "bk_cloud_id": 0}],
                },
            }
        )
        flat = normalize_migrate_ticket_details(details)
        self.assertTrue(flat["auto_deploy_dts"])
        self.assertTrue(flat["cleanup_after_migrate"])
        self.assertIn("deploy_subflow", flat)
        self.assertEqual(flat["deploy_subflow"]["cluster_name"], "dts-test")

    def test_normalize_many_to_one_sources(self):
        details = {
            "dts_resource": {"mode": DtsLifecycleMode.USE_EXISTING.value, "cluster_id": 9},
            "migrate": {
                "topology": MigrateTopology.MANY_TO_ONE.value,
                "many_to_one": {
                    "sources": [{"cluster_id": 1}, {"cluster_id": 2}],
                    "target": {"cluster_id": 10},
                },
            },
            "task": {"full_load": {"engine": "builtin"}},
        }
        flat = normalize_migrate_ticket_details(details)
        self.assertEqual(len(flat["many_to_one"]["src_infos"]), 2)
        self.assertEqual(flat["many_to_one"]["dst_info"]["cluster_id"], 10)
