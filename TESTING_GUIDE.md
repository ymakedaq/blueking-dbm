# MySQL回档演练流程分支网关改造 - 测试指南

## 测试环境准备

### 1. 环境激活
```bash
conda activate dbm-dev && source /root/set_dbm_env.sh
```

### 2. 进入项目目录
```bash
cd /data/project/blueking-dbm/dbm-ui
```

## 单元测试

### 测试新建的组件

#### 测试 MarkRollbackStatusComponent
```python
# 创建测试文件: backend/flow/plugins/components/collections/mysql/test_mark_rollback_status.py
from unittest.mock import Mock
from backend.flow.plugins.components.collections.mysql.mark_rollback_status import MarkRollbackStatusService

def test_mark_rollback_status():
    service = MarkRollbackStatusService()
    data = Mock()
    trans_data = Mock()
    data.get_one_of_inputs = Mock(return_value=trans_data)
    
    result = service._execute(data, None)
    
    assert result == True
    assert trans_data.rollback_status == "success"
    assert data.outputs.trans_data == trans_data
    print("✅ MarkRollbackStatusService 测试通过")

if __name__ == "__main__":
    test_mark_rollback_status()
```

#### 测试 CheckRollbackStatusComponent
```python
# 创建测试文件: backend/flow/plugins/components/collections/mysql/test_check_rollback_status.py
from unittest.mock import Mock
from backend.flow.plugins.components.collections.mysql.check_rollback_status import CheckRollbackStatusService

def test_check_rollback_status_success():
    """测试检测到成功标记的情况"""
    service = CheckRollbackStatusService()
    data = Mock()
    trans_data = Mock()
    trans_data.rollback_status = "success"
    data.get_one_of_inputs = Mock(return_value=trans_data)
    
    result = service._execute(data, None)
    
    assert result == True
    assert trans_data.rollback_status == "success"
    print("✅ CheckRollbackStatusService (成功场景) 测试通过")

def test_check_rollback_status_failed():
    """测试未检测到成功标记的情况"""
    service = CheckRollbackStatusService()
    data = Mock()
    trans_data = Mock()
    # 没有设置 rollback_status
    data.get_one_of_inputs = Mock(return_value=trans_data)
    
    result = service._execute(data, None)
    
    assert result == True
    assert trans_data.rollback_status == "failed"
    print("✅ CheckRollbackStatusService (失败场景) 测试通过")

if __name__ == "__main__":
    test_check_rollback_status_success()
    test_check_rollback_status_failed()
```

## 集成测试

### 测试完整的回档演练流程

#### 测试命令
```bash
cd /data/project/blueking-dbm/dbm-ui
python manage.py test backend.flow.signal.test_mysql_rollback_exercise_handler -v 2
```

### 手动测试步骤

#### 测试场景1: 正常回档成功

1. **准备测试数据**
   - 创建一个测试集群
   - 准备有效的备份数据

2. **执行回档演练**
   ```python
   from backend.flow.engine.bamboo.scene.mysql.mysql_rollback_exercise import MySQLRollbackExerciseFlow
   
   # 构造测试数据
   test_data = {
       "ticket_type": "MYSQL_ROLLBACK_EXERCISE",
       "exercise_cluster_id": <集群ID>,
       "backup_id": "<备份ID>",
       "rollback_host": {
           "ip": "x.x.x.x",
           "bk_host_id": <主机ID>,
           "bk_cloud_id": 0
       },
       "bk_biz_id": <业务ID>,
       "created_by": "admin",
       "uid": <单据ID>,
   }
   
   # 执行流程
   flow = MySQLRollbackExerciseFlow(root_id="test_root_id", data=test_data)
   flow.run()
   ```

3. **验证结果**
   - 检查流程是否成功完成
   - 检查任务状态是否为`recover_success`
   - 检查后续资源回收流程是否正常执行
   
   ```python
   from backend.db_periodic_task.models import MySQLBackupRecoverTask
   
   task = MySQLBackupRecoverTask.objects.get(task_id="test_root_id")
   assert task.task_status == "recover_success"
   assert task.status == True
   print("✅ 正常回档成功场景测试通过")
   ```

#### 测试场景2: 回档失败但流程继续

1. **模拟回档失败**
   - 使用无效的备份ID
   - 或者模拟磁盘空间不足
   - 或者手动在流程执行过程中强制失败某个节点

2. **执行回档演练**（同上）

3. **验证结果**
   - 检查流程是否继续执行（未因子流程失败而中断）
   - 检查任务状态是否为`recover_failed`
   - 检查后续资源回收流程是否正常执行
   
   ```python
   from backend.db_periodic_task.models import MySQLBackupRecoverTask
   
   task = MySQLBackupRecoverTask.objects.get(task_id="test_root_id")
   assert task.task_status == "recover_failed"
   assert task.status == False  # 失败状态
   print("✅ 回档失败但流程继续场景测试通过")
   ```

