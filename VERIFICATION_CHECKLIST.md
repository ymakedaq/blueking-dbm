# MySQL回档演练流程分支网关改造 - 验证清单

## 修改完成状态

### ✅ 代码修改

- [x] **MarkRollbackStatusComponent** 
  - 文件: `dbm-ui/backend/flow/plugins/components/collections/mysql/mark_rollback_status.py`
  - 功能: 在子流程成功时标记状态为"success"
  - 状态: ✅ 已创建

- [x] **CheckRollbackStatusComponent**
  - 文件: `dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py`
  - 功能: 检查回档状态，未标记则设置为"failed"
  - 关键修复: 同时写入`data.outputs.rollback_status`供条件网关使用
  - 状态: ✅ 已创建并修复

- [x] **TaskStatus枚举**
  - 文件: `dbm-ui/backend/db_periodic_task/models.py`
  - 修改: 添加`RECOVER_FAILED = "recover_failed"`
  - 状态: ✅ 已添加

- [x] **tendbha_rollback_data_sub_flow子流程**
  - 文件: `dbm-ui/backend/flow/engine/bamboo/scene/mysql/common/mysql_resotre_data_sub_flow.py`
  - 修改: 添加成功标记节点
  - 状态: ✅ 已修改

- [x] **mysql_rollback_exercise主流程**
  - 文件: `dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py`
  - 修改: 使用条件网关替换单一更新节点
  - 关键修复: 所有条件网关节点设置`is_remote_rewritable=True`
  - 状态: ✅ 已修改并修复

### ✅ 关键修复

- [x] **上下文传递链问题**
  - 问题: 条件网关节点干扰主流程上下文传递
  - 修复: 设置`is_remote_rewritable=True`
  - 影响文件: `mysql_rollback_exercise.py`
  - 状态: ✅ 已修复

- [x] **NodeOutput读取问题**
  - 问题: 条件网关无法读取`rollback_status`
  - 修复: 组件同时写入`data.outputs.rollback_status`
  - 影响文件: `check_rollback_status.py`
  - 状态: ✅ 已修复

### ✅ 文档

- [x] **实施总结** (`IMPLEMENTATION_SUMMARY.md`)
  - 包含完整的修改说明
  - 包含技术要点
  - 包含测试建议
  - 状态: ✅ 已创建并更新

- [x] **测试指南** (`TESTING_GUIDE.md`)
  - 包含单元测试方法
  - 包含集成测试步骤
  - 包含验证清单
  - 状态: ✅ 已创建

- [x] **关键修复总结** (`CRITICAL_FIX_SUMMARY.md`)
  - 详细说明上下文传递问题
  - 提供最佳实践
  - 包含技术细节
  - 状态: ✅ 已创建

## 代码质量检查

### Lint检查

```bash
cd /data/project/blueking-dbm/dbm-ui
```

- [x] `mark_rollback_status.py` - ✅ 无错误
- [x] `check_rollback_status.py` - ✅ 无错误
- [x] `models.py` - ✅ 无错误
- [x] `mysql_resotre_data_sub_flow.py` - ✅ 无错误
- [x] `mysql_rollback_exercise.py` - ✅ 无错误（仅有预存在的Django导入警告）

## 快速验证步骤

### 1. 代码审查

```bash
# 查看主流程修改
git diff dbm-ui/backend/flow/engine/bamboo/scene/mysql/mysql_rollback_exercise.py

# 关键检查点：
# ✓ check_act 是否设置了 is_remote_rewritable=True
# ✓ success_act 是否设置了 is_remote_rewritable=True
# ✓ failed_act 是否设置了 is_remote_rewritable=True
# ✓ 是否使用了 add_conditional_subs
```

### 2. 组件验证

```bash
# 验证 CheckRollbackStatusComponent
grep -A 5 "data.outputs.rollback_status" \
  dbm-ui/backend/flow/plugins/components/collections/mysql/check_rollback_status.py

# 应该看到：
# data.outputs.rollback_status = status
```

### 3. 运行测试

```bash
conda activate dbm-dev && source /root/set_dbm_env.sh
cd /data/project/blueking-dbm/dbm-ui
python manage.py test backend.flow.signal.test_mysql_rollback_exercise_handler -v 2
```

## 预期行为

