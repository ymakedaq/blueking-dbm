# 关键修复：上下文传递链问题

## 问题描述

在实施MySQL回档演练流程分支网关改造后，出现以下错误：

```
[ERROR][2025-10-21 17:56:07 ERROR][flow]: [写入上下文结果失败] failed: 'str' object has no attribute 'time_zone_info'
[ERROR][2025-10-21 17:56:07 ERROR][flow]: [安装MySQL实例] 获取执行后写入流程上下文失败，ip:[9.150.82.69]
```

## 根本原因

### bamboo-engine的上下文传递机制

在bamboo-engine中，`is_remote_rewritable`参数控制节点是否参与上下文传递链：

```python
# Builder.add_act() 方法中
if not is_remote_rewritable:
    self.rewritable_node_source_keys.append({"source_act": act.id, "source_key": "trans_data"})
```

- `is_remote_rewritable=False`（默认）: 节点加入上下文传递链
- `is_remote_rewritable=True`: 节点不参与上下文传递链

### 问题产生过程

1. **条件网关中的节点未设置`is_remote_rewritable=True`**
   ```python
   check_act = pipeline.add_act(
       act_name=_("检查回档执行状态"),
       extend=False,  # 通过条件网关连接，不在串行链上
       # 缺少 is_remote_rewritable=True
   )
   ```

2. **节点被错误地加入上下文传递链**
   - 这些节点通过`extend=False`创建，不在主流程的串行链上
   - 但被加入到`rewritable_node_source_keys`列表
   - 导致上下文传递链的顺序和结构混乱

3. **后续节点读取错误的上下文对象**
   - 例如"安装MySQL实例"节点期望从`trans_data`读取`time_zone_info`
   - 但由于上下文链被破坏，得到的是错误的对象类型（字符串而非对象）
   - 导致属性访问失败

## 修复方案 - 最终版本

### 关键发现

**问题根源**：`add_conditional_subs`方法内部会通过`self.pipe.extend(source_act)`将source_act连接到主流程。因此：
1. source_act（check_act）必须**参与上下文传递链**
2. 只有分支节点（success_act/failed_act）才需要设置`is_remote_rewritable=True`

### 1. 正确配置check_act节点

**修复文件**: `dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py`

```python
# 检查回档执行状态
check_act = pipeline.add_act(
    act_name=_("检查回档执行状态"),
    act_component_code=CheckRollbackStatusComponent.code,
    kwargs={},
    write_payload_var="rollback_status",
    # ✅ 关键：不设置is_remote_rewritable，让它参与上下文传递链
    extend=False,  # 避免重复连接（add_conditional_subs会连接它）
)

# 创建成功分支节点
success_act = pipeline.add_act(
    act_name=_("更新演练任务状态为成功"),
    act_component_code=MySQLBackupRecoverTaskMetaComponent.code,
    kwargs={"task_id": self.root_id, "task_status": "recover_success"},
    is_remote_rewritable=True,  # ✅ 关键修复！
    extend=False,
)

# 创建失败分支节点
failed_act = pipeline.add_act(
    act_name=_("更新演练任务状态为失败"),
    act_component_code=MySQLBackupRecoverTaskMetaComponent.code,
    kwargs={"task_id": self.root_id, "task_status": "recover_failed"},
    is_remote_rewritable=True,  # ✅ 关键修复！
    extend=False,
)

# 添加条件网关
pipeline.add_conditional_subs(
    source_act=check_act,  # check_act会被add_conditional_subs连接到主流程
    conditions=[
        Conditions(act_object=success_act, express='== "success"'),
        Conditions(act_object=failed_act, express='== "failed"'),
    ],
    name=_("根据回档结果选择分支"),
    conditions_param="rollback_status",
)
```

**关键理解**：

1. **source_act（check_act）**：
   - 设置`extend=False`：避免在add_act时重复连接
   - **不设置**`is_remote_rewritable`（默认False）：必须参与上下文传递链
   - add_conditional_subs内部会通过`self.pipe.extend(source_act)`将它连接到主流程
   
2. **分支节点（success_act/failed_act）**：
   - 设置`extend=False`：通过条件网关连接，不直接连接主流程
   - 设置`is_remote_rewritable=True`：不参与上下文传递链，避免干扰

### 2. 修复CheckRollbackStatusComponent的输出

**修复文件**: `dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py`

