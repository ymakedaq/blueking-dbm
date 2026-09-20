/*
 * TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
 * Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
 * Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at https://opensource.org/licenses/MIT
 * Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
 * an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations under the License.
 */

package matcherr

import "fmt"

// NoMatchExecuteDbError 表单库在模拟实例中匹配为空时的报错。
func NoMatchExecuteDbError(formDbs, ignoreDbs, instanceDbs []string) error {
	return fmt.Errorf(
		"表单变更对象 %v 在模拟实例中不存在。忽略库：%v。模拟实例当前库：%v。请检查变更对象是否填写正确。",
		formDbs, ignoreDbs, instanceDbs,
	)
}
