# MySQL回档演练流程分支网关改造 - 实施总结

## 实施完成时间
2025年10月21日

## 改造目标
使`tendbha_rollback_data_sub_flow`子流程失败后能继续执行，并根据执行结果更新不同的任务状态。

## 已完成的修改

### 1. 新建组件文件

#### 1.1 MarkRollbackStatusComponent
**文件**: `dbm-ui/backend/flow/plugins/components/collections/mysql/mark_rollback_status.py`

**功能**: 在子流程成功执行完毕时，标记状态为"success"并写入上下文变量`trans_data.rollback_status`

**关键代码**:
```python
trans_data.rollback_status = "success"
data.outputs.trans_data = trans_data
```

#### 1.2 CheckRollbackStatusComponent
**文件**: `dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py`

**功能**: 检查子流程执行状态，如果有成功标记则保持，否则设置为"failed"。此组件始终返回成功，确保流程继续。

**关键代码**:
```python
if hasattr(trans_data, "rollback_status") and trans_data.rollback_status == "success":
    status = "success"
else:
    status = "failed"
trans_data.rollback_status = status
```

### 2. 更新TaskStatus枚举

**文件**: `dbm-ui/backend/db_periodic_task/models.py`

**修改内容**: 添加`RECOVER_FAILED = "recover_failed"`状态

```python
class TaskStatus:
    ...
    RECOVER_SUCCESS = "recover_success"
    RECOVER_FAILED = "recover_failed"  # 新增
    RESOURCE_RETURN_SUCCESS = "resource_return_success"
```

### 3. 修改子流程

**文件**: `dbm-ui/backend/flow/engine/bamboo/scene/mysql/common/mysql_resotre_data_sub_flow.py`

**修改内容**:
1. 导入新组件: `from backend.flow.plugins.components.collections.mysql.mark_rollback_status import MarkRollbackStatusComponent`
2. 在`tendbha_rollback_data_sub_flow`函数末尾，`build_sub_process`之前添加标记节点:

```python
# 标记回档执行成功
sub_pipeline.add_act(
    act_name=_("标记回档执行成功"),
    act_component_code=MarkRollbackStatusComponent.code,
    kwargs={},
    write_payload_var="rollback_status",
)
```

**注意**: 使用默认的`is_remote_rewritable=False`，确保上下文能正常传递。

### 4. 修改主流程

**文件**: `dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py`

**修改内容**:

1. 导入新组件和Conditions:
```python
from backend.flow.engine.bamboo.scene.common.builder import Builder, Conditions
from backend.flow.plugins.components.collections.mysql.check_rollback_status import CheckRollbackStatusComponent
```

2. 替换单一更新节点为条件网关（第249-292行）:

```python
# 检查回档执行状态
check_act = pipeline.add_act(
    act_name=_("检查回档执行状态"),
    act_component_code=CheckRollbackStatusComponent.code,
    kwargs={},
    write_payload_var="rollback_status",
    # 关键！不设置is_remote_rewritable，让它参与上下文传递链
    extend=False,  # add_conditional_subs内部会连接它
)

# 创建成功分支节点
success_act = pipeline.add_act(
    act_name=_("更新演练任务状态为成功"),
    act_component_code=MySQLBackupRecoverTaskMetaComponent.code,
    kwargs={"task_id": self.root_id, "task_status": "recover_success"},
    is_remote_rewritable=True,
    extend=False,
)

# 创建失败分支节点
failed_act = pipeline.add_act(
    act_name=_("更新演练任务状态为失败"),
    act_component_code=MySQLBackupRecoverTaskMetaComponent.code,
    kwargs={"task_id": self.root_id, "task_status": "recover_failed"},
    is_remote_rewritable=True,
    extend=False,
)

# 添加条件网关：根据回档执行结果选择不同分支
pipeline.add_conditional_subs(
    source_act=check_act,
    conditions=[
        Conditions(act_object=success_act, express='== "success"'),
        Conditions(act_object=failed_act, express='== "failed"'),
    ],
    name=_("根据回档结果选择分支"),
    conditions_param="rollback_status",
)
```

## 技术要点

### 1. 上下文变量传递与条件网关 ⚠️ 重要
- **关键修复**: 
  - **source_act（check_act）不设置`is_remote_rewritable`**：必须参与上下文传递链，因为`add_conditional_subs`内部会通过`self.pipe.extend(source_act)`将它连接到主流程
  - **分支节点（success_act/failed_act）必须设置`is_remote_rewritable=True`**：避免干扰主流程的上下文传递链
