# DBM 资源申请 API v2 实现说明

## 概述

v2 API 实现了基于云字段（bk_cloud_region / bk_cloud_zone）的资源申请，完全不修改现有 v1 apply/pick 源文件。

## 新增文件

```
internal/controller/applyv2/
├── README.md              # 本文档
├── types.go               # v2 请求/响应类型定义
├── apply_v2.go            # v2 核心申请逻辑
├── types_test.go          # 类型方法测试（依赖配置）
└── validation_test.go     # 纯函数验证测试（无配置依赖）
```

路由注册：`internal/routers/routers.go` 已添加 v2 handler。

## 核心功能

### 1. 旧字段拒绝（Red Line R1-R4）

v2 API 会拒绝以下旧字段：
- `city` → 使用 `bk_cloud_region`
- `sub_zone` / `sub_zone_ids` → 使用 `bk_cloud_zone_list`
- `exclude_sub_zone_ids` → 使用 `exclude_bk_cloud_zone_list`
- `include_or_exclue` → v2 不支持
- `current_hosts[].sub_zone` → 使用 `bk_cloud_zone`

### 2. 亲和维度（affinity_dimension）

- **可选字段**，缺省默认为 `bk_cloud_zone`
- **MVP 白名单**：只允许空或 `bk_cloud_zone`
- 其他值（如 `rack_id`）会被拒单

### 3. 空维语义（Empty Dimension Rules）

- **current_hosts 主维为空**：整单 4xx 拒绝（调用方数据异常）
- **库存候选主维为空**：从候选集踢出，继续选机（不整单拒）
- 踢除后数量不足 → 业务失败（资源不足），不是参数 4xx

### 4. 路径 A 复用（Reuse Path A）

v2 复用现有选机逻辑，但**不使用**以下旧组件：
- ❌ `CycleApply`（完整生命周期）
- ❌ `MatchLocationSpec`（逻辑城转换）
- ❌ `GetIdcCityByLogicCity`（逻辑城 API）

v2 **复用**以下组件（零改动）：
- ✅ `PickInstanceBase`（内存亲和选机）
- ✅ `PickerDone`（选满检查）
- ✅ `PreselectedSatisfiedInstance`（Unused → Preselected）
- ✅ `LockReturnPickers`（Preselected → Used）

### 5. 内存适配（In-Memory Adaptation）

为了让 v1 选机逻辑正确工作，v2 在内存中做以下适配（**不回写数据库**）：
1. 将 `bk_cloud_zone` 值同时写入 `SubZone` 和 `SubZoneID`
2. 将 `current_hosts.bk_cloud_zone` 写入 `SubZone`

这样 v1 的分桶逻辑（按 SubZone）和快照逻辑（读 SubZoneID）都能正确处理云可用区。

### 6. 多组同位置（groups_in_same_location）

v2 自建外层协调，按云可用区固定主维尝试多组：
1. 枚举候选 zone（按优先级）
2. 每个 zone 尝试选满所有组
3. 任一组失败 → 回滚本批 Preselected，尝试下一个 zone
4. 全部失败 → 整单失败

### 7. 并发安全（R03 细则）

- **共池互斥**：与 v1 使用同一套锁（按 resource_type 或 global）
- **预选部分失败**：
  - `PreselectedSatisfiedInstance` 返回 `affectRows < 期望` 时
  - 立即查询本批中仍为 `Preselected` 的 host
  - **只回滚本请求确持有的 host**，禁止误伤他请求
- **后组失败 / 提交失败**：清理本请求累计的 Preselected 集合

## 接口契约

### 请求示例

```json
POST /v2/resource/apply

{
  "resource_type": "MySQL",
  "for_biz_id": 1,
  "details": [
    {
      "group_mark": "backend",
      "count": 2,
      "affinity": "CROS_SUBZONE",
      "affinity_dimension": "bk_cloud_zone",
      "location_spec": {
        "bk_cloud_region": "ap-chongqing",
        "bk_cloud_zone_list": ["ap-chongqing-1", "ap-chongqing-2"],
        "exclude_bk_cloud_zone_list": []
      },
      "current_hosts": [
        {
          "bk_host_id": 1001,
          "bk_cloud_zone": "ap-chongqing-1",
          "rack_id": "rack-a"
        }
      ]
    }
  ]
}
```

### 响应

成功响应保证包含：
- `bk_host_id`
- 主机的云字段（通过模型原生字段或映射）

## 运行测试

### 编译验证

```bash
cd dbm-services/common/db-resource
go build -o /tmp/db-resource ./
```

### 单元测试

单元测试依赖配置文件，需要在有配置的环境下运行：

