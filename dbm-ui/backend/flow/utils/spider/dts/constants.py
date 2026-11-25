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
# TenDBCluster → TenDBCluster DTS 迁移（cluster_to_cluster）的平台侧固定取值：
# 任务模式只允许 DTS 内置全量 + 增量；分片协调固定悲观模式，单据不可覆盖
CLUSTER_TO_CLUSTER_TASK_MODE = "all"
CLUSTER_TO_CLUSTER_SHARD_MODE = "pessimistic"
# 切换命令锁模式：默认逐表读锁（03 dbactuator spider dts-cutover）
SPIDER_DTS_CUTOVER_LOCK_MODE = "tables"
# spider dts-cutover 命令上限约 3 分钟（03 第 8 节），执行节点 Job 超时设 5 分钟兜底
SPIDER_DTS_CUTOVER_JOB_TIMEOUT = 300
