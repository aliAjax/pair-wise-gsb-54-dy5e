# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量，以及船舶同期航行/剩余备缆的资源冲突检查。
- `src/repository.py`：SQLite建表（幂等升级）、`BEGIN IMMEDIATE`串行化写事务、写锁重试和查询。
- `src/service.py`：用例编排、权限检查、资源占用预留/改派/扣减/释放、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应（冲突携带占用方详情）。
- `src/audit.py`：事件时间线。
- `static/index.html`：资源调度演示页（输入本地保留、冲突就地提示、一键改派/重试）。
- `tests/`：完整流程、规则计算、失败场景、资源冲突、并发抢占与存量升级测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表；检测到旧版本数据库时会自动补齐新表，并为已在进行中（approved～tested）且已指定船名的抢修补建资源占用（幂等，可重复执行）。

## 资源占用规则

- 每条抢修对一艘抢修船产生一行占用（`resource_allocations`），登记航行窗口、许可到期、在船备缆、预留量、实耗量、归还量。
- **审批（approve）**时可指定`vessel_name`、`voyage_start/voyage_end`（ISO8601，成对提供）、`vessel_spare_km`、`permit_expires_at`；服务在同一写事务内核：
  1. 船的**同期航行**不重叠（任一方未声明窗口时按同期处理，防止重复预订）；
  2. 该船**剩余备缆**（在船备缆 − 其他抢修净占用）不小于本抢修`required_spare_km`；
  3. **许可有效期**未过（`permit_valid`为真且`permit_expires_at`晚于当前时间）。
  未在审批时指定船名的老流程仍可在**动员（mobilize）**时补登占用，校验同样生效。
- 冲突返回`409 conflict`，记录停在原状态、版本不变；响应`details.held_by`写明被哪条抢修（`record_id`/`reference`）占住，前端保留输入并提示改派或换船重试。
- **改派（reassign）**（approved/mobilized，经理或船长可用）：在同一事务内核对新船资源，通过后旧占用置为`released`、新占用登记；失败则原占用不动。
- **取消（cancel）**：释放占用，未实耗部分计为归还。
- **接续（splice）**：按`spare_used_km`登记实际用量。
- **恢复（restore）**：净占用清零并把`预留 − 实耗`的余量记回`returned_km`，船与备缆即可被后续抢修使用。

## 并发与写入

- 所有写操作以`BEGIN IMMEDIATE`串行化，两个调度员同时提交同一艘船时只有一个通过，后到者得到409并保留输入。
- 写锁竞争（`database is locked/busy`）自动指数退避重试4次；仍失败返回`503 write_busy`，客户端可安全重试（版本号乐观锁兜底）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：资源调度演示页。
- `GET /api/records`：记录列表（含`resource`占用信息），可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（`approve/mobilize/survey/splice/test/restore/cancel/reassign`），请求体为`{"expected_version":1,"data":{...}}`。

动作数据示例（审批并占用船舶）：

```json
{"expected_version":1,"data":{"repair_manager":"RM-2","vessel_name":"CS-1",
 "voyage_start":"2026-10-10T00:00:00Z","voyage_end":"2026-10-12T00:00:00Z",
 "vessel_spare_km":40,"permit_expires_at":"2027-01-01T00:00:00Z"}}
```

冲突响应示例：

```json
{"error":"conflict","message":"抢修船CS-1的同期航行已被抢修记录HTTP-1占用（航行窗口…）",
 "details":{"resource":"vessel","vessel":"CS-1","held_by":{"record_id":1,"reference":"HTTP-1"}}}
```

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、船舶/备缆/许可三类资源冲突、改派释放、接续扣减、恢复归还、并发同船抢占、写锁重试以及旧库升级回填。
