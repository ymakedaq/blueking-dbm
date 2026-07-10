# -*- coding: utf-8 -*-
from django.test import SimpleTestCase

from backend.flow.utils.mysql.dts.constants import MigrateTopology
from backend.ticket.builders.mysql.dts.mysql_dts_tickets import MysqlMigrateBaseDetailSerializer


class MysqlDtsTicketSerializerTest(SimpleTestCase):
    def test_migrate_serializer_builds_plan(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data={
                "migrate_topology": MigrateTopology.ONE_TO_ONE.value,
                "one_to_one": {
                    "src_info": {"cluster_id": 100},
                    "dst_info": {"cluster_id": 200},
                },
            }
        )
        self.assertTrue(slz.is_valid(), slz.errors)
        self.assertIn("migrate_plan", slz.validated_data)

    def test_migrate_serializer_requires_topology_field(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data={
                "migrate_topology": MigrateTopology.ONE_TO_ONE.value,
            }
        )
        self.assertFalse(slz.is_valid())
        self.assertIn("one_to_one", str(slz.errors))

    def test_auto_deploy_requires_deploy_subflow(self):
        slz = MysqlMigrateBaseDetailSerializer(
            data={
                "migrate_topology": MigrateTopology.ONE_TO_ONE.value,
                "auto_deploy_dts": True,
                "one_to_one": {
                    "src_info": {"cluster_id": 100},
                    "dst_info": {"cluster_id": 200},
                },
            }
        )
        self.assertFalse(slz.is_valid())
        self.assertIn("deploy_subflow", str(slz.errors))
