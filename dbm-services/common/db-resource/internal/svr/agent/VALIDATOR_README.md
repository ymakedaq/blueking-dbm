# 验证 Agent 功能说明

## 概述

验证 Agent 是资源池分析系统的新增功能，用于检查主分析 Agent 生成结果的**逻辑一致性和合理性**。采用**迭代验证模式**：主 Agent 完成初步分析后，验证 Agent 检查结果的合理性，如果发现逻辑问题，主 Agent 会根据反馈进行改进。

## 功能特性

### 验证维度

1. **原因-建议匹配**：检查失败原因和建议措施是否对应
   - 磁盘问题 → adjust_disk
   - 标签问题 → add_labels
   - 资源类型问题 → change_rstype
   - 规格问题 → adjust_spec 或 add_resources

2. **数量逻辑**：检查资源数量计算是否合理
   - 申请数 N，可用数 M，建议补充应至少 (N-M) 台
   - 预测数量不能小于申请数量

3. **优先级合理性**：检查建议的优先级排序是否合理
   - 高影响因素的建议应该有更高优先级

4. **禁止建议检查**：确保没有违反系统规则
   - 不能建议降低申请数量
   - 不能建议放宽亲和性
   - 不能建议更换地域

5. **完整性检查**：确保分析结果的基本完整性
   - 必须有摘要
   - 必须有失败原因
   - 必须有改进建议

## 配置说明

在 `conf/config.yaml` 中配置验证器：

```yaml
llm:
  enabled: true
  provider: bk_ai  # 或 openai / azure
  
  # 主模型配置
  bk_ai:
    model: "dsv32-thinking"  # 主分析使用的模型
    ...
  
  agent:
    max_iterations: 15
    timeout_seconds: 360
    validator:
      enabled: true              # 是否启用验证器
      max_refinements: 2         # 最大改进次数
      min_confidence_score: 70   # 最低置信度要求（0-100）
      use_custom_model: false    # 是否为验证器使用独立的模型
      model: "dsv32"             # 验证器使用的模型名称（仅当 use_custom_model=true 时生效）
```

### 配置参数说明

- **enabled**: 是否启用验证功能（默认 false）
- **max_refinements**: 验证失败后允许主 Agent 重新分析的最大次数（默认 2）
- **min_confidence_score**: 最低置信度要求，低于此分数的分析结果将被标记为不通过（默认 70）
- **use_custom_model**: 是否为验证器使用独立的模型配置（默认 false）
- **model**: 验证器使用的模型名称，使用主配置的提供商和其他参数（仅当 use_custom_model=true 时生效）

### 为什么使用独立模型？

使用独立模型配置可以带来以下好处：

1. **性能优化**：验证任务相对简单，可以使用更快的模型（如从 dsv32-thinking 换成 dsv32）
2. **成本控制**：使用更便宜的模型进行验证，节省 API 调用成本
3. **灵活性**：可以针对不同任务使用不同的模型
4. **可靠性**：主分析使用强模型保证质量，验证使用快速模型提高效率

### 配置示例

#### 场景 1：使用相同模型（默认）

```yaml
agent:
  validator:
    enabled: true
    max_refinements: 2
    min_confidence_score: 70
    use_custom_model: false  # 使用主配置的模型
```

#### 场景 2：使用更快的验证模型（推荐）

```yaml
llm:
  provider: "bk_ai"
  bk_ai:
    model: "dsv32-thinking"  # 主分析使用 thinking 版本
    
  agent:
    validator:
      enabled: true
      use_custom_model: true
      model: "dsv32"  # 验证使用标准版本，更快
```

#### 场景 3：OpenAI 提供商

```yaml
llm:
  provider: "openai"
  openai:
    model: "gpt-4"  # 主分析使用 GPT-4
    
  agent:
    validator:
      enabled: true
      use_custom_model: true
      model: "gpt-4o-mini"  # 验证使用更快的模型
```

### 配置说明

- **简化配置**：验证器只需指定模型名称，自动继承主配置的提供商、API Key、Base URL 等参数
- **灵活切换**：通过 `use_custom_model` 开关快速启用/禁用独立模型
- **向后兼容**：不启用 `use_custom_model` 时，完全使用主模型配置
```

## 使用方式

### API 调用

当验证器启用后，调用 `/resource/analyze` API 会自动进行验证：

```bash
curl -X POST http://localhost:80/resource/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "apply_params": {
      "bk_cloud_id": 0,
      "city": "深圳",
      "count": 10,
      ...
    }
  }'