```bash
# 在有配置文件的环境
go test ./internal/controller/applyv2 -v

# 或使用 mock 配置
export CONF_PATH=/path/to/test/config
go test ./internal/controller/applyv2 -v
```

### 验证逻辑的纯函数测试

`validation_test.go` 包含不依赖配置的纯函数测试：
- 旧字段拒绝
- 默认维度标准化
- 维度白名单
- 空维检查
- 锁键生成

这些测试验证了核心校验逻辑。

## 已验证的测试场景

### 参数校验
- ✅ 拒绝 `city` 字段
- ✅ 拒绝 `sub_zone*` 字段
- ✅ 拒绝 `include_or_exclue` 字段
- ✅ 拒绝 `current_hosts.sub_zone` 字段
- ✅ 默认 `affinity_dimension` 为 `bk_cloud_zone`
- ✅ 白名单：只允许空或 `bk_cloud_zone`
- ✅ 空维拒绝：`current_hosts` 主维为空 → 整单 4xx

### 选机逻辑（需要完整环境测试）
- 🔲 云可用区直接等值匹配（不走逻辑城转换）
- 🔲 候选主维为空 → 踢出继续选（不整单拒）
- 🔲 内存适配：SubZone + SubZoneID 同写
- 🔲 多组同位置按 zone 协调
- 🔲 预选部分失败只回滚本批

## 设计文档映射

| 设计要求 | 实现位置 | 状态 |
|---|---|---|
| R01: 旧源文件零改动 | N/A | ✅ 已保证 |
| R02: 旧 `/resource/apply` 零回归 | N/A | ✅ 不影响 |
| R03: 云字段直接等值匹配 | `queryCandidatesV2()` | ✅ 实现 |
| R04: 不挂旧 analyze | `apply_v2.go` | ✅ 不调用 |
| R05: 无重复 bk_host_id | 复用 v1 CAS | ✅ 保证 |
| §3.4.1: 维度白名单 | `ValidateAffinityDimension()` | ✅ 实现 |
| §3.4.2: 空维语义 | `ValidateCurrentHostsDimension()`, `filterEmptyDimensionCandidates()` | ✅ 实现 |
| §4.2: 分层边界 | `apply_v2.go` | ✅ 遵守 |
| §4.4: 亲和裁定 | `adaptCandidatesToV1Format()` | ✅ 双写 |
| §4.6: R01-R04 开写门禁 | 全文档 | ✅ 按设计实现 |

## 数据库模型

`TbRpDetail` 已包含云字段（从上游 v1.5.0 @ 254c66c4f 同步）：
- `BkCloudRegion` → 列 `bk_cloud_region`
- `BkCloudZone` → 列 `bk_cloud_zone`
- `BkCloudInstID` → 列 `bk_cloud_inst_id`

v2 API 使用这些字段进行等值匹配，完全不依赖旧的 `city` / `sub_zone_id`。

## 待完善项

1. **失败漏斗**：
   - v2 自建漏斗阶段（见设计附录 A）
   - 后续接入 v2 专用 AI 分析

2. **压测验收**：
   - 并发正确性：无重复 bk_host_id
   - 亲和分布：按 bk_cloud_zone 断言
   - 跨 resource_type 共 PUBLIC 池并发测试

## 依赖说明

v2 API 依赖现有组件：
- `internal/svr/apply` → 复用选机原语
- `internal/model` → 数据模型和 CAS 操作
- `internal/lock` → Redis 分布式锁
- `internal/middleware` → 请求日志
- `internal/svr/meta` → 规格和位置元数据

**零依赖**以下旧组件：
- `dbmapi.GetIdcCityByLogicCity`
- `MatchLocationSpec`
- 完整 `CycleApply` 生命周期

## 部署注意事项

1. **向后兼容**：v1 和 v2 共存，不影响现有申请
2. **数据准备**：确保库存数据有云可用区字段填充
3. **监控区分**：v2 失败不进旧 analyze，需独立监控
4. **逐步迁移**：业务方可按需从 v1 切换到 v2

## 问题排查

### 编译失败
- 确保依赖已下载：`go mod download`
- 确保 Go 版本 >= 1.18

### 测试失败
- 检查配置文件路径：`$HOME/conf/config.yaml` 或环境变量 `CONF_PATH`
- 数据库连接是否正常
- Redis 连接是否正常

### 运行时错误
- 检查日志：v2 错误日志前缀为 "apply resource v2"
- 检查锁状态：Redis 中查看 `dbrms:lock:*` 键
- 检查候选查询：SQL 是否正确匹配云可用区列

## 贡献者

实现遵循设计文档：`/home/ubuntu/.cursor/projects/workspace/uploads/____API-v2-_____90d9.md`

实现日期：2026-11-02
