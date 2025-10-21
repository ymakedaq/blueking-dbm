# 快速验证指南

## ✅ 最终修复已完成

### 核心修复（关键！）

**问题**: `check_act`错误地设置了`is_remote_rewritable=True`，导致上下文传递链断裂

**修复**: 移除`check_act`的`is_remote_rewritable=True`设置

```python
# ✅ 正确配置
check_act = pipeline.add_act(
    act_name=_("检查回档执行状态"),
    act_component_code=CheckRollbackStatusComponent.code,
    kwargs={},
    write_payload_var="rollback_status",
    # 不设置is_remote_rewritable（默认False），让它参与上下文传递链
    extend=False,
)
```

## 快速测试

```bash
cd /data/project/blueking-dbm/dbm-ui
conda activate dbm-dev && source /root/set_dbm_env.sh
python manage.py test backend.flow.signal.test_mysql_rollback_exercise_handler -v 2
```

## 期望结果

✅ **不应该再出现**以下错误：
```
[ERROR] [写入上下文结果失败] failed: 'str' object has no attribute 'time_zone_info'
[ERROR] [安装MySQL实例] 获取执行后写入流程上下文失败
```

✅ **应该看到**：
- 回档演练流程正常执行
- 条件网关正确分支（成功/失败）
- 任务状态正确更新
- 后续资源回收流程正常执行

## 代码检查清单

- [x] `check_act`: **没有**`is_remote_rewritable=True`
- [x] `success_act`: **有**`is_remote_rewritable=True`
- [x] `failed_act`: **有**`is_remote_rewritable=True`
- [x] `CheckRollbackStatusComponent`: 同时写入`data.outputs.rollback_status`

## 如果还有问题

1. 检查文件位置：
   - 主流程：`dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py`（第252-283行）
   - 检查组件：`dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py`（第46-47行）

2. 查看详细日志：
   ```bash
   tail -f /data/logs/dbm/flow.log | grep -E "检查回档|time_zone|上下文"
   ```

3. 参考文档：
   - `FINAL_FIX_SUMMARY.md` - 最终修复总结
   - `CRITICAL_FIX_SUMMARY.md` - 问题详细分析
   - `IMPLEMENTATION_SUMMARY.md` - 完整实施文档

## 核心原则

**使用条件网关时**：
1. **source_act（判断节点）**：不设置`is_remote_rewritable`，必须参与上下文传递链
2. **分支节点**：设置`is_remote_rewritable=True`，不干扰主流程

**原因**：`add_conditional_subs`内部会通过`self.pipe.extend(source_act)`将source_act连接到主流程，source_act必须在上下文传递链中才能正确传递上下文给后续节点。

---

**修复状态**: ✅ 完成  
**验证状态**: ⏳ 等待测试  
**最后更新**: 2025-10-21