```

### 响应格式

API 响应会包含验证信息：

```json
{
  "code": 0,
  "data": {
    "summary": "分析摘要...",
    "reasons": [...],
    "suggestions": [...],
    "markdown_text": "# 资源申请分析报告\n...",
    "validation": {
      "passed": true,
      "confidence_score": 85,
      "issues_count": 0
    }
  }
}
```

## 验证报告格式

### ValidationReport 结构

```go
type ValidationReport struct {
    Passed          bool              // 是否通过验证
    ConfidenceScore int               // 置信度评分（0-100）
    Issues          []ValidationIssue // 发现的问题列表
    Suggestions     []string          // 改进建议
}

type ValidationIssue struct {
    Category    string // 问题类别
    Severity    string // 严重性（high/medium/low）
    Description string // 问题描述
    Field       string // 具体问题字段
}
```

## 置信度评分机制

- 初始分数：100 分
- 高严重性问题：-20 分
- 中等严重性问题：-10 分
- 低严重性问题：-5 分
- 最低分数：0 分

## 迭代验证流程

```
1. 主 Agent 生成初步分析结果
   ↓
2. 验证 Agent 检查结果
   ↓
3. 验证通过？
   - 是：返回结果
   - 否：继续第 4 步
   ↓
4. 达到最大改进次数？
   - 是：返回结果（带验证警告）
   - 否：将验证反馈发送给主 Agent
   ↓
5. 主 Agent 根据反馈改进分析
   ↓
6. 返回第 2 步
```

## 常见问题

### Q: 验证器会显著增加分析耗时吗？

A: 验证本身非常快速（通常 < 1 秒）。如果需要改进，会增加一次 LLM 调用的时间。通常情况下，验证器能在第一次或第二次改进后通过。

### Q: 可以禁用验证功能吗？

A: 可以。在配置文件中设置 `validator.enabled: false` 即可禁用。禁用后系统行为与之前完全一致。

### Q: 验证不通过会导致 API 调用失败吗？

A: 不会。验证不通过只是标记分析结果可能存在逻辑问题，仍会返回分析结果。用户可以查看验证报告了解具体问题。

### Q: 如何调整验证的严格程度？

A: 通过调整 `min_confidence_score` 参数：
- 设置为 90：非常严格，只允许几乎完美的分析结果
- 设置为 70：标准严格度（推荐）
- 设置为 50：较宽松，允许更多小问题

## 开发指南

### 添加新的验证规则

在 `validator_tool.go` 中添加新的验证方法：

```go
func (v *ResourceAnalysisValidator) checkMyNewRule(result *AnalysisResult, report *ValidationReport) {
    // 实现验证逻辑
    if /* 验证失败 */ {
        report.Issues = append(report.Issues, ValidationIssue{
            Category:    "my_category",
            Severity:    "high",
            Description: "问题描述",
            Field:       "相关字段",
        })
    }
}
```

然后在 `ValidateAnalysisResult` 方法中调用：

```go
func (v *ResourceAnalysisValidator) ValidateAnalysisResult(result *AnalysisResult) *ValidationReport {
    // ... 现有验证 ...
    
    // 添加新的验证
    v.checkMyNewRule(result, report)
    
    // ... 剩余代码 ...
}
```

## 测试

运行单元测试：

```bash
cd dbm-services/common/db-resource
go test ./internal/svr/agent -v -run TestValidate
```

## 相关文件

- `internal/svr/agent/validator_tool.go`: 验证逻辑实现
- `internal/svr/agent/validator_tool_test.go`: 单元测试
- `internal/svr/agent/executor.go`: 迭代验证流程
- `internal/svr/agent/analyzer.go`: 验证入口
- `internal/config/config.go`: 配置结构
- `internal/controller/analyze/analyze.go`: API 接口
- `conf/config.yaml`: 配置文件

## 版本历史

- v1.0.0 (2024-01-XX): 初始版本
  - 实现基础验证功能
  - 支持迭代改进
  - 提供置信度评分
