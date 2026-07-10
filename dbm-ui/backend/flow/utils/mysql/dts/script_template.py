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
start_mysql_dts_master_template = """
set -euo pipefail
DEPLOY_PATH="{{deploy_path}}"
PKG_NAME="{{pkg_name}}"
BIN_DIR="${DEPLOY_PATH}/bin"
CONF_DIR="${DEPLOY_PATH}/conf"
LOG_DIR="${DEPLOY_PATH}/log"
mkdir -p "${BIN_DIR}" "${CONF_DIR}" "${LOG_DIR}"
if [[ -f /data/install/${PKG_NAME} ]]; then
  tar -zxf /data/install/${PKG_NAME} -C "${BIN_DIR}" --strip-components=1 2>/dev/null || \
  tar -xf /data/install/${PKG_NAME} -C "${BIN_DIR}" --strip-components=1
fi
chmod +x "${BIN_DIR}/dm-master" 2>/dev/null || true
setsid "${BIN_DIR}/dm-master" -config "${CONF_DIR}/{{config_file}}" \
  > "${LOG_DIR}/{{node_name}}.output" 2>&1 &
echo "started dm-master {{node_name}}"
"""

start_mysql_dts_worker_template = """
set -euo pipefail
DEPLOY_PATH="{{deploy_path}}"
PKG_NAME="{{pkg_name}}"
BIN_DIR="${DEPLOY_PATH}/bin"
CONF_DIR="${DEPLOY_PATH}/conf"
LOG_DIR="${DEPLOY_PATH}/log"
mkdir -p "${BIN_DIR}" "${CONF_DIR}" "${LOG_DIR}"
if [[ -f /data/install/${PKG_NAME} ]]; then
  tar -zxf /data/install/${PKG_NAME} -C "${BIN_DIR}" --strip-components=1 2>/dev/null || \
  tar -xf /data/install/${PKG_NAME} -C "${BIN_DIR}" --strip-components=1
fi
chmod +x "${BIN_DIR}/dm-worker" 2>/dev/null || true
setsid "${BIN_DIR}/dm-worker" -config "${CONF_DIR}/{{config_file}}" \
  > "${LOG_DIR}/{{node_name}}.output" 2>&1 &
echo "started dm-worker {{node_name}}"
"""

stop_mysql_dts_process_template = """
set -euo pipefail
pkill -f "{{deploy_path}}/bin/dm-master" 2>/dev/null || true
pkill -f "{{deploy_path}}/bin/dm-worker" 2>/dev/null || true
echo "stopped dts processes under {{deploy_path}}"
"""

clean_mysql_dts_data_dir_template = """
set -euo pipefail
rm -rf "{{deploy_path}}"
echo "cleaned {{deploy_path}}"
"""

push_mysql_dts_config_template = """
set -euo pipefail
CONF_DIR="{{deploy_path}}/conf"
mkdir -p "${CONF_DIR}"
cat > "${CONF_DIR}/{{config_file}}" <<'DTS_CONFIG_EOF'
{{config_content}}
DTS_CONFIG_EOF
echo "wrote ${CONF_DIR}/{{config_file}}"
"""