- `CheckRollbackStatusComponent`需要同时写入两处：
  - `trans_data.rollback_status`: 传递给后续节点使用（如果需要）
  - `data.outputs.rollback_status`: 供条件网关的`NodeOutput`读取
- 子流程中的节点使用默认的`is_remote_rewritable=False`，确保子流程内部上下文正常传递

### 2. 条件网关机制
- 使用bamboo-engine的`ConditionalParallelGateway`实现分支控制
- `conditions_param`指定用于判断的上下文变量名
- `express`定义条件表达式（如`== "success"`）
- 默认分支会在所有条件都不满足时执行

### 3. 流程继续保证
- `CheckRollbackStatusComponent`始终返回`True`
- 即使子流程失败，主流程也能继续执行后续的资源回收操作

## 执行流程

### 成功场景:
1. `tendbha_rollback_data_sub_flow`子流程正常执行
2. 子流程末尾的`MarkRollbackStatusComponent`标记`rollback_status = "success"`
3. `CheckRollbackStatusComponent`检测到成功标记，保持状态
4. 条件网关判断`rollback_status == "success"`为真，执行成功分支
5. 更新任务状态为`recover_success`
6. 继续执行后续的资源回收流程

### 失败场景:
1. `tendbha_rollback_data_sub_flow`子流程执行失败
2. 子流程中的标记节点未执行，`rollback_status`未设置
3. `CheckRollbackStatusComponent`未检测到成功标记，设置`rollback_status = "failed"`
4. 条件网关判断进入失败分支
5. 更新任务状态为`recover_failed`
6. 继续执行后续的资源回收流程

## 验证建议

### 测试用例1: 正常回档成功
- **操作**: 执行正常的回档演练任务
- **预期**: 任务状态更新为`recover_success`，后续资源回收正常执行

### 测试用例2: 回档失败
- **操作**: 模拟回档失败（如备份文件损坏、磁盘空间不足等）
- **预期**: 任务状态更新为`recover_failed`，后续资源回收仍正常执行

### 测试用例3: 子流程中断
- **操作**: 在子流程执行过程中手动强制失败某个节点
- **预期**: 流程能继续，状态更新为`recover_failed`

## 注意事项

1. **向后兼容**: 新增的`RECOVER_FAILED`状态需要在前端和监控系统中对应处理
2. **国际化**: 所有新增的中文字符串都使用了`_()`进行国际化包装
3. **日志记录**: 两个新组件都添加了详细的日志记录，便于问题排查
4. **上下文管理**: 严格控制`is_remote_rewritable`参数，确保上下文正确传递

## 关键问题与修复

### 问题: 上下文传递链被破坏

**症状**:
```
[ERROR] [写入上下文结果失败] failed: 'str' object has no attribute 'time_zone_info'
[ERROR] [安装MySQL实例] 获取执行后写入流程上下文失败
```

**原因**:
1. 条件网关中的节点如果不设置`is_remote_rewritable=True`，会被加入到`rewritable_node_source_keys`列表
2. 这些节点通过条件网关连接，不在主流程的串行链上，导致上下文传递链混乱
3. 后续节点尝试从错误的上下文对象读取数据，导致属性不存在错误

**解决方案**:
1. **所有条件网关中的节点**都必须设置`is_remote_rewritable=True`
2. `CheckRollbackStatusComponent`需要同时写入两个位置：
   - `data.outputs.rollback_status`: 供条件网关的`NodeOutput`读取
   - `trans_data.rollback_status`: 供后续节点使用（如果需要）
3. 子流程中的节点使用默认设置，确保子流程内部上下文传递正常

**代码示例**:
```python
# 正确的设置
check_act = pipeline.add_act(
    act_name=_("检查回档执行状态"),
    act_component_code=CheckRollbackStatusComponent.code,
    kwargs={},
    write_payload_var="rollback_status",
    is_remote_rewritable=True,  # 必须设置！
    extend=False,
)
```

## 相关文件清单

- ✅ `/dbm-ui/backend/flow/plugins/components/collections/mysql/mark_rollback_status.py` (新建)
- ✅ `/dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py` (新建)
- ✅ `/dbm-ui/backend/db_periodic_task/models.py` (修改)
- ✅ `/dbm-ui/backend/flow/engine/bamboo/scene/mysql/common/mysql_resotre_data_sub_flow.py` (修改)
- ✅ `/dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py` (修改)

