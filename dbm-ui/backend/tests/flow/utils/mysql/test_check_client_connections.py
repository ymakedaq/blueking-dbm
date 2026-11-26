# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from backend.flow.engine.bamboo.scene.mysql.common.common_sub_flow import (
    check_long_active_process_sub_flow,
    check_sub_flow,
)
from backend.flow.utils.mysql.check_client_connections import exclude_dbha_sessions
from backend.flow.utils.mysql.get_mysql_sys_user import get_dbha_probe_user, get_mysql_sys_users

_ACCOUNT_ENV = "backend.flow.utils.mysql.act_payload.mixed.account_mixed.mysql_account_mixed.env"
_ACCOUNT_EXTENSION = (
    "backend.flow.utils.mysql.act_payload.mixed.account_mixed.mysql_account_mixed.DBExtension.get_latest_extension"
)
_SYS_USER_ENV = "backend.flow.utils.mysql.get_mysql_sys_user.env"
_SYS_USER_EXTENSION = "backend.flow.utils.mysql.get_mysql_sys_user.DBExtension.get_latest_extension"


class DbhaProbeUserTestCase(SimpleTestCase):
    @patch(_ACCOUNT_EXTENSION)
    @patch(_ACCOUNT_ENV)
    def test_env_username_skips_extension(self, account_env, get_latest_extension):
        account_env.DBHA_USERNAME = "probeuser"
        account_env.DBHA_PASSWORD = "secret"
        account_env.TEST_ACCESS_HOSTS = ["127.0.0.1"]

        self.assertEqual(get_dbha_probe_user(0), "probeuser")
        get_latest_extension.assert_not_called()

    @patch(_ACCOUNT_EXTENSION, side_effect=AttributeError("no extension"))
    @patch(_ACCOUNT_ENV)
    def test_missing_extension_returns_empty(self, account_env, get_latest_extension):
        account_env.DBHA_USERNAME = ""

        self.assertEqual(get_dbha_probe_user(0), "")
        get_latest_extension.assert_called_once()


class GetMysqlSysUsersTestCase(SimpleTestCase):
    @patch(_SYS_USER_EXTENSION)
    @patch(_SYS_USER_ENV)
    def test_passed_user_skips_dbha_extension(self, sys_env, get_latest_extension):
        sys_env.DRS_USERNAME = "drsuser"
        sys_env.DBHA_USERNAME = "trapuser"

        users = get_mysql_sys_users(0, dbha_user="probeuser")

        self.assertIn("probeuser", users)
        self.assertIn("drsuser", users)
        self.assertNotIn("trapuser", users)
        get_latest_extension.assert_not_called()

    @patch(_SYS_USER_EXTENSION)
    @patch(_SYS_USER_ENV)
    def test_unspecified_user_keeps_env_dbha(self, sys_env, get_latest_extension):
        sys_env.DRS_USERNAME = "drsuser"
        sys_env.DBHA_USERNAME = "envdbha"

        users = get_mysql_sys_users(0)

        self.assertIn("envdbha", users)
        get_latest_extension.assert_not_called()

    @patch(_SYS_USER_EXTENSION)
    @patch(_SYS_USER_ENV)
    def test_escapes_quote_in_passed_user(self, sys_env, get_latest_extension):
        sys_env.DRS_USERNAME = "drsuser"
        sys_env.DBHA_USERNAME = "trapuser"

        users = get_mysql_sys_users(0, dbha_user="a'b")

        self.assertIn("a''b", users)
        get_latest_extension.assert_not_called()


class ExcludeDbhaSessionsTestCase(SimpleTestCase):
    def test_drops_probe_user_only(self):
        infos = [{"User": "probeuser", "Id": 1}, {"User": "biz", "Id": 2}]
        self.assertEqual(exclude_dbha_sessions(infos, "probeuser"), [{"User": "biz", "Id": 2}])

    def test_blank_user_keeps_rows(self):
        infos = [{"User": "biz", "Id": 2}]
        self.assertEqual(exclude_dbha_sessions(infos, "  "), infos)


class CheckSubFlowProbeUserTestCase(SimpleTestCase):
    @patch("backend.flow.engine.bamboo.scene.common.builder.SubBuilder.build_sub_process", return_value=None)
    @patch("backend.flow.engine.bamboo.scene.common.builder.SubBuilder.add_parallel_acts")
    @patch(
        "backend.flow.engine.bamboo.scene.mysql.common.common_sub_flow.get_dbha_probe_user",
        return_value="probeuser",
    )
    def test_resolves_once_for_multiple_instances(
        self, get_dbha_probe_user_mock, add_parallel_acts, build_sub_process
    ):
        check_sub_flow(
            uid="1",
            root_id="root",
            cluster=SimpleNamespace(bk_cloud_id=0, name="c"),
            is_check_client_conn=True,
            check_client_conn_inst=["127.0.0.1:20000", "127.0.0.2:20000"],
        )
        get_dbha_probe_user_mock.assert_called_once_with(0)
        acts = add_parallel_acts.call_args.kwargs["acts_list"]
        self.assertEqual([act["kwargs"]["dbha_user"] for act in acts], ["probeuser", "probeuser"])
        build_sub_process.assert_called_once()

    @patch("backend.flow.engine.bamboo.scene.common.builder.SubBuilder.build_sub_process", return_value=None)
    @patch("backend.flow.engine.bamboo.scene.common.builder.SubBuilder.add_parallel_acts")
    @patch(
        "backend.flow.engine.bamboo.scene.mysql.common.common_sub_flow.get_dbha_probe_user",
        return_value="probeuser",
    )
    def test_long_process_resolves_once(self, get_dbha_probe_user_mock, add_parallel_acts, build_sub_process):
        check_long_active_process_sub_flow(
            uid="1",
            root_id="root",
            cluster=SimpleNamespace(bk_cloud_id=0, name="c"),
            node_insts=["127.0.0.1:20000", "127.0.0.2:20000"],
        )
        get_dbha_probe_user_mock.assert_called_once_with(0)
        acts = add_parallel_acts.call_args.kwargs["acts_list"]
        dbha_users = [act["kwargs"]["dbha_user"] for act in acts if "dbha_user" in act["kwargs"]]
        self.assertEqual(dbha_users, ["probeuser", "probeuser"])
        build_sub_process.assert_called_once()
