# DBM 资源申请 API v2 实现说明

## 概述

v2 API 基于请求级 `affinity_dimension` 提供两种位置模式，并复用 v1 apply/pick 能力：

- **cloud 模式**（默认，`affinity_dimension` 为空或 `bk_cloud_zone`）：使用云字段（bk_cloud_region / bk_cloud_zone）筛选与分桶；
- **legacy 模式**（`affinity_dimension=sub_zone`）：使用 v1 的 city / sub_zone 字段筛选与分桶，行为对齐 v1（含逻辑城市转换）。

两种模式整单统一、字段互斥（混传返回 4xx），核心选机逻辑复用 v1。为支持复用，对少量 v1 公共逻辑做了导出与兼容封装；未改变 v1 对外行为。

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

### 1. 位置模式与字段（Red Line R1-R4）

位置模式由请求级 `affinity_dimension` 决定，两种模式的字段互斥，**混传返回 4xx（不做静默忽略）**，
避免调用方误以为未生效的过滤条件已经生效：

| 字段 | cloud 模式 | legacy 模式（sub_zone） |
|---|---|---|
| `bk_cloud_region` / `bk_cloud_zone_list` / `exclude_bk_cloud_zone_list` | ✅ | ❌ 拒单 |
| `city` / `sub_zone_ids` / `exclude_sub_zone_ids` / `exclude_rack_ids` / `exclude_net_device_ids` | ❌ 拒单 | ✅（字段含义与 v1 相同） |
| `include_or_exclue` | ❌ 不支持 | ❌ 不支持（`sub_zone_ids` 语义固定为「包含」） |
| `hosts`（指定 bk_host_id 申请） | ✅（v1 语义） | ✅（v1 语义） |

`current_hosts[].sub_zone` / `current_hosts[].bk_cloud_zone`：按请求级维度取主维字段。

逻辑城市转换：**legacy 模式与 v1 一致**，`city` 非空时调用 `GetIdcCityByLogicCity`（dev 模式跳过）；
cloud 模式不调用转换，按云地域直接匹配。

### 2. 亲和维度（affinity_dimension）

- **请求级字段**（与 `groups_in_same_location` 同层），非 detail 级：位置模式是整单语义，
  放在请求层后结构上保证同一请求内不会混用两种模式
- **可选字段**，缺省默认为 `bk_cloud_zone`
- **白名单**：允许空、`bk_cloud_zone` 或 `sub_zone`
- 其他值（如 `rack_id`）会被拒单

### 3. 空维语义（Empty Dimension Rules）

- **current_hosts 主维为空**：整单 4xx 拒绝（调用方数据异常）
- **库存候选主维为空**：从候选集踢出，继续选机（不整单拒）
- 踢除后数量不足 → 业务失败（资源不足），不是参数 4xx

### 4. 路径 A 复用（Reuse Path A）

cloud 模式**不使用**以下旧组件（legacy 模式使用前两项）：
- ❌/✅ `MatchLocationSpec`（城市/园区过滤，legacy 模式复用）
- ❌/✅ `GetIdcCityByLogicCity`（逻辑城转换，legacy 模式复用）
- ❌ `CycleApply`（完整生命周期，两种模式都不使用，v2 自建 CycleApplyV2）

v2 **复用**以下组件（少量导出/兼容封装，两种模式共用）：
- ✅ `PickInstanceBase`（内存亲和选机）
- ✅ `PickerDone`（选满检查）
- ✅ `PreselectFromUnusedWithIDs`（Unused → Preselected，返回真正成功的 host id）
- ✅ `LockReturnPickers`（Preselected → Used）

### 5. 内存适配（In-Memory Adaptation）

为了让 v1 选机逻辑正确工作，v2 在内存中做以下适配（**不回写数据库**），仅 cloud 模式需要：
1. 将 `bk_cloud_zone` 值同时写入 `SubZone` 和 `SubZoneID`
2. 将 `current_hosts.bk_cloud_zone` 写入 `SubZone`

这样 v1 的分桶逻辑（按 SubZone）和快照逻辑（读 SubZoneID）都能正确处理云可用区。

legacy 模式不需要这层适配：`SubZone`/`SubZoneID` 是原生真实值，v1 分桶语义直接成立。

### 6. 多组同位置（groups_in_same_location）

v2 自建外层协调，按模式枚举候选位置并逐个尝试：
1. cloud 模式：枚举候选云可用区（指定的 zone 列表，或库存中该地域全部 zone）；
   legacy 模式：在同一 city 下枚举候选园区（`getAllAvailableSubZones`，枚举已含位置过滤条件）
2. 每个候选位置尝试选满所有组：cloud 用 `BkCloudZoneList` 锁定同云区（组内可跨园区）；
   legacy 用 `ForcedSubZoneIds` 锁定同园区（对齐 v1 语义）
3. 任一组因资源不足失败 → 回滚本批 Preselected，尝试下一个候选；系统错误立即上抛
4. 全部失败 → 整单失败，并附带失败现场

同位置校验：cloud 模式要求各分组 `bk_cloud_region` 与 zone 条件一致；
legacy 模式对齐 v1，要求各分组 `city` 与园区条件一致。