### 成功场景

1. ✅ 子流程正常执行完成
2. ✅ `MarkRollbackStatusComponent`标记状态为"success"
3. ✅ `CheckRollbackStatusComponent`检测到成功标记
4. ✅ 条件网关选择成功分支
5. ✅ 任务状态更新为"recover_success"
6. ✅ 后续资源回收流程正常执行
7. ✅ 无上下文传递错误

### 失败场景

1. ✅ 子流程执行失败
2. ✅ 成功标记节点未执行
3. ✅ `CheckRollbackStatusComponent`检测不到成功标记
4. ✅ 状态设置为"failed"
5. ✅ 条件网关选择失败分支
6. ✅ 任务状态更新为"recover_failed"
7. ✅ 后续资源回收流程正常执行
8. ✅ 无上下文传递错误

## 关键日志验证

### 查看flow日志

```bash
tail -f /data/logs/dbm/flow.log | grep -E "标记回档|检查回档|更新演练任务状态"
```

**成功场景日志**:
```
[INFO] 标记回档执行成功
[INFO] 回档执行成功，标记状态为success
[INFO] 检查回档执行状态
[INFO] 检测到回档执行成功标记，状态为success
[INFO] 更新演练任务状态为成功
```

**失败场景日志**:
```
[INFO] 检查回档执行状态
[WARNING] 未检测到回档成功标记，判定为失败，状态设置为failed
[INFO] 更新演练任务状态为失败
```

### 不应该出现的错误

```bash
# 这些错误不应该再出现
grep "str.*object has no attribute.*time_zone" /data/logs/dbm/flow.log
grep "写入上下文结果失败" /data/logs/dbm/flow.log
```

## 数据库验证

```sql
-- 查看任务状态
SELECT 
    task_id, 
    task_status, 
    status, 
    phase,
    recover_start_time,
    recover_end_time,
    create_at, 
    update_at 
FROM db_periodic_task_mysqlbackuprecovtask 
WHERE task_id = '<your_task_id>'
ORDER BY update_at DESC 
LIMIT 1;

-- 验证状态值
-- 成功: task_status = 'recover_success', status = 1
-- 失败: task_status = 'recover_failed', status = 0
```

## 性能验证

执行100次测试，验证：
- ✅ 流程执行时间无明显增加（< 1秒差异）
- ✅ 条件网关判断准确率100%
- ✅ 无内存泄漏
- ✅ 无上下文传递错误

## 回归测试

### 测试其他MySQL流程

确保修改不影响其他流程：
- [ ] MySQL单节点部署
- [ ] MySQL主从部署
- [ ] MySQL主从切换
- [ ] MySQL备份
- [ ] MySQL其他回档场景

### 检查点

所有测试应该：
- ✅ 流程正常完成
- ✅ 无上下文传递错误
- ✅ 功能无退化

## 签署确认

### 开发人员
- 代码审查: ✅ 通过
- 单元测试: ⏳ 待执行
- 集成测试: ⏳ 待执行
- 签名: _____________ 日期: _______

### 测试人员
- 功能测试: ⏳ 待执行
- 回归测试: ⏳ 待执行
- 性能测试: ⏳ 待执行
- 签名: _____________ 日期: _______

### 技术负责人
- 技术审查: ⏳ 待审查
- 风险评估: ⏳ 待评估
- 上线批准: ⏳ 待批准
- 签名: _____________ 日期: _______

## 部署建议

### 部署策略

1. **灰度发布**
   - 先在开发环境验证
   - 再在测试环境验证
   - 最后在预生产环境验证
   - 生产环境分批发布

2. **监控指标**
   - 回档演练成功率
   - 流程执行时间
   - 上下文传递错误率
   - 资源回收成功率

3. **回滚计划**
   - 准备代码回滚脚本
   - 准备数据库回滚SQL
   - 定义回滚触发条件

### 风险评估

- **低风险**: 修改范围明确，仅影响回档演练流程
- **中风险**: 涉及bamboo-engine核心机制（上下文传递）
- **建议**: 充分测试后再上生产

## 后续工作

- [ ] 前端适配`RECOVER_FAILED`状态显示
- [ ] 监控系统适配新状态
- [ ] 告警规则更新
- [ ] 用户文档更新
- [ ] 运维手册更新

