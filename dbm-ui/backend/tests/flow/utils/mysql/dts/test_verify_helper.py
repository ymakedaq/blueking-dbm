# -*- coding: utf-8 -*-
"""verify_helper：OpenAPI addr 解析与节点匹配单测。"""
import os
import sys
import unittest
from types import SimpleNamespace

_DBM_UI = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../../"))
if _DBM_UI not in sys.path:
    sys.path.insert(0, _DBM_UI)

from django.conf import settings  # noqa: E402

if not settings.configured:
    # 仅满足 gettext 调用，不拉起完整 Django apps
    settings.configure(USE_I18N=False, SECRET_KEY="test-verify-helper")

from backend.flow.utils.mysql.dts.verify_helper import extract_ip_from_addr, match_nodes  # noqa: E402


class ExtractIpFromAddrTest(unittest.TestCase):
    def test_http_peer_url(self):
        self.assertEqual(extract_ip_from_addr("http://127.0.0.2:18401"), "127.0.0.2")

    def test_host_port_without_scheme(self):
        self.assertEqual(extract_ip_from_addr("127.0.0.3:18301"), "127.0.0.3")

    def test_bare_ip(self):
        self.assertEqual(extract_ip_from_addr("127.0.0.4"), "127.0.0.4")

    def test_empty(self):
        self.assertEqual(extract_ip_from_addr(""), "")


class MatchNodesTest(unittest.TestCase):
    def test_match_http_addr_against_plain_ip(self):
        api_items = [SimpleNamespace(name="dts-master-1", addr="http://127.0.0.2:18401", alive=True)]
        expected = [{"ip": "127.0.0.2", "bk_cloud_id": 0}]
        match_nodes(api_items, expected, "Master")

    def test_missing_raises_detailed_error(self):
        api_items = [SimpleNamespace(name="dts-master-1", addr="http://127.0.0.2:18401", alive=True)]
        expected = [{"ip": "127.0.0.9", "bk_cloud_id": 0}]
        with self.assertRaises(ValueError) as ctx:
            match_nodes(api_items, expected, "Master")
        msg = str(ctx.exception)
        self.assertIn("127.0.0.9", msg)
        self.assertIn("127.0.0.2", msg)
        self.assertIn("dts-master-1@", msg)


if __name__ == "__main__":
    unittest.main()
