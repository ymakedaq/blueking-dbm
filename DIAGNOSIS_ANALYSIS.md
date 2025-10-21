# 诊断分析报告 - MySQL回档演练上下文传递问题

## 关键发现

通过git历史对比，发现：

1. **原始版本**（51461d515）：没有条件网关，只有一个更新任务状态节点（带`is_remote_rewritable=True`）
2. **修改后版本**（179d8df4e/当前）：添加了条件网关和check_act
3. **用户报告**：即使多次调整后，错误依然出现在MySQL安装子流程

## 问题定位

错误发生在：
```
dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_single_apply_flow.py:155
write_payload_var=SingleApplyManualContext.get_time_zone_var_name()
```

这是MySQL安装子流程中的"安装MySQL实例"节点。

## 根本原因分析

### 假设：rewritable_node_source_keys为空导致trans_data未正确初始化

当前流程的上下文传递链：

```
MySQL安装子流程（SubBuilder）
  ├─ 初始化机器
  ├─ 下发MySQL介质
  └─ 安装MySQL实例 ← 这里出错，trans_data是字符串而不是对象
```

**关键问题**：父流程（MySQLRollbackExerciseFlow）在调用MySQL安装子流程前，有多个节点设置了`is_remote_rewritable=True`：
1. 屏蔽告警（第197行）
2. 更新任务状态-deploy_success（第207行）
3. 创建目录（第236行）

**如果这些节点都不参与上下文传递链，那么`rewritable_node_source_keys`可能为空或不完整！**

### 验证方法

在`run_pipeline`调用前打印`pipeline.rewritable_node_source_keys`：
- 如果为空：说明所有节点都设置了`is_remote_rewritable=True`
- 如果不完整：说明上下文传递链断裂

### 关键代码分析

`builder.py:286-287`：
```python
self.global_data.inputs["${trans_data}"] = RewritableNode(
    source_act=self.rewritable_node_source_keys, type=Var.SPLICE, value=init_trans_data_class
)
```

**问题**：如果`source_act`（即`rewritable_node_source_keys`）为空或不完整，RewritableNode如何工作？

### MySQL安装子流程的trans_data初始化

`mysql_single_apply_flow.py:114`：
```python
sub_pipeline = SubBuilder(root_id=self.root_id, data=copy.deepcopy(sub_flow_context))
```

`builder.py:345-346`：
```python
sub_data.inputs["${trans_data}"] = RewritableNode(
    source_act=self.rewritable_node_source_keys, type=Var.SPLICE, value=None
)
```

**子流程的trans_data初始化值为None！**

`builder.py:353`：
```python
sub_params = Params({"${trans_data}": Var(type=Var.SPLICE, value="${trans_data}")})
```

**子流程params指定从父流程的${trans_data}获取值**

## 问题根源

**核心问题**：父流程的MySQL安装子流程被调用时，如果父流程的`rewritable_node_source_keys`为空或不完整，父流程的`trans_data`可能未正确初始化为`SingleApplyManualContext`对象，而是保持为某个默认值（可能是空字符串）。

当子流程尝试从父流程获取`trans_data`时，得到的是字符串而不是对象。

## 解决方案

### 方案1：确保父流程有参与上下文传递的节点

在MySQL安装子流程之前，至少要有一个节点**不设置**`is_remote_rewritable=True`，确保上下文传递链不为空。

**实施**：移除某些节点的`is_remote_rewritable=True`设置。

**风险**：这些节点原本就设置了`is_remote_rewritable=True`，可能有其他原因。

### 方案2：显式初始化子流程的trans_data

修改MySQL安装子流程的调用，不依赖父流程的trans_data：

```python
MySQLSingleApplyFlow(root_id=self.root_id, data=install_ticket).deploy_mysql_single_flow(...)
```

在`deploy_mysql_single_flow`中显式初始化trans_data为`SingleApplyManualContext()`。

### 方案3：修改run_pipeline的init_trans_data_class

在父流程中：
```python
pipeline.run_pipeline(init_trans_data_class=SingleApplyManualContext())
```

但这可能不够，因为子流程的trans_data初始化也需要处理。

## 建议的实施步骤

1. **添加诊断日志**：在关键位置打印rewritable_node_source_keys和trans_data的类型
2. **尝试方案1**：移除屏蔽告警节点的`is_remote_rewritable=True`
3. **如果方案1不行**：移除所有后续节点的`is_remote_rewritable=True`
4. **最后方案**：修改子流程初始化逻辑

## 下一步行动

立即添加诊断日志，验证假设！

