# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import patch

from backend.flow.engine.bamboo.scene.spider.spider_layer_disaster_recover_flow import (
    TenDBClusterSpiderLayerDisasterRecoverFlow,
)
from backend.flow.utils.mysql.common.random_job_with_ticket_map import random_job_with_ticket_map
from backend.flow.utils.spider.spider_disaster_recover import resolve_ctl_primary_ip, slave_only_recover_cluster_ids
from backend.ticket.constants import TicketType

FLOW_MODULE = "backend.flow.engine.bamboo.scene.spider.spider_layer_disaster_recover_flow"


def test_slave_only_connects_to_tdbctl_primary_host():
    cluster = SimpleNamespace(id=31, tendbcluster_ctl_primary_address=lambda: "127.0.0.2:26000")
    assert resolve_ctl_primary_ip(cluster) == "127.0.0.2"


def test_temp_account_clusters_are_slave_only():
    infos = [
        {"cluster_id": 11, "spider_slave_new_ip_list": [{"ip": "127.0.0.3"}]},
        {"cluster_id": 12, "spider_master_new_ip_list": [{"ip": "127.0.0.4"}]},
        {
            "cluster_id": 13,
            "spider_master_new_ip_list": [{"ip": "127.0.0.5"}],
            "spider_slave_new_ip_list": [{"ip": "127.0.0.6"}],
        },
        {"cluster_id": "11", "spider_slave_new_ip_list": [{"ip": "127.0.0.7"}]},
    ]
    assert slave_only_recover_cluster_ids(infos) == [11]


def test_slave_layer_dr_grants_temp_account_only_on_tdbctl_primary():
    rule = random_job_with_ticket_map[TicketType.TENDBCLUSTER_SPIDER_LAYER_DR]
    assert rule.is_only_tdbctl_primary_add is True


def test_slave_only_flow_asks_builder_to_create_temp_account():
    data = {
        "bk_biz_id": 1,
        "infos": [
            {"cluster_id": 11, "spider_slave_new_ip_list": [{"ip": "127.0.0.3"}]},
            {"cluster_id": 12, "spider_master_new_ip_list": [{"ip": "127.0.0.4"}]},
        ],
    }
    flow = TenDBClusterSpiderLayerDisasterRecoverFlow(root_id="job1", data=data)
    with (
        patch.object(flow, "_cluster_sub_flow", return_value="sub"),
        patch(f"{FLOW_MODULE}.Builder") as builder,
    ):
        builder.return_value.add_parallel_sub_pipeline.return_value = None
        builder.return_value.run_pipeline_with_sidecar.return_value = None
        flow.spider_layer_disaster_recover()
    assert builder.call_args.kwargs["need_random_pass_cluster_ids"] == [11]