## 日志验证

### 查看条件网关执行日志

```bash
# 查看flow日志
tail -f /data/logs/dbm/flow.log | grep -E "检查回档执行状态|标记回档执行成功|更新演练任务状态"
```

**期望日志输出**:

**成功场景**:
```
[INFO] 标记回档执行成功
[INFO] 回档执行成功，标记状态为success
[INFO] 检查回档执行状态
[INFO] 检测到回档执行成功标记，状态为success
[INFO] 更新演练任务状态为成功
```

**失败场景**:
```
[INFO] 检查回档执行状态
[WARNING] 未检测到回档成功标记，判定为失败，状态设置为failed
[INFO] 更新演练任务状态为失败
```

## 数据库验证

### 验证TaskStatus更新

```sql
-- 查看任务状态
SELECT task_id, task_status, status, phase, create_at, update_at 
FROM db_periodic_task_mysqlbackuprecovtask 
WHERE task_id = 'test_root_id';
```

**期望结果**:
- 成功场景: `task_status = 'recover_success'`, `status = 1`
- 失败场景: `task_status = 'recover_failed'`, `status = 0`

## Bamboo Engine 流程树验证

### 查看流程树状态

```python
from backend.flow.engine.bamboo.engine import BambooEngine

engine = BambooEngine(root_id="test_root_id")
tree = engine.get_pipeline_tree_states()

# 打印流程树
import json
print(json.dumps(tree, indent=2, ensure_ascii=False))
```

**关键验证点**:
1. 子流程`tendbha_rollback_data_sub_flow`的执行状态
2. `CheckRollbackStatusComponent`节点是否执行
3. 条件网关是否正确选择了分支
4. 成功/失败分支节点的执行状态
5. 后续资源回收流程是否正常执行

## 性能测试

### 测试条件网关的性能影响

```python
import time
from backend.flow.engine.bamboo.scene.mysql.mysql_rollback_exercise import MySQLRollbackExerciseFlow

# 记录执行时间
start_time = time.time()
flow = MySQLRollbackExerciseFlow(root_id="test_root_id", data=test_data)
flow.run()
end_time = time.time()

print(f"流程执行时间: {end_time - start_time} 秒")
```

**预期**: 添加条件网关后，流程执行时间应该没有明显增加（< 1秒差异）

## 回归测试

### 验证现有功能不受影响

1. **正常回档演练流程**
   - 所有原有功能应该正常工作
   - 不应该有任何功能退化

2. **其他MySQL流程**
   - 确保修改只影响回档演练流程
   - 其他流程（如部署、升级等）不受影响

## 故障排查

### 常见问题

#### 1. 上下文变量未传递
**症状**: `CheckRollbackStatusComponent`中无法读取`rollback_status`

**排查**:
- 检查`MarkRollbackStatusComponent`是否设置了`write_payload_var="rollback_status"`
- 检查是否错误设置了`is_remote_rewritable=True`

#### 2. 条件网关未正确分支
**症状**: 无论成功失败都走同一个分支

**排查**:
- 检查`conditions_param="rollback_status"`是否正确
- 检查条件表达式`== "success"`和`== "failed"`是否正确
- 查看日志中`rollback_status`的实际值

#### 3. 流程在子流程失败后中断
**症状**: 子流程失败后，后续节点未执行

**排查**:
- 检查`CheckRollbackStatusComponent`是否始终返回`True`
- 检查bamboo engine的错误日志

## 测试检查清单

- [ ] MarkRollbackStatusComponent 单元测试通过
- [ ] CheckRollbackStatusComponent 单元测试通过（成功和失败两种场景）
- [ ] TaskStatus.RECOVER_FAILED 枚举已添加
- [ ] 子流程末尾成功标记节点正确添加
- [ ] 主流程条件网关正确配置
- [ ] 回档成功场景测试通过
- [ ] 回档失败但流程继续场景测试通过
- [ ] 日志输出符合预期
- [ ] 数据库状态更新正确
- [ ] Bamboo Engine流程树状态正确
- [ ] 性能无明显下降
- [ ] 现有功能回归测试通过

## 测试报告模板

```markdown
# MySQL回档演练流程分支网关测试报告

## 测试环境
- 测试日期: YYYY-MM-DD
- 测试人员: XXX
- 环境: 开发/测试/预生产

## 测试结果

### 单元测试
- [ ] MarkRollbackStatusComponent: 通过/失败
- [ ] CheckRollbackStatusComponent: 通过/失败

### 集成测试
- [ ] 回档成功场景: 通过/失败
- [ ] 回档失败场景: 通过/失败
- [ ] 流程继续执行: 通过/失败

### 回归测试
- [ ] 现有功能: 通过/失败

## 发现的问题
1. 问题描述
2. 重现步骤
3. 影响程度
4. 解决方案

## 测试结论
- [ ] 功能完全符合预期
- [ ] 建议上线
```

