# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验（含ISO时间与`ResourceConflict`）。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/resources.py`：抢修船同期航行、备缆余量、许可有效期的占用核对与释放。
- `src/repository.py`：SQLite建表、旧库升级回填、事务和查询（写事务带锁重试）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：资源总览与冲突保留输入的审批/改派演示页面。
- `tests/`：完整流程、规则计算、失败场景、资源冲突/并发/升级测试。

## 资源占用规则

- **审批`approve`**：按`required_spare_km`预留备缆；若提供`vessel_name`与`sailing_from/sailing_to`，核对该船同期航行是否重叠，并校验许可有效期（`permit_expiry`支持日期时间或`YYYY-MM-DD`，缺省取船舶台账）。冲突时记录停在当前状态、版本不变，审计写`blocked`，409响应的`conflict.occupied_by`写明被哪项抢修占住（reference、航行窗）。
- **动员`mobilize`**：补占/确认船舶窗口并再次核对许可；旧版审批未占资源时在此补占，兼容旧数据。
- **接续`splice`**：按`spare_used_km`实际用量扣减，超出余量则停在`spliced`前。
- **恢复`restore`**：`spare_returned_km`余量回流（记回），同时释放抢修船。
- **取消`cancel`/改派`reassign`**：取消释放船与备缆；改派先按新船新窗口核对，冲突则整体回滚、原占用保留，通过后原占用改记新船。`reassign`不改变记录状态，支持`approved/mobilized`。
- 并发：所有占用核对在`BEGIN IMMEDIATE`写事务内完成，两名调度员同时提交同一艘船只让一个通过；`database is locked/busy`指数退避重试（最多5次）。
- 旧库升级：新增`resource_occupations/spare_stock/vessel_registry`表（`PRAGMA user_version=1`），未结束抢修自动回填占用，存量默认取历史申报`spare_length_km`最大值，可用接口调整。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表并升级旧库。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，含`occupations`当前占用。
- `GET /api/records/{id}/audit`：审计时间线（冲突会出现`blocked`事件）。
- `GET /api/stats`：状态统计。
- `GET /api/resources`：备缆余量、各船在航任务、全部活动占用。
- `PUT /api/spare-stock`：设置备缆总存量，体`{"total_km":100}`（admin/repair_manager）。
- `POST /api/vessels` 或 `PUT /api/vessels/{name}`：登记船舶许可号与有效期。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；`reassign`的`data`含`vessel_name/sailing_from/sailing_to`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、船期/备缆/许可冲突、取消与改派释放、接续扣减与恢复回流、并发抢占（服务层与HTTP层）以及旧库升级回填。
