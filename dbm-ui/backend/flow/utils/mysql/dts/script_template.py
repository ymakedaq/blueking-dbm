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

# 介质包布局固定为 dts/{bin,conf,scripts,...}，解到 deploy_path 去掉顶层 dts/ 即可。
#
# 注意：日志文件必须用 dts_node_name（如 dm-master-1），不能用 pipeline 的 node_name。
# SubBuilder.add_act 会把 kwargs["node_name"] 覆盖成中文 act_name（如「启动 Worker」）。

# 后台启动：优先 setsid -f（util-linux 较新）；旧系统不支持 -f 时回退 setsid ... &
_START_DAEMON_HELPER = """
start_daemon() {
  local bin="$1"
  local conf="$2"
  local out="$3"
  if setsid -f true >/dev/null 2>&1; then
    setsid -f "${bin}" -config "${conf}" > "${out}" 2>&1 < /dev/null
  else
    setsid "${bin}" -config "${conf}" > "${out}" 2>&1 < /dev/null &
  fi
}
"""

start_mysql_dts_master_template = (
    """
set -euo pipefail
DEPLOY_PATH="{{deploy_path}}"
PKG_NAME="{{pkg_name}}"
DTS_NODE_NAME="{{dts_node_name}}"
BIN_DIR="${DEPLOY_PATH}/bin"
CONF_DIR="${DEPLOY_PATH}/conf"
LOG_DIR="${DEPLOY_PATH}/log"
PKG_FILE="/data/install/${PKG_NAME}"
OUTPUT_FILE="${LOG_DIR}/${DTS_NODE_NAME}.output"

if [[ -z "${PKG_NAME}" ]]; then
  echo "pkg_name is empty" >&2
  exit 1
fi
if [[ -z "${DTS_NODE_NAME}" ]]; then
  echo "dts_node_name is empty" >&2
  exit 1
fi
if [[ ! -f "${PKG_FILE}" ]]; then
  echo "DTS package not found: ${PKG_FILE}" >&2
  ls -la /data/install/ || true
  exit 1
fi

mkdir -p "${DEPLOY_PATH}" "${CONF_DIR}" "${LOG_DIR}"
if ! tar -zxf "${PKG_FILE}" -C "${DEPLOY_PATH}" --strip-components=1 2>/dev/null; then
  tar -xf "${PKG_FILE}" -C "${DEPLOY_PATH}" --strip-components=1
fi
if [[ ! -f "${BIN_DIR}/dm-master" ]]; then
  echo "dm-master missing after extract, expect ${BIN_DIR}/dm-master" >&2
  ls -la "${DEPLOY_PATH}" "${BIN_DIR}" || true
  exit 1
fi
chmod +x "${BIN_DIR}/dm-master"
"""
    + _START_DAEMON_HELPER
    + """
start_daemon "${BIN_DIR}/dm-master" "${CONF_DIR}/{{config_file}}" "${OUTPUT_FILE}"

LISTEN_PORT="{{listen_port}}"
is_port_listen() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "(^|[.:])${port}$"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "(^|[.:])${port}$"
  else
    (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
  fi
}

# 进程在 + master-addr 端口已监听，才视为启动成功（OpenAPI 注册由后续验收组件负责）
ready=0
for _i in $(seq 1 10); do
  if pgrep -f "${BIN_DIR}/dm-master" >/dev/null 2>&1 && is_port_listen "${LISTEN_PORT}"; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" -ne 1 ]]; then
  echo "dm-master failed to become ready (process/port ${LISTEN_PORT}):" >&2
  cat "${OUTPUT_FILE}" >&2 || true
  exit 1
fi
echo "started dm-master ${DTS_NODE_NAME} (listen ${LISTEN_PORT})"
"""
)

