# 上下文传递问题最终解决方案

## 问题根源

经过深入分析，发现问题的根源是：

**在MySQL安装子流程之前，所有节点都设置了`is_remote_rewritable=True`，导致父流程的`rewritable_node_source_keys`为空，进而导致`trans_data`未正确初始化为`SingleApplyManualContext`对象。**

## 流程分析

```
MySQLRollbackExerciseFlow (父流程)
  ├─ MySQL安装子流程 (SubBuilder)
  │   └─ 安装MySQL实例 (需要write_payload_var=time_zone_info)
  │       ← trans_data应该是SingleApplyManualContext对象，但实际是字符串！
  ├─ 屏蔽告警 (is_remote_rewritable=True) ← 不参与上下文传递链
  ├─ 更新任务状态-deploy_success (is_remote_rewritable=True) ← 不参与上下文传递链
  ├─ 创建目录 (is_remote_rewritable=True) ← 不参与上下文传递链
  ├─ 回档子流程
  ├─ check_act (参与上下文传递链)
  ├─ 条件网关
  │   ├─ success_act (is_remote_rewritable=True)
  │   └─ failed_act (is_remote_rewritable=True)
  └─ ...
```

## 核心问题

### 上下文传递链机制

在bamboo-engine中，`RewritableNodeOutput`需要通过`source_act`（即`rewritable_node_source_keys`）来构建上下文传递链：

```python
# builder.py:286-287
self.global_data.inputs["${trans_data}"] = RewritableNode(
    source_act=self.rewritable_node_source_keys,  # ← 如果这个列表为空！
    type=Var.SPLICE, 
    value=init_trans_data_class  # SingleApplyManualContext()
)
```

### 子流程参数传递

子流程通过params从父流程获取trans_data：

```python
# builder.py:353
sub_params = Params({"${trans_data}": Var(type=Var.SPLICE, value="${trans_data}")})
```

**如果父流程的trans_data未正确初始化（因为rewritable_node_source_keys为空），子流程获取到的就是错误的值（字符串而不是对象）。**

## 解决方案

### 修改内容

移除MySQL安装子流程之后、回档子流程之前的所有节点的`is_remote_rewritable=True`设置，确保它们参与上下文传递链：

1. **屏蔽告警节点**（第182-197行）
   - 移除：`is_remote_rewritable=True`
   - 原因：需要参与上下文传递链

2. **更新任务状态-deploy_success节点**（第198-206行）
   - 移除：`is_remote_rewritable=True`
   - 原因：需要参与上下文传递链

3. **创建目录节点**（第230-234行）
   - 移除：`is_remote_rewritable=True`
   - 原因：需要参与上下文传递链

### 保持不变

1. **条件网关中的分支节点**（success_act/failed_act）
   - 保持：`is_remote_rewritable=True`
   - 原因：避免干扰主流程上下文传递链

2. **最后的更新任务状态节点**（第365-373行）
   - 保持：`is_remote_rewritable=True`
   - 原因：在所有操作之后，不影响前面的流程

## 修复后的流程

```
MySQLRollbackExerciseFlow (父流程)
  ├─ MySQL安装子流程 (SubBuilder)
  │   └─ 安装MySQL实例 ✅ trans_data正确为SingleApplyManualContext对象
  ├─ 屏蔽告警 ✅ 参与上下文传递链
  ├─ 更新任务状态-deploy_success ✅ 参与上下文传递链
  ├─ 创建目录 ✅ 参与上下文传递链
  ├─ 回档子流程
  ├─ check_act ✅ 参与上下文传递链
  ├─ 条件网关
  │   ├─ success_act (is_remote_rewritable=True) ✅ 不干扰主链
  │   └─ failed_act (is_remote_rewritable=True) ✅ 不干扰主链
  └─ 更新任务状态-resource_return_success (is_remote_rewritable=True) ✅ 不影响前面
```

## 关键理解

### is_remote_rewritable的作用

- **False（默认）**：节点加入`rewritable_node_source_keys`，参与上下文传递链
- **True**：节点不加入列表，不参与上下文传递链

### 何时使用is_remote_rewritable=True

1. **条件网关的分支节点**：避免干扰主流程
2. **流程末尾的节点**：不影响前面的上下文传递
3. **不需要上下文的独立节点**：纯操作类节点

### 何时不使用is_remote_rewritable=True

1. **需要传递上下文的节点链**：确保上下文正确传递
2. **在子流程之前的节点**：确保子流程能获取正确的上下文
3. **条件网关的source_act**：必须参与上下文传递链

## 验证方法

修复后应该：
- ✅ 无 `'str' object has no attribute 'time_zone_info'` 错误
- ✅ MySQL安装子流程正常执行
- ✅ 回档子流程正常执行
- ✅ 条件网关正确分支
- ✅ 任务状态正确更新

## 测试命令

```bash
cd /data/project/blueking-dbm/dbm-ui
conda activate dbm-dev && source /root/set_dbm_env.sh
python manage.py test backend.flow.signal.test_mysql_rollback_exercise_handler -v 2
```

## 经验教训

1. **理解上下文传递链的构建机制**：RewritableNode依赖source_act列表
2. **谨慎使用is_remote_rewritable=True**：确保有足够的节点参与上下文传递
3. **子流程的上下文来自父流程**：父流程的trans_data必须正确初始化
4. **诊断方法**：检查rewritable_node_source_keys是否为空

## 文件修改清单

- ✅ `dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py`
  - 第197行：移除屏蔽告警节点的`is_remote_rewritable=True`
  - 第206行：移除更新任务状态节点的`is_remote_rewritable=True`
  - 第234行：移除创建目录节点的`is_remote_rewritable=True`

---

**修复完成时间**: 2025-10-22
**修复状态**: ✅ 已完成，等待测试验证

