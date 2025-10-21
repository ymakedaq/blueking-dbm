# 最终验证清单 - 上下文传递问题修复

## ✅ 已完成的修改

### 核心修复

**问题**：MySQL安装子流程中trans_data变成字符串，导致无法访问time_zone_info属性

**根本原因**：父流程在MySQL安装子流程之前的所有节点都设置了`is_remote_rewritable=True`，导致`rewritable_node_source_keys`为空，trans_data未正确初始化

**修复方案**：移除3个关键节点的`is_remote_rewritable=True`设置

### 具体修改

#### 文件：`dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py`

1. **屏蔽告警节点**（约第182-197行）
   ```python
   # 修改前
   pipeline.add_act(
       act_name=_("屏蔽集群 {} 告警12小时").format(cluster_class.name),
       ...
       is_remote_rewritable=True,  # ← 移除这行
   )
   
   # 修改后
   pipeline.add_act(
       act_name=_("屏蔽集群 {} 告警12小时").format(cluster_class.name),
       ...
   )
   ```

2. **更新任务状态-deploy_success节点**（约第198-206行）
   ```python
   # 修改前
   pipeline.add_act(
       act_name=_("更新演练任务状态"),
       ...
       is_remote_rewritable=True,  # ← 移除这行
   )
   
   # 修改后
   pipeline.add_act(
       act_name=_("更新演练任务状态"),
       ...
   )
   ```

3. **创建目录节点**（约第230-234行）
   ```python
   # 修改前
   pipeline.add_act(
       act_name=_("创建目录 {}".format(mycluster["file_target_path"])),
       ...
       is_remote_rewritable=True,  # ← 移除这行
   )
   
   # 修改后
   pipeline.add_act(
       act_name=_("创建目录 {}".format(mycluster["file_target_path"])),
       ...
   )
   ```

## 代码检查清单

- [x] 屏蔽告警节点：**已移除**`is_remote_rewritable=True`
- [x] 更新任务状态-deploy_success节点：**已移除**`is_remote_rewritable=True`  
- [x] 创建目录节点：**已移除**`is_remote_rewritable=True`
- [x] check_act节点：**没有**`is_remote_rewritable=True`（正确）
- [x] success_act节点：**保持**`is_remote_rewritable=True`（正确）
- [x] failed_act节点：**保持**`is_remote_rewritable=True`（正确）
- [x] Lint检查：仅有预存在的Django导入警告，无新增错误

## 预期效果

修复后，上下文传递链应该是：

```
Pipeline初始化：rewritable_node_source_keys = [
    {source_act: MySQL安装子流程的某个内部节点, source_key: "trans_data"},
    {source_act: 屏蔽告警节点, source_key: "trans_data"},
    {source_act: 更新任务状态节点, source_key: "trans_data"},
    {source_act: 创建目录节点, source_key: "trans_data"},
    {source_act: 回档子流程的某个内部节点, source_key: "trans_data"},
    {source_act: check_act, source_key: "trans_data"},
    ...
]
```

**关键**：列表不为空，trans_data能正确初始化为`SingleApplyManualContext()`对象

## 快速测试

```bash
# 1. 激活环境
cd /data/project/blueking-dbm/dbm-ui
conda activate dbm-dev && source /root/set_dbm_env.sh

# 2. 运行测试
python manage.py test backend.flow.signal.test_mysql_rollback_exercise_handler -v 2

# 3. 或者执行实际的回档演练任务
```

## 验证要点

### ✅ 应该看到

1. **MySQL安装成功**
   ```
   [INFO] [安装MySQL实例] 任务正在执行
   [INFO] [安装MySQL实例] 任务调度成功
   [INFO] [安装MySQL实例] 该节点需要获取执行后日志，赋值到流程上下文
   ✅ 无错误，成功写入time_zone_info到上下文
   ```

2. **流程正常执行**
   - 屏蔽告警成功
   - 更新任务状态成功
   - 创建目录成功
   - 回档子流程成功
   - 条件网关正确分支
   - 资源回收成功

### ❌ 不应该看到

```
[ERROR] [写入上下文结果失败] failed: 'str' object has no attribute 'time_zone_info'
[ERROR] [安装MySQL实例] 获取执行后写入流程上下文失败
```

## 如果还有问题

### 诊断步骤

1. **添加调试日志**
   在`builder.py:286`行前添加：
   ```python
   print(f"DEBUG: rewritable_node_source_keys = {self.rewritable_node_source_keys}")
   print(f"DEBUG: init_trans_data_class = {init_trans_data_class}")
   ```

2. **检查流程树**
   ```python
   from backend.flow.engine.bamboo.engine import BambooEngine
   engine = BambooEngine(root_id="<your_root_id>")
   tree = engine.get_pipeline_tree_states()
   print(tree)
   ```

3. **回滚验证**
   如果问题依然存在，可能是bamboo-engine版本问题或其他未知因素

## 相关文档

- `DIAGNOSIS_ANALYSIS.md` - 问题诊断分析
- `CONTEXT_FIX_SOLUTION.md` - 详细解决方案
- `FINAL_FIX_SUMMARY.md` - 之前的修复总结
- `CRITICAL_FIX_SUMMARY.md` - 关键问题分析

## 总结

这次修复解决了bamboo-engine上下文传递链的根本问题：

1. **问题**：过多节点设置`is_remote_rewritable=True`导致上下文传递链为空
2. **修复**：移除关键节点的该设置，确保上下文传递链完整
3. **原则**：只有在不需要传递上下文的场景才使用`is_remote_rewritable=True`

---

**修复时间**: 2025-10-22  
**修复状态**: ✅ 代码已修改，等待测试验证  
**预期结果**: 上下文传递正常，流程成功执行