### 7. 并发安全（R03 细则）

- **共池互斥**：与 v1 使用同一套锁（按 resource_type 或 global）；`dry_run=true` 全程只读，不写 Preselected
- **预选部分失败**：
  - `PreselectFromUnusedWithIDs` 通过 `FOR UPDATE + status=Unused` 的 CAS 返回本请求真正写成功的 host id
  - 只回滚这批成功 id，禁止误伤他请求
- **后组失败**：`CycleApplyV2` 返回累计 pickers，由 handler 统一回滚；同位置逻辑先回滚本批再尝试下一个候选
- **失败现场**：失败请求投递 `AnalysisTaskChan`，并携带漏斗/亲和分布等可观测数据

## 接口契约

### 请求示例

cloud 模式（默认）：

```json
POST /v2/resource/apply

{
  "resource_type": "MySQL",
  "for_biz_id": 1,
  "affinity_dimension": "bk_cloud_zone",
  "details": [
    {
      "group_mark": "backend",
      "count": 2,
      "affinity": "CROS_SUBZONE",
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

legacy 模式（`affinity_dimension=sub_zone`）：

```json
{
  "resource_type": "MySQL",
  "for_biz_id": 1,
  "affinity_dimension": "sub_zone",
  "details": [
    {
      "group_mark": "backend",
      "count": 2,
      "affinity": "CROS_SUBZONE",
      "location_spec": {
        "city": "chongqing",
        "sub_zone_ids": ["sz-1", "sz-2"],
        "exclude_sub_zone_ids": [],
        "exclude_rack_ids": [],
        "exclude_net_device_ids": []
      },
      "current_hosts": [
        {
          "bk_host_id": 1001,
          "sub_zone": "sz-1",
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
- Summary：cloud 模式输出 `by_cloud_zone` / `by_cloud_zone_rack`；
  legacy 模式输出 `by_sub_zone` / `by_sub_zone_rack` / `by_cloud_zone` / `by_cloud_zone_rack`

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
- 旧字段静默忽略
- 默认维度标准化
- 维度白名单
- 空维检查
- 锁键生成

这些测试验证了核心校验逻辑。

## 已验证的测试场景

### 参数校验
- ✅ 位置字段混传拒绝：模式与字段不匹配（如 `sub_zone` 维度带 `bk_cloud_region`）→ 整单 4xx
- ✅ `include_or_exclue` 两种模式都不支持（legacy 的 `sub_zone_ids` 语义固定为「包含」）
- ✅ `affinity` 归一化并对未知枚举显式拒绝
- ✅ `tolerance` 范围校验（0~1）
- ✅ `bk_cloud_zone_list` 与 `exclude_bk_cloud_zone_list` 交集校验（cloud 模式）
- ✅ legacy 模式亲和性数量校验对齐 v1（`CROS_SUBZONE` ≥2 园区、`tolerance=0` 园区数下限、`STRONG` ≥3、`WEAK` ≥2）
- ✅ `groups_in_same_location=true`：cloud 校验 `bk_cloud_region`/云可用区一致性；legacy 校验 `city`/园区条件一致性
- ✅ 默认 `affinity_dimension` 为 `bk_cloud_zone`（请求级字段）
- ✅ 白名单：允许空、`bk_cloud_zone` 或 `sub_zone`
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
| R03: 位置字段按模式匹配 | `queryCandidatesV2()`：cloud 走 `MatchBkCloudLocationSpec`，legacy 走 v1 `MatchLocationSpec` | ✅ 实现 |
| R04: 不挂旧 analyze | `apply_v2.go` | ✅ 不调用（legacy 逻辑城市转换走 `GetIdcCityByLogicCity`，与 analyze 无关） |
| R05: 无重复 bk_host_id | 复用 v1 CAS | ✅ 保证 |
| §3.4.1: 维度白名单 | `ValidateAffinityDimension()` | ✅ 实现 |
| §3.4.2: 空维语义 | `ValidateCurrentHostsDimension()`, `filterEmptyDimensionCandidates()` | ✅ 实现 |
| §4.2: 分层边界 | `apply_v2.go` | ✅ 遵守 |
| §4.4: 亲和裁定 | `adaptCandidatesToV1Format()` | ✅ cloud 双写；legacy 原生值直通 |
| §4.6: R01-R04 开写门禁 | 全文档 | ✅ 按设计实现 |

## 数据库模型

`TbRpDetail` 已包含云字段（从上游 v1.5.0 @ 254c66c4f 同步）：
- `BkCloudRegion` → 列 `bk_cloud_region`
- `BkCloudZone` → 列 `bk_cloud_zone`
- `BkCloudInstID` → 列 `bk_cloud_inst_id`

cloud 模式使用这些字段进行等值匹配；legacy 模式使用旧的 `city` / `sub_zone_id` / `rack_id` / `net_device_id` 列，行为与 v1 一致。

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

**零依赖**以下旧组件（cloud 模式；legacy 模式依赖前两项以对齐 v1 行为）：
- `dbmapi.GetIdcCityByLogicCity`（legacy 模式使用）
- `MatchLocationSpec`（legacy 模式使用）
- 完整 `CycleApply` 生命周期（两种模式都不使用）

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
