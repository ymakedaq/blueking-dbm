# -*- coding: utf-8 -*-
"""
接入层灾难重建单据（TENDBCLUSTER_SPIDER_LAYER_DR）主机回收联动单测：
- 单据注册为回收类单据，且开启 recycle_hosts 补充；
- spider_master / spider_slave 两种重建对象的旧机器都会写入 recycle_hosts；
- 单据成功后触发 create_recycle_ticket(RECYCLE_OLD_HOST)；
- 资源申请 post_callback 按角色把 old_nodes.proxy 映射到对应的旧 IP 列表字段。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from backend.ticket.builders import BuilderFactory
from backend.ticket.builders.common.base import fetch_recycle_hosts
from backend.ticket.builders.tendbcluster.tendb_spider_layer_disaster_recover import (
    SpiderLayerDisasterRecoverFlowBuilder,
    TendbClusterSpiderLayerDrResourceParamBuilder,
)
from backend.ticket.constants import TicketStatus, TicketType

OLD_HOSTS = [
    {"bk_cloud_id": 0, "bk_host_id": 2001, "ip": "127.0.0.21"},
    {"bk_cloud_id": 0, "bk_host_id": 2002, "ip": "127.0.0.22"},
]


def _dr_details(role_key: str) -> dict:
    """模拟前端提交的单据详情：旧机器统一放 old_nodes.proxy，新机按角色放 resource_spec"""
    return {
        "ip_source": "resource_pool",
        "infos": [
            {
                "cluster_id": 1,
                "old_nodes": {"proxy": [dict(h) for h in OLD_HOSTS]},
                "resource_spec": {role_key: {"count": 2, "spec_id": 1, "labels": [], "label_names": []}},
                "strip_dns_before_install": True,
            }
        ],
    }


class SpiderLayerDrRecycleRegisterTest(SimpleTestCase):
    def test_registered_as_recycle_ticket(self):
        self.assertIn(TicketType.TENDBCLUSTER_SPIDER_LAYER_DR, BuilderFactory.recycle_ticket_type)

    def test_need_patch_recycle_host_details_enabled(self):
        self.assertTrue(SpiderLayerDisasterRecoverFlowBuilder.need_patch_recycle_host_details)


class SpiderLayerDrRecycleHostsTest(SimpleTestCase):
    def test_fetch_recycle_hosts_for_master_and_slave(self):
        for role_key in ("spider_master_new_ip_list", "spider_slave_new_ip_list"):
            with self.subTest(role_key=role_key):
                hosts = fetch_recycle_hosts(_dr_details(role_key))
                self.assertEqual(sorted(h["bk_host_id"] for h in hosts), [2001, 2002])

    @patch("backend.ticket.builders.common.base.ResourceHandler.standardized_resource_host")
    def test_patch_recycle_host_details_writes_recycle_hosts(self, mock_standardize):
        mock_standardize.side_effect = lambda hosts: [{**h, "os_name": "linux", "os_type": "1"} for h in hosts]
        for role_key in ("spider_master_new_ip_list", "spider_slave_new_ip_list"):
            with self.subTest(role_key=role_key):
                ticket = SimpleNamespace(id=30001, details=_dr_details(role_key))
                builder = SpiderLayerDisasterRecoverFlowBuilder.__new__(SpiderLayerDisasterRecoverFlowBuilder)
                builder.ticket = ticket

                builder.patch_recycle_host_details()

                recycle_hosts = ticket.details["recycle_hosts"]
                self.assertEqual(sorted(h["bk_host_id"] for h in recycle_hosts), [2001, 2002])

    @patch("backend.ticket.flow_manager.manager.create_recycle_ticket.apply_async")
    @patch("backend.ticket.flow_manager.manager.notify.send_msg.apply_async")
    @patch("backend.ticket.flow_manager.manager.add_ticket_audit_event.apply_async")
    def test_succeeded_triggers_create_recycle_ticket(self, _mock_audit, _mock_notify, mock_apply_async):
        from backend.ticket.flow_manager.manager import TicketFlowManager

        ticket = SimpleNamespace(
            id=30001,
            ticket_type=TicketType.TENDBCLUSTER_SPIDER_LAYER_DR.value,
            details={**_dr_details("spider_slave_new_ip_list"), "recycle_hosts": OLD_HOSTS},
            config={},
        )
        manager = TicketFlowManager.__new__(TicketFlowManager)
        manager.ticket = ticket
        manager.ticket_status_trigger(TicketStatus.RUNNING, TicketStatus.SUCCEEDED)

        mock_apply_async.assert_called_once_with(args=(ticket.id, OLD_HOSTS, TicketType.RECYCLE_OLD_HOST))


class SpiderLayerDrPostCallbackTest(SimpleTestCase):
    def _run_post_callback(self, info: dict) -> dict:
        next_flow = SimpleNamespace(details={"ticket_data": {"infos": [info]}}, save=MagicMock())
        ticket = SimpleNamespace(details={}, next_flow=MagicMock(return_value=next_flow))
        builder = TendbClusterSpiderLayerDrResourceParamBuilder.__new__(TendbClusterSpiderLayerDrResourceParamBuilder)
        builder.ticket = ticket

        builder.post_callback()

        next_flow.save.assert_called_once_with(update_fields=["details"])
        return next_flow.details["ticket_data"]["infos"][0]

    def test_master_role_maps_to_master_old_list(self):
        info = self._run_post_callback(_dr_details("spider_master_new_ip_list")["infos"][0])
        self.assertNotIn("old_nodes", info)
        self.assertEqual(info["spider_master_old_ip_list"], OLD_HOSTS)
        self.assertNotIn("spider_slave_old_ip_list", info)

    def test_slave_role_maps_to_slave_old_list(self):
        info = self._run_post_callback(_dr_details("spider_slave_new_ip_list")["infos"][0])
        self.assertNotIn("old_nodes", info)
        self.assertEqual(info["spider_slave_old_ip_list"], OLD_HOSTS)
        self.assertNotIn("spider_master_old_ip_list", info)

    def test_slave_role_detected_from_applied_hosts(self):
        """资源申请回填后 info 直接带 spider_slave_new_ip_list 也能识别为 slave"""
        info = {
            "cluster_id": 1,
            "old_nodes": {"proxy": [dict(h) for h in OLD_HOSTS]},
            "spider_slave_new_ip_list": [{"bk_host_id": 3001, "ip": "127.0.0.31"}],
        }
        info = self._run_post_callback(info)
        self.assertEqual(info["spider_slave_old_ip_list"], OLD_HOSTS)
