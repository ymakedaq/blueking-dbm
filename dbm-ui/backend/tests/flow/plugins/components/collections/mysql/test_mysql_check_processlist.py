# -*- coding: utf-8 -*-
from django.test import SimpleTestCase

from backend.flow.plugins.components.collections.mysql.mysql_check_processlist import build_check_processlist_sql

_BASE_USERS = (
    "event_scheduler",
    "system user",
    "yw",
    "MONITOR",
    "ADMIN",
    "repl",
)


class BuildCheckProcesslistSqlTestCase(SimpleTestCase):
    def test_includes_probe_user(self):
        sql = build_check_processlist_sql("probeuser")
        for user in _BASE_USERS:
            self.assertIn("'{}'".format(user), sql)
        self.assertIn("'probeuser'", sql)
        self.assertIn("User!=SUBSTRING_INDEX(current_user(),'@',1)", sql)

    def test_blank_user_does_not_add_empty_quotes(self):
        blank_sql = build_check_processlist_sql("")
        space_sql = build_check_processlist_sql("   ")
        self.assertEqual(blank_sql, space_sql)
        self.assertNotIn("''", blank_sql)
        for user in _BASE_USERS:
            self.assertIn("'{}'".format(user), blank_sql)

    def test_escapes_single_quote(self):
        sql = build_check_processlist_sql("a'b")
        self.assertIn("'a''b'", sql)
