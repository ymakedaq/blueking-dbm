# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and limitations under the License.
"""

from django.utils.translation import gettext_lazy as _

from ..base import BaseApi
from ..domains import HCM_APIGW_DOMAIN


class _HCMResourceApi(BaseApi):
    MODULE = _("HCM海垒 资源服务")
    BASE = HCM_APIGW_DOMAIN

    def __init__(self):
        self.create_apply = self.generate_data_api(
            method="POST",
            url="/api/v1/task/create_apply",
            description=_("创建资源申请单据"),
        )
        self.get_apply_ticket = self.generate_data_api(
            method="POST",
            url="/api/v1/task/get_apply_ticket",
            description=_("获取资源申请单据详情"),
        )
        self.get_apply_device = self.generate_data_api(
            method="POST",
            url="/api/v1/task/get_apply_device",
            description=_("获取资源申请单据的设备信息"),
        )
        self.get_apply_status = self.generate_data_api(
            method="POST",
            url="/api/v1/task/get_apply_status",
            description=_("获取资源申请单据状态"),
        )

    def create_apply_order(self, apply_data: dict):
        """
        创建申请单据的基础方法。

        :param apply_data: dict，申请单据的详细数据

        请求参数：
        - apply_data: dict，申请单据数据，包含以下字段：
            - bk_biz_id (int): CC业务ID，必选
            - bk_username (string): 资源申请提单人，必选
            - follower (string array, 可选): 关注人列表，如果有多人，如：["name1","name2"]
            - enable_notice (bool, 可选): 是否通知用户单据完成，默认为false
            - require_type (int): 需求类型，必选。1: 常规项目; 2: 春节保障; 3: 机房裁撤; 6: 滚服项目; 7: 小额绿通
            - expect_time (string): 期望交付时间，必选
            - remark (string, 可选): 备注
            - suborders (object array): 资源申请子需求单信息，必选，包含以下字段：
                - resource_type (string): 需求资源类型，必选。"QCLOUDCVM": 腾讯云虚拟机, "IDCPM": IDC物理机
                - replicas (int): 需求资源数量，必选
                - anti_affinity_level (string, 可选): 反亲和策略，默认值为"ANTI_NONE"。"ANTI_NONE": 无要求, "ANTI_CAMPUS": 分Campus, "ANTI_MODULE": 分Module, "ANTI_RACK": 分机架
                - remark (string, 可选): 备注
                - spec (object): 资源需求声明，必选

                    spec for QCLOUDCVM:
                    - region (string): 地域，必选
                    - zone (string): 可用区，必选
                    - device_type (string): 机型，必选
                    - image_id (string): 镜像ID，必选
                    - disk_size (int): 数据盘磁盘大小，单位G，必选
                    - disk_type (string, 可选): 数据盘磁盘类型，默认值是"CLOUD_PREMIUM"。"CLOUD_SSD": SSD云硬盘, "CLOUD_PREMIUM": 高性能云盘
                    - vpc (string, 可选): 私有网络，默认为空
                    - subnet (string, 可选): 私有子网，默认为空
                    - charge_type (string, 可选): 计费模式 (PREPAID:包年包月，POSTPAID_BY_HOUR:按量计费)，默认:包年包月
                    - charge_months (int, 可选): 计费时长，单位：月(计费模式为包年包月时，该字段必传)
                    - inherit_instance_id (string, 可选): 被继承云主机实例ID（同一批次只支持一台），如果是滚服项目，该字段必传

                    spec for IDCPM:
                    - region (string): 地域，必选
                    - zone (string): 可用区，必选
                    - device_type (string): 机型，必选
                    - os_type (string): 操作系统，必选
                    - raid_type (string): RAID类型，必选
                    - network_type (string): 网络类型，必选。"ONETHOUSAND": 千兆, "TENTHOUSAND": 万兆
                    - isp (string, 可选): 外网运营商

        返回参数：
        - response: dict，包含服务器的响应数据，示例：
            {
                "result": true,
                "code": 0,
                "message": "success",
                "data": {
                    "order_id": 123456
                }
            }

        响应参数说明：
        - result (bool): 请求成功与否。true:请求成功；false请求失败
        - code (int): 错误编码。0表示success，>0表示失败错误
        - message (string): 请求失败返回的错误信息
        - data (object): 请求返回的数据
            - order_id (int): 单据ID

        :return: dict，服务器的响应数据
        """  # noqa: E501
        if not HCM_APIGW_DOMAIN:
            return {"status": "error", "message": "HCM API domain not configured"}

        try:
            resp = self.create_apply(params=apply_data)
            return resp
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_apply_ticket_order(self, order_id: int):
        """
        获取申请单据详情的基础方法。

        :param order_id: int，资源申请单据ID

        请求参数：
        - order_id (int): 资源申请单据ID，必选

        返回参数：
        - response: dict，包含服务器的响应数据，示例：
            {
                "result": true,
                "code": 0,
                "message": "success",
                "data": {
                    "order_id": 123456,
                    "itsm_ticket_id": "ITSM******",
                    "stage": "RUNNING",
                    "bk_biz_id": 20********,
                    "bk_username": "****",
                    "follower": ["user1", "user2"],
                    "enable_notice": false,
                    "require_type": 1,
                    "expect_time": "2025-01-15",
                    "remark": "申请备注",
                    "suborders": [
                        {
                            "resource_type": "QCLOUDCVM",
                            "replicas": 2,
                            "anti_affinity_level": "ANTI_NONE",
                            "remark": "子需求备注",
                            "spec": {
                                "region": "ap-guangzhou",
                                "zone": "ap-guangzhou-3",
                                "device_group": "标准型",
                                "device_type": "SA3.2XLARGE32",
                                "image_id": "img-******",
                                "image": "******* Server *.*",
                                "disk_size": 100,
                                "disk_type": "CLOUD_PREMIUM",
                                "network_type": "TENTHOUSAND",
                                "vpc": "vpc-******",
                                "subnet": "subnet-******",
                                "os_type": "Linux",
                                "raid_type": "RAID1",
                                "isp": "BGP",
                                "mount_path": "/data",
                                "cpu_provider": "Intel",
                                "kernel": "5.4.119"
                            }
                        }
                    ]
                }
            }
        """
        if not HCM_APIGW_DOMAIN:
            return {"status": "error", "message": "HCM API domain not configured"}

        try:
            resp = self.get_apply_ticket(params={"order_id": order_id})
            return resp
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_apply_device_info(self, order_id: int, suborder_id: str = None):
        """
        资源申请已交付机器列表查询。

        :param order_id: int，资源申请单号
        :param suborder_id: str，资源申请子单号，可选

        请求参数：
        - order_id (int): 资源申请单号，必选
        - suborder_id (string, 可选): 资源申请子单号。若为空，则返回申请单号关联的所有已交付机器列表；若非空，则只返回申请子单号关联的已交付机器列表

        调用示例：
        {
            "order_id": 1001,
            "suborder_id": "1001-1"
        }

        返回参数：
        - response: dict，包含服务器的响应数据，示例：
            {
                "result": true,
                "code": 0,
                "message": "",
                "data": {
                    "info": [
                        {
                            "ip": "10.*.*.***",
                            "asset_id": "TC**************"
                        }
                    ]
                }
            }

        响应参数说明：
        - result (bool): 请求成功与否。true:请求成功；false请求失败
        - code (int): 错误编码。0表示success，>0表示失败错误
        - message (string): 请求失败返回的错误信息
        - data (object): 请求返回的数据
            - info (object): 已交付的机器列表
                - ip (string): 资源申请已交付的机器ip
                - asset_id (string): 资源申请已交付的机器固资号

        :return: dict，服务器的响应数据
        """
        if not HCM_APIGW_DOMAIN:
            return {"status": "error", "message": "HCM API domain not configured"}

        # 构建请求参数
        params = {"order_id": order_id}
        if suborder_id is not None:
            params["suborder_id"] = suborder_id

        try:
            resp = self.get_apply_device(params=params)
            return resp
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_apply_status_info(self, order_id: int):
        """
        获取申请单据状态。

        :param order_id: int，资源申请单据ID

        请求参数：
        - order_id (int): 资源申请单据ID，必选

        返回参数：
        - response: dict，包含服务器的响应数据，示例：
            {
                "result": true,
                "code": 0,
                "message": "",
                "data": {
                    "info": [
                        {
                            "order_id": 123456,
                            "suborder_id": "123456-1",
                            "bk_biz_id": 20********,
                            "bk_username": "****",
                            "require_type": 1,
                            "resource_type": "QCLOUDCVM",
                            "expect_time": "2025-01-15",
                            "spec": {
                                "region": "ap-guangzhou",
                                "zone": "ap-guangzhou-3",
                                "device_group": "标准型",
                                "device_type": "SA3.2XLARGE32",
                                "image_id": "img-******",
                                "image": "******* Server *.*",
                                "disk_size": 100,
                                "disk_type": "CLOUD_PREMIUM",
                                "network_type": "TENTHOUSAND",
                                "vpc": "vpc-******",
                                "subnet": "subnet-******",
                                "os_type": "Linux",
                                "raid_type": "RAID1",
                                "isp": "BGP",
                                "mount_path": "/data",
                                "cpu_provider": "Intel",
                                "kernel": "5.4.119"
                            },
                            "anti_affinity_level": "ANTI_NONE",
                            "stage": "RUNNING",
                            "status": "MATCHING",
                            "total_num": 2,
                            "success_num": 1,
                            "pending_num": 1,
                            "create_at": 17********,
                            "update_at": 1704873300
                        }
                    ]
                }
            }
        :return: dict，服务器的响应数据
        """
        if not HCM_APIGW_DOMAIN:
            return {"status": "error", "message": "HCM API domain not configured"}

        try:
            resp = self.get_apply_status(params={"order_id": order_id})
            return resp
        except Exception as e:
            return {"status": "error", "message": str(e)}


HCMApi = _HCMResourceApi()