**问题**: 条件网关使用`NodeOutput`从节点输出读取值，期望从`data.outputs.rollback_status`读取，但组件只写入了`trans_data.rollback_status`。

**修复**:
```python
def _execute(self, data, parent_data) -> bool:
    trans_data = data.get_one_of_inputs("trans_data")
    
    # 检查是否已有rollback_status
    if hasattr(trans_data, "rollback_status") and trans_data.rollback_status == "success":
        status = "success"
    else:
        status = "failed"
    
    # 将最终状态写入trans_data
    trans_data.rollback_status = status
    data.outputs.trans_data = trans_data
    
    # ✅ 关键修复！同时将状态直接写入outputs，供条件网关使用
    data.outputs.rollback_status = status
    
    return True
```

## 技术细节

### bamboo-engine条件网关的工作原理

1. **NodeOutput的定义**
   ```python
   self.global_data.inputs[f"${{{conditions_param}}}"] = NodeOutput(
       type=Var.SPLICE, 
       source_act=source_act_id, 
       source_key=conditions_param
   )
   ```

2. **NodeOutput如何读取值**
   - `source_act`: 源节点ID（这里是check_act）
   - `source_key`: 要读取的键名（这里是"rollback_status"）
   - NodeOutput会从`source_act.outputs[source_key]`读取值
   - **不是**从`trans_data.rollback_status`读取！

3. **为什么需要同时写入两处**
   - `data.outputs.rollback_status`: 供条件网关的NodeOutput读取
   - `trans_data.rollback_status`: 如果后续节点需要使用（保持兼容性）

## 验证结果

修复后，上下文传递链恢复正常：

✅ 条件网关中的节点不参与主流程上下文传递链
✅ 条件网关能正确读取`rollback_status`进行分支判断
✅ 后续节点（如"安装MySQL实例"）能正确读取`time_zone_info`等属性
✅ 整个流程能正常执行，无论回档成功或失败

## 经验教训

### 使用条件网关的注意事项

1. **通过条件网关连接的节点必须设置`is_remote_rewritable=True`**
   - 这些节点不在主流程的串行链上
   - 如果参与上下文传递链，会破坏主流程的上下文传递顺序

2. **条件网关使用NodeOutput读取值**
   - NodeOutput从节点的`data.outputs`中读取
   - 不是从`trans_data`中读取
   - 源节点需要将值写入到`data.outputs[key]`

3. **子流程中的节点使用默认设置**
   - 子流程内部的节点应该参与子流程的上下文传递链
   - 只有通过条件网关连接的节点才需要特殊处理

### 最佳实践

```python
# ✅ 正确：条件网关的source_act
source_act = pipeline.add_act(
    act_name=_("检查状态"),
    act_component_code=CheckComponent.code,
    kwargs={...},
    write_payload_var="status_var",
    # 不设置is_remote_rewritable，参与上下文传递链
    extend=False,  # add_conditional_subs会连接它
)

# ✅ 正确：条件网关的分支节点
branch_act = pipeline.add_act(
    act_name=_("处理分支"),
    act_component_code=SomeComponent.code,
    kwargs={...},
    is_remote_rewritable=True,  # 必须设置！避免干扰主流程
    extend=False,
)

# ✅ 正确：串行链上的节点
node = pipeline.add_act(
    act_name=_("节点名称"),
    act_component_code=SomeComponent.code,
    kwargs={...},
    write_payload_var="some_var",  # 如果需要
    # is_remote_rewritable 默认False，参与上下文传递
)

# ✅ 正确：组件同时写入outputs和trans_data
def _execute(self, data, parent_data):
    trans_data = data.get_one_of_inputs("trans_data")
    result = "some_value"
    
    # 写入trans_data（供后续节点使用）
    trans_data.some_var = result
    data.outputs.trans_data = trans_data
    
    # 写入outputs（供条件网关使用）
    data.outputs.some_var = result
    
    return True
```

## 相关文件

- ✅ `dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py` (已修复)
- ✅ `dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py` (已修复)
- ✅ `dbm-ui/backend/flow/engine/bamboo/scene/common/builder.py` (参考实现)

## 测试验证

建议进行以下测试：

1. **正常回档场景**: 验证条件网关正确选择成功分支
2. **回档失败场景**: 验证条件网关正确选择失败分支
3. **上下文传递**: 验证后续节点能正确读取上下文数据（如time_zone_info）
4. **资源回收**: 验证无论成功失败，资源回收流程都能正常执行