start_mysql_dts_worker_template = (
    """
set -euo pipefail
DEPLOY_PATH="{{deploy_path}}"
PKG_NAME="{{pkg_name}}"
DTS_NODE_NAME="{{dts_node_name}}"
BIN_DIR="${DEPLOY_PATH}/bin"
CONF_DIR="${DEPLOY_PATH}/conf"
LOG_DIR="${DEPLOY_PATH}/log"
PKG_FILE="/data/install/${PKG_NAME}"
OUTPUT_FILE="${LOG_DIR}/${DTS_NODE_NAME}.output"

if [[ -z "${PKG_NAME}" ]]; then
  echo "pkg_name is empty" >&2
  exit 1
fi
if [[ -z "${DTS_NODE_NAME}" ]]; then
  echo "dts_node_name is empty" >&2
  exit 1
fi
if [[ ! -f "${PKG_FILE}" ]]; then
  echo "DTS package not found: ${PKG_FILE}" >&2
  ls -la /data/install/ || true
  exit 1
fi

mkdir -p "${DEPLOY_PATH}" "${CONF_DIR}" "${LOG_DIR}"
if ! tar -zxf "${PKG_FILE}" -C "${DEPLOY_PATH}" --strip-components=1 2>/dev/null; then
  tar -xf "${PKG_FILE}" -C "${DEPLOY_PATH}" --strip-components=1
fi
if [[ ! -f "${BIN_DIR}/dm-worker" ]]; then
  echo "dm-worker missing after extract, expect ${BIN_DIR}/dm-worker" >&2
  ls -la "${DEPLOY_PATH}" "${BIN_DIR}" || true
  exit 1
fi
chmod +x "${BIN_DIR}/dm-worker"
"""
    + _START_DAEMON_HELPER
    + """
start_daemon "${BIN_DIR}/dm-worker" "${CONF_DIR}/{{config_file}}" "${OUTPUT_FILE}"

LISTEN_PORT="{{listen_port}}"
is_port_listen() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "(^|[.:])${port}$"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -qE "(^|[.:])${port}$"
  else
    (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
  fi
}

# 进程在 + worker-addr 端口已监听，才视为启动成功
ready=0
for _i in $(seq 1 10); do
  if pgrep -f "${BIN_DIR}/dm-worker" >/dev/null 2>&1 && is_port_listen "${LISTEN_PORT}"; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" -ne 1 ]]; then
  echo "dm-worker failed to become ready (process/port ${LISTEN_PORT}):" >&2
  cat "${OUTPUT_FILE}" >&2 || true
  exit 1
fi
echo "started dm-worker ${DTS_NODE_NAME} (listen ${LISTEN_PORT})"
"""
)

stop_mysql_dts_process_template = """
set -euo pipefail
# 先停 Worker 再停 Master，满足 offline_worker「进程须先离线」约束
pkill -f "{{deploy_path}}/bin/dm-worker" 2>/dev/null || true
pkill -f "{{deploy_path}}/bin/dm-master" 2>/dev/null || true
sleep 1
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

# 将 /data/install 下的 dbbackup 介质解压到 /home/mysql/dbbackup，保证 bin/myloader 可用
ensure_myloader_binary_template = """
set -euo pipefail
INSTALL_DIR="/data/install"
TARGET="/home/mysql/dbbackup"
PKG="$(ls -1t ${INSTALL_DIR}/dbbackup*.tar.gz ${INSTALL_DIR}/dbbackup*.tgz 2>/dev/null | head -1 || true)"
if [ -z "${PKG}" ]; then
  echo "dbbackup package not found under ${INSTALL_DIR}"
  exit 1
fi
rm -rf "${TARGET}"
mkdir -p "${TARGET}"
tar -xzf "${PKG}" -C "${TARGET}"
if [ ! -e "${TARGET}/bin/myloader" ]; then
  FOUND="$(find "${TARGET}" -type f -name myloader 2>/dev/null | head -1 || true)"
  if [ -z "${FOUND}" ]; then
    echo "myloader binary not found after extract ${PKG}"
    exit 1
  fi
  mkdir -p "${TARGET}/bin"
  ln -sfn "${FOUND}" "${TARGET}/bin/myloader"
fi
chown -R mysql:mysql "${TARGET}" || true
chmod a+x "${TARGET}/bin/myloader" || true
test -e "${TARGET}/bin/myloader"
echo "myloader ready at ${TARGET}/bin/myloader"
"""
