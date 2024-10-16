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
import glob
import json
import logging
import os

from django.core.management.base import BaseCommand

from backend.db_monitor.constants import TPLS_ALARM_DIR, TargetPriority

logger = logging.getLogger("root")


class Command(BaseCommand):
    help = "策略模板文件修复"

    def add_arguments(self, parser):
        parser.add_argument(
            "-d",
            "--dbtype",
            choices=["mysql", "redis", "es", "hdfs", "kafka", "pulsar", "influxdb", "all"],
            default="all",
            type=str,
            help="db类型",
        )

    def clear_id(self, objs, id_name="id"):
        for obj in objs:
            obj.pop(id_name, None)

    def update_json_file(self, f, template_dict):
        f.seek(0)
        f.write(json.dumps(template_dict, indent=2))
        f.truncate()

    def update_json_file_name(self, json_file, old, new):
        if old == new:
            return

        print(f"rename json name: {old} -> {new}")
        os.rename(json_file, os.path.join(TPLS_ALARM_DIR, f"{new}.json"))

    def handle(self, *args, **options):
        # db_type = options["dbtype"]
        alarm_jsons = glob.glob(os.path.join(TPLS_ALARM_DIR, "*.json"))
        for alarm_json in alarm_jsons:
            need_update = False

            with open(alarm_json, "r+") as f:
                template_dict = json.loads(f.read())
                template_dict["custom_conditions"] = []

                # old_template_name = template_dict["name"]
                template_name = template_dict["name"]
                db_type = template_dict["db_type"]

                # 清理实例参数
                details = template_dict["details"]

                # 补充默认值
                if not template_dict.get("monitor_indicator"):
                    template_dict["monitor_indicator"] = details["items"][0]["name"]

                # if not template_dict.get("version"):
                #     template_dict["version"] = 0

                template_dict.pop("monitor_strategy_id", None)
                # details.pop("monitor_indicator", None)

                details["labels"] = sorted(set(details["labels"]))
                details["source"] = "dbm"
                details["bk_biz_id"] = ""
                details["priority"] = TargetPriority.PLATFORM.value
                # 平台策略仅开启基于分派通知
                details["notice"]["options"]["assign_mode"] = ["by_rule"]
                for item in details["items"]:
                    # 清空监控目标
                    item["target"] = []
                    item["origin_sql"] = ""

                    # 补充app_id作为维度
                    self.clear_id(item["query_configs"])
                    self.clear_id(item["algorithms"])
                    for query_config in item["query_configs"]:
                        metric_id = query_config["metric_id"]

                        # 标记告警数据来源
                        # template_dict["alert_source"] = query_config["data_type_label"]

                        # 增加版本
                        template_dict["version"] = 17

                        if "promql" in query_config:
                            promql = query_config["promql"]
                            if metric_id != promql:
                                query_config["metric_id"] = promql
                        else:
                            # 奇怪，监控新版阉割了name
                            metric_field = query_config.get("metric_field")
                            if metric_field and not metric_id.endswith(metric_field):
                                query_config["metric_id"] = ".".join(metric_id.split(".")[:-1] + [metric_field])
                                print(f"found bad metric_id rule: {template_name} -> {query_config['metric_id']}")

                        # 根据监控侧长度限制进行截断
                        if len(query_config["metric_id"]) > 128:
                            print(f"found too long promql metric rule: {template_name} -> {query_config['metric_id']}")
                            query_config["metric_id"] = ""

                        # if "agg_dimension" in query_config and "appid" not in query_config["agg_dimension"]:
                        #     query_config["agg_dimension"].append("appid")
                        #     query_config["agg_dimension"].pop("app_id", None)

                        if "agg_condition" in query_config and "dbm_system" in query_config["result_table_id"]:
                            agg_condition = query_config["agg_condition"]
                            has_db_type = len(list(filter(lambda x: x["key"] == "db_type", agg_condition))) > 0
                            if not has_db_type:
                                agg_condition.append(
                                    {
                                        "key": "db_type",
                                        "value": [db_type],
                                        "method": "eq",
                                        "condition": "and",
                                        "dimension_name": "db_type",
                                    }
                                )

                            need_update = True

                self.clear_id(details["items"])
                # self.update_json_file_name(alarm_json, old_template_name, template_name)
                if need_update:
                    self.update_json_file(f, template_dict)
