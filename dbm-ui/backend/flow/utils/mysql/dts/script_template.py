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
PKG_FILE="/data/install/${PKG_NAME}"

if [[ -z "${PKG_NAME}" ]]; then
  echo "pkg_name is empty, cannot extract DTS package" >&2
  exit 1
fi
if [[ ! -f "${PKG_FILE}" ]]; then
  echo "DTS package not found: ${PKG_FILE}" >&2
  ls -la /data/install/ || true
  exit 1
fi

mkdir -p "${BIN_DIR}" "${CONF_DIR}" "${LOG_DIR}"
if ! tar -zxf "${PKG_FILE}" -C "${BIN_DIR}" --strip-components=1 2>/dev/null; then
  tar -xf "${PKG_FILE}" -C "${BIN_DIR}" --strip-components=1
fi
if [[ ! -x "${BIN_DIR}/dm-master" && ! -f "${BIN_DIR}/dm-master" ]]; then
  echo "dm-master binary missing after extract, BIN_DIR contents:" >&2
  ls -la "${BIN_DIR}" || true
  exit 1
fi
chmod +x "${BIN_DIR}/dm-master"

setsid "${BIN_DIR}/dm-master" -config "${CONF_DIR}/{{config_file}}" \
  > "${LOG_DIR}/{{node_name}}.output" 2>&1 &
sleep 2
if ! pgrep -f "${BIN_DIR}/dm-master" >/dev/null 2>&1; then
  echo "dm-master failed to start, output:" >&2
  cat "${LOG_DIR}/{{node_name}}.output" >&2 || true
  exit 1
fi
echo "started dm-master {{node_name}}"
"""

start_mysql_dts_worker_template = """
set -euo pipefail
DEPLOY_PATH="{{deploy_path}}"
PKG_NAME="{{pkg_name}}"
BIN_DIR="${DEPLOY_PATH}/bin"
CONF_DIR="${DEPLOY_PATH}/conf"
LOG_DIR="${DEPLOY_PATH}/log"
PKG_FILE="/data/install/${PKG_NAME}"

if [[ -z "${PKG_NAME}" ]]; then
  echo "pkg_name is empty, cannot extract DTS package" >&2
  exit 1
fi
if [[ ! -f "${PKG_FILE}" ]]; then
  echo "DTS package not found: ${PKG_FILE}" >&2
  ls -la /data/install/ || true
  exit 1
fi

mkdir -p "${BIN_DIR}" "${CONF_DIR}" "${LOG_DIR}"
if ! tar -zxf "${PKG_FILE}" -C "${BIN_DIR}" --strip-components=1 2>/dev/null; then
  tar -xf "${PKG_FILE}" -C "${BIN_DIR}" --strip-components=1
fi
if [[ ! -x "${BIN_DIR}/dm-worker" && ! -f "${BIN_DIR}/dm-worker" ]]; then
  echo "dm-worker binary missing after extract, BIN_DIR contents:" >&2
  ls -la "${BIN_DIR}" || true
  exit 1
fi
chmod +x "${BIN_DIR}/dm-worker"

setsid "${BIN_DIR}/dm-worker" -config "${CONF_DIR}/{{config_file}}" \
  > "${LOG_DIR}/{{node_name}}.output" 2>&1 &
sleep 2
if ! pgrep -f "${BIN_DIR}/dm-worker" >/dev/null 2>&1; then
  echo "dm-worker failed to start, output:" >&2
  cat "${LOG_DIR}/{{node_name}}.output" >&2 || true
  exit 1
fi
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
