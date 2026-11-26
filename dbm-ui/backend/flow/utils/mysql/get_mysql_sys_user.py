"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import logging

from django.utils.translation import gettext as _

from backend import env
from backend.core.encrypt.constants import AsymmetricCipherConfigType
from backend.core.encrypt.handlers import AsymmetricHandler
from backend.db_proxy.constants import ExtensionType
from backend.db_proxy.models import DBExtension
from backend.flow.consts import DBM_MYSQL_JOB_TMP_USER_PREFIX, UserName
from backend.flow.utils.mysql.act_payload.mixed.account_mixed.mysql_account_mixed import MySQLAccountMixed

logger = logging.getLogger("flow")


def get_dbha_probe_user(bk_cloud_id: int) -> str:
    """
    返回云区域 DBHA 探测用户名。
    解析失败时返回空字符串，不把异常抛给检查节点。
    """
    try:
        user = MySQLAccountMixed.mysql_dbha_account(int(bk_cloud_id)).get("user") or ""
    except Exception as exc:
        logger.warning(_("获取云区域 {} 的 DBHA 探测账号失败: {}").format(bk_cloud_id, exc))
        return ""
    return str(user).strip()


def get_mysql_sys_users(bk_cloud_id, dbha_user=None) -> list:
    """
    增加方法：收集SaaS内mysql/spider的系统账号列表，作为固定参数传入待执行Actuator指令
    dbha_user 非 None 时使用调用方已经取到的探测用户名，不再查询 DBHA 组件。
    """
    sys_users_map = {
        ExtensionType.DRS: env.DRS_USERNAME,
    }
    if dbha_user is None:
        sys_users_map[ExtensionType.DBHA] = env.DBHA_USERNAME
    sys_users = [UserName.ADMIN.value, UserName.PARTITION_YW.value]
    for key, value in sys_users_map.items():
        if value:
            sys_users.append(value)
        else:
            bk_cloud_name = AsymmetricCipherConfigType.get_cipher_cloud_name(bk_cloud_id)
            info = DBExtension.get_latest_extension(bk_cloud_id=bk_cloud_id, extension_type=key)
            logger.error(f"[{key}] details: {info.details}, bk_cloud_name: {bk_cloud_name}")
            sys_users.append(AsymmetricHandler.decrypt(name=bk_cloud_name, content=info.details["user"]))
            # 对于drs组件单独添加webconsole账号
            if key == ExtensionType.DRS:
                sys_users.append(
                    AsymmetricHandler.decrypt(name=bk_cloud_name, content=info.details["webconsole_user"])
                )

    if dbha_user is not None:
        probe_user = str(dbha_user).strip()
        if probe_user:
            sys_users.append(probe_user.replace("'", "''"))

    return sys_users


def generate_mysql_tmp_user(root_id):
    return f"{DBM_MYSQL_JOB_TMP_USER_PREFIX}{root_id}"
