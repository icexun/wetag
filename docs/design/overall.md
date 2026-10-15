# WeTag 整体技术方案

WeTag 是运行在企业微信智能机器人上的协作助理：群内 @ 问答、需求创建、文档更新、待办跟进、代码检索，以及需求/文档变更的订阅通知。本文描述分层、进程拓扑、跨模块契约、存储与主链路；目录归属见 `code-layout.md`，运行时细节见 `runtime.md`，接入层见 `channel-space.md`。

## 1. 分层与依赖方向

| 模块 | 目录 | 职责 |
| --- | --- | --- |
| Channel | `app/gateway/` | 企微长连接协议、排重、卡片绑定，不理解业务 |
| Space | `app/space/` | Bot → 业务线 → 群 → 会话事项 的归属与路由 |
| Capability | `app/capability/` | Router + 确定性 Workflow + 语义节点 + Integration Ports |
| Control | `app/control/` | Rules 装配、审批闸门、三层语义记忆、Todo 引用台账 |
| Runtime | `app/runtime/` | Task 队列、WorkerPool、Scheduler、Outbox、审计（横切） |
| Analytics | `app/analytics/` | 指标查询与 Loop（只读聚合） |

`app/contracts/`（契约）与 `app/platform/`（DB / CLI / 并发 / 可观测底座）可被任何模块 import，二者不 import 业务模块。

```
Channel ──► Space ──► Capability ──► Control
   ▲                      │
   └────── Runtime ◄──────┘        Runtime 横切：入站转 Task、调 Capability、经 Outbox 出站
                                   Analytics：只读消费
```

边界纪律：

1. **Channel 不懂业务**：不判定意图、不选工具、不解释结果。
2. **Capability 不碰协议**：不拼 CLI 字符串、不感知背后是哪个 CLI、不直接发企微消息，出站一律产出 Envelope。
3. **所有入口收敛为 Task**：@ 消息、定时触发、卡片按钮、派生任务都是 `task` 表的一行，由同一个 WorkerPool 消费。
4. **一次执行只属于一个业务线**：`workspace_id` 在入口确定，Capability 入口再次校验，Repository 层强制过滤。

## 2. 进程拓扑与隔离

企微限制单机器人一条长连接，因此**一个 workspace（业务线）一个进程、一个机器人**，没有中心网关：

```
应用机（supervisor / systemd）                       PostgreSQL（可独立部署）
├── wetag WORKSPACE_ID=team_a  bot_a │ repos/team_a ──┐
├── wetag WORKSPACE_ID=team_b  bot_b │ repos/team_b ──┼──► ws_team_a / ws_team_b / ws_demo / org
└── wetag WORKSPACE_ID=demo    bot_d │ 无代码仓     ──┘
语义记忆：AgentRun 托管 collection（与 PG 无关）
```

进程内单 asyncio 事件循环，承载长连接、Scheduler、WorkerPool、Outbox 派发、HTTP（AgentRun 兼容端点 + 控制台）。`WORKSPACE_ID` 决定 DB schema 与角色、记忆 collection、代码仓目录、外部凭据分组；部署配置在 `config/workspaces/<id>.yaml`。

隔离分层叠加，任何一层单独都不够：

| 层 | 手段 | 落点 |
| --- | --- | --- |
| 部署 | 进程环境里只有本业务线凭据；记忆 collection、代码仓目录物理分离 | `config/workspaces/*.yaml` |
| 协议 | 入站 `aibotid` 必须等于本进程 bot；出站目标群必须已归属本业务线 | `gateway/inbound.py`、`space/resolver.py` |
| **DB 权限** | 每业务线一个 schema `ws_<id>` + 专属角色，只能读写自己 schema；对 `org` 只有 `SELECT` + `chat_ownership` 的 `INSERT` | `migrations/0007` |
| **连接** | `search_path = ws_<id>, org`，**不含 public**，漏写前缀的 SQL 直接报错 | `platform/db/schema.py`、`pool.py` |
| **语句** | Repository `_bind()` 要求 SQL 含 `workspace_id`；`_guard_rows()` 行级二次校验，不符即抛 `IsolationViolation` | `platform/db/repository.py` |

权限层把「漏写 `WHERE workspace_id`」从静默泄露变成显式权限错误；语句层补上 `org` 共享表权限层拦不住的部分。DB 权限错误与 `aibotid` 不符一律按安全事件告警，不重试。

## 3. 技术选型

| 维度 | 选型 |
| --- | --- |
| 运行时 | Python 3.11 asyncio；AgentScope 1.x `ReActAgent`；AgentRun SDK；任意 OpenAI 兼容模型端点 |
| 调度 | APScheduler `AsyncIOScheduler` + `SQLAlchemyJobStore`，jobstore 落各自 `ws_<id>` schema |
| 结构化存储 | PostgreSQL，asyncpg 每进程一个连接池；`migrations/` 为唯一变更途径，启动校验版本 |
| 语义记忆 | AgentRun SDK 托管 collection；Agent 工作记忆持久化到 PG（`agent_working_memory`） |
| 外部集成 | 官方 CLI（lark-cli / tapd-cli / wecom-cli）；代码仓本地只读浅克隆 + `rg` |
| 向量 | `real[]` + 进程内暴力余弦，不引入 pgvector |
| 可观测 | 标准 OpenTelemetry（可选，未安装即 no-op）+ `task` / `audit_log` 聚合 |

## 4. 跨模块契约（`app/contracts/`）

术语：业务线 `workspace_id`（如 `demo` / `team_a`，须匹配 `^[a-z][a-z0-9_]{1,30}$`）、群或单聊 `chat_id`、会话事项 `topic_id`（群内并行话题，代码不用 `session` 命名）、任务 `task_id`、出站信封 `Envelope`。

### 4.1 SpaceContext

`SpaceContext { organization_id, workspace_id, bot_id, chat_id, topic_id, actor_user_id, effective_config_version }`，由 `space/context_factory.py` 唯一构造，随 Task 持久化，不可被 Agent 参数覆盖。恒等式：`event.aibotid = 进程 bot_id = 群归属 bot_id = 执行时 workspace 对应 bot_id`，任一不等即拒绝并告警。

### 4.2 Task 与 JobResult

```
Task { id, workspace_id, job_name, kind: deterministic|agentic, trigger_source: mention|schedule|card_action|derived,
       parent_id, idem_key(UNIQUE), chat_id, topic_id, payload, priority(用户=1, 定时=5),
       status: PENDING|RUNNING|WAIT_CONFIRM|SUCCEEDED|FAILED|DEAD, attempt, progress, error, rule_version, lease_expire_at, token_cost }
```

- 确定性 Job 与 Agentic Task 分离：轮询、同步、回读、比对一律零 token；只有需要语义判断时才调用模型。
- Job 不直接调 Agent、不直接发消息，只在 `JobResult` 中声明 `derived_tasks` 与 `envelopes`，由 Worker 统一落库派发（`parent_id` 可追溯）。
- 幂等键 `idem_key` 建唯一索引，去重靠 `ON CONFLICT DO NOTHING`，不做应用层先查后写。

### 4.3 Envelope 与 Outbox

`Envelope { chat_id, topic_id, msg_type: markdown|template_card, payload, merge_key, update_of, priority: urgent|normal, task_id }`。Capability 与 Job 只产出 Envelope，经 Outbox 四道关卡发送：

1. **合并窗口**：同 `(chat_id, merge_key)` 在窗口内并入同一行的 `payload.items`，窗口不因新事件顺延；`urgent` 或无 `merge_key` 直通。
2. **会话态校验**：群从未激活 → `DROPPED` + 告警；超会话窗且 `SESSION_WINDOW_ENFORCE` 开 → `SUSPENDED`，下次入站补发，超保留期转 `DROPPED`。
3. **per-chat 双层令牌桶**：普通 24/min、800/h；`urgent` 可透支到平台上限。取不到令牌重排，不丢弃。
4. **发送与重试**：递增退避，超次 `FAILED` + 告警；`IsolationViolation` 立即终止。

### 4.4 Integration Ports（`contracts/ports.py`，业务层只依赖 Protocol）

| Port | 方法 | 实现 |
| --- | --- | --- |
| `DocumentPort` | `fetch` / `batch_meta` / `list_tree` / `update` / `add_comment` | `integration/document/lark_cli.py` |
| `RequirementPort` | `project_config` / `list_active` / `get_changes` / `create` / `update` / `quota_watermark` | `tapd_cli.py`（官方 tapd-cli），解析在 `normalize.py` |
| `TodoPort` | `resolve_user` / `create` / `get_batch` | `integration/todo/wecom.py`（唯一实现） |
| `CodeRepositoryPort` | `sync` / `search` / `last_synced_at` | `integration/repo/service.py` |

`container.py` 在装配期探测一次各端口可用性：不可用的端口为 `None` 并在 `PortStatus` 记录原因，绝不塞空实现。

### 4.5 CLI 白名单

三个 CLI 加 `rg` / `git` 共用 `platform/cli/executor.py`：参数数组执行（无 shell 拼接）、仅执行 `platform/cli/registry.py` 登记的子命令（**删除类不登记**）、仅注入当前 workspace 的凭据、输出统一解析为 `CliResult`（业务层不接触 stdout 原文）、错误码归一化决定重试或告警、每次调用写审计。

### 4.6 幂等、审批、审计

| 纪律 | 规定 |
| --- | --- |
| 幂等 | 外部写必带业务幂等键；CLI 不支持时由 `idempotency_record` 兜底（claim → 外部调用 → settle，外部调用不在事务内） |
| 审批 | 四级分档（`control/approval/classifier.py`）：只读直通；低危写直通 + 审计；高危写命中 Auto Mode 规则直通，否则写 `pending_approval`、发审批卡片、任务进 `WAIT_CONFIRM`，超时由 `approval_expiry` 取消；删除类直接拒绝。未知写工具默认按高危 |
| 审计 | 工具调用、记忆写入、外部请求、审批动作统一经 `runtime/audit.py` 写 `audit_log`，含 `trace_id / workspace_id / chat_id / topic_id / task_id / rule_version`，只存摘要不存凭据与正文 |

## 5. 数据存储

### 5.1 `org` schema（`migrations/0001`）

- `schema_migration`：迁移台账（文件、版本、checksum）。
- `chat_ownership`：`chat_id` 主键，`INSERT ... ON CONFLICT DO NOTHING` 后回读判定归属，一个群只属于一个业务线（单聊不登记）。
- `org_config`：组织级 / 业务线级 Rules 与公共配置（带版本号）。

### 5.2 业务线 schema `ws_<id>`（`migrations/0002`–`0006`）

| 表 | 所有者 | 用途 |
| --- | --- | --- |
| `task` / `outbox` / `audit_log` / `runtime_state` | Runtime | 任务、出站队列、审计、降级开关与 Job 游标 |
| `apscheduler_jobs` | Runtime | APScheduler 自建，必须落本 schema，否则多进程互抢 job |
| `chat_group` / `topic` / `topic_correction` | Space | 群登记与激活时间、会话事项、纠正样本（仅评测） |
| `card_task` / `feedback_record` | Channel | 卡片安全绑定（CAS 状态迁移）、用户反馈 |
| `requirement` / `requirement_relation` | Capability | TAPD 需求投影（也是变更比对基线）；需求 ↔ 文档 / 群 / 事项 / 待办 / 仓库 的确定性关联 |
| `doc_index` | Capability | 文档快照：章节摘录 + 哈希 + embedding + revision，`stale` 标记 |
| `chat_subscription` | Capability | 群订阅 `(chat_id, source, target)`，`target='*'` 表示全部 |
| `idempotency_record` / `rule_change_log` | Capability | 外部写幂等兜底；Loop 自动规则变更存档 |
| `pending_approval` | Control | 高危写审批的唯一状态载体 |
| `pending_memory` / `memory_conflict` | Control | 待审批记忆、矛盾记忆标记 |
| `todo_ref` | Control | 企微原生待办引用，只存归属与提醒计数 |
| `agent_working_memory(_meta)` | Control | Agent 对话上下文持久化 |

### 5.3 三层语义记忆（`control/memory/`）

- 组织级：`ORG_MEMORY_COLLECTION_NAME`，只读，写入只能走 `pending_memory` 提案。
- 业务线级：进程级 collection `MEMORY_COLLECTION_NAME`，低置信度写入进 `pending_memory` 确认。
- 群级：业务线 collection 内按 metadata `chat_id` 过滤，回复后自动沉淀。

召回范围固定为「当前群 + 当前业务线 + 组织级」，结果标注来源层级与来源群。写入前去重，矛盾记忆两条都保留并记 `memory_conflict`；事项归档时只提炼结论。记忆服务不可用时显式降级并告知「本次未使用历史记忆」。

**双轨分界**：需要精确更新、确定性删除、可解释比对的走 PG 台账（需求、文档快照、事项、订阅、待办引用）；只需模糊召回的走语义记忆（规范、术语、历史决策、群偏好）。

## 6. 主链路

### 6.1 用户触发

```
群内 @ → Channel：aibotid 校验 → 转 Task（msgid 落 idem_key 排重）
  → Space：chat_ownership 登记 / chat_group upsert / 事项路由 L1–L4 → SpaceContext
  → Runtime：priority=1 的 Task，同 chat_id 串行
  → agent_reply：Rules 中间件装配 system_prompt → ReActAgent + 审批闸门中间件 + 记忆召回
     或 Capability Router → 确定性 Workflow（requirement_create / doc_update）
  → FastAck：5s 内完成直接回复，否则先回执，终态原地更新
  → Outbox → 企微
```

事项路由：L1 引用消息 → L2 外部标识（TAPD 编号 / 飞书链接）→ L2.5 超短应答归最近事项 → L3 语义判定（`capability/intelligence/topic_route.py`）→ L4 新建。原则是宁可新建，不错误合并。

### 6.2 变更通知

```
tapd_sync ── 字段比对（title/status/owners/category）──┐
                                                      ├→ Notifier → Outbox → 订阅群
doc_poll → index_refresh ── 章节比对（anchor + hash）──┘
```

- 比对全部确定性，不调模型；首次入库只建基线，不推送。
- `doc_poll` 监听清单 = `runtime_state` 手工清单 ∪ `doc_index` 已入库文档 ∪ 非终态需求关联文档 ∪ 被订阅文档；按版本比对，有变化才派生 `index_refresh`，幂等键为文档 + 版本。
- `index_refresh` 刷新 `doc_index`：老 revision 不覆盖新 revision，内容未变只 touch `refreshed_at`；拉取失败标 `stale`，下轮自愈重刷。
- Agent 经飞书工具写文档成功后直接派生 `index_refresh`，不等下一轮轮询。
- 订阅由 Agent 工具 `wetag_subscribe_changes` / `wetag_unsubscribe_changes` / `wetag_list_subscriptions` 维护，群取自任务上下文，模型不能替别的群订阅。
- 通知不设 `merge_key`（合并会把正文并进 `items`）。

### 6.3 待办跟进

`wecom_todo_create` 工具创建企微原生待办并登记 `todo_ref`；`todo_readback` 周期经 `TodoPort.get_batch` 实时回读，逾期提醒、达阈值升级抄送负责人，已完成归档、不可见失效。待办域不可用时群内卡片明示「暂不可用」，不以本地数据伪装已交付。

## 7. Analytics 与 Loop

Analytics 零埋点，指标全部来自已有表：使用（`task`）、推送（`outbox` + `feedback_record`）、待办（`todo_ref` + 实时回读）、记忆（记忆层 + `audit_log`）、成本（`task.token_cost`）。控制台 API 挂在进程 HTTP 端点（`analytics/console/api.py`）。

- **Loop-2 待办跟进**（`loops/loop2_todo.py`）：只产出策略提案，经人确认后生效。
- **Loop-3 回复规约**（`loops/loop3_push.py`）：`loop_digest` 每周触发，👎 反馈达阈值 → 提炼规约 → 写 biz 层 Rules，下一条消息即生效。护栏：只写 `loop3-` 前缀规则、单次条数与长度有上限、每次变更落 `rule_change_log` 并通知群。

会话事项纠正样本只进 `topic_correction` 供离线评测，不喂 Prompt。

## 8. 非功能

### 8.1 安全

- 跨业务线越权：第 2 章隔离分层；`IsolationViolation` 穿透 Router，任务直接 `DEAD` + 安全告警。
- Prompt 注入：群消息、文档、代码一律视为不可信数据；CLI 参数按 schema 生成，不透传原始字符串。
- 误操作与凭据：四级审批 + 幂等键 + 删除类命令不登记；凭据只在进程环境、不进对话上下文；代码仓只读。
- 未内置群准入管控，由部署方控制机器人可见范围。

### 8.2 失败不静默

全链路统一语义：**未完成必须返回 `completed=False` + reason**，不允许以空结果冒充成功。

- Router 未登记的类型、端口不可用、未预期异常都收敛为 `JobResult.incomplete(reason)`；`IsolationViolation` 例外，穿透向上。
- `require_port` 取不到端口、TAPD 归一层认不出响应都抛 `PortUnavailable`，不返回空列表（会被读成「没有进行中需求」）。
- 结构化输出失败一律 `ok=False`，上层转为未完成。

| 告警条件 | 动作 |
| --- | --- |
| 同一 `job_name` 连续 `FAIL_ALERT_STREAK` 次失败 | 告警 + 控制台红标 |
| `doc_poll` 等新鲜度超阈值 | 标 `stale`，不输出「无变化」结论，结论附快照时间 |
| 凭据失效 / 权限错误 / TAPD 额度低 | 告警并进入降级 |
| DB 不可达或连接池耗尽 | 告警；任务回 `PENDING` 退避，不计 attempt |
| DB 权限错误 / 归属不符 | 按疑似越权安全事件处理，不重试 |

### 8.3 降级

| 故障 | 降级 |
| --- | --- |
| 飞书权限不足 | 只处理可读文档，明示覆盖范围 |
| TAPD 额度低 | 低于安全水位降频，低于停用水位停跑，状态落 `runtime_state` |
| `tapd-cli` 缺失 / 未配 `TAPD_TOKEN` / 鉴权失败 | 需求端口不可用，`tapd_sync` 不跑，启动日志与覆盖范围明示 |
| 代码仓过期 / `rg` 缺失 | 结论附快照时间 / 回落 `git grep` |
| DB 不可达 | 显式回复暂不可用，不做内存暂存（会破坏幂等语义）；定时 Job 恢复后按 misfire 宽限补跑 |

### 8.4 PostgreSQL 工程约束

- jobstore 按 schema 隔离；每进程一个连接池，`池大小 × 进程数` 留在 `max_connections` 安全水位内。
- 事务要短：LLM / 外部调用前提交，结果回来另起事务；Workflow 装配期拒绝「开事务的 step 同时调外部」；角色设 `idle_in_transaction_session_timeout=15s`。
- 去重靠 `UNIQUE` + `ON CONFLICT`；迁移只增不改（`init_workspace.py` 校验 checksum，启动校验版本）；时区统一 UTC。

## 9. 设计决策

- **一业务线一进程、无中心网关**：长连接必须由持有 Bot 凭据的进程自建；网关会集中凭据、破坏进程级隔离。
- **共享 PostgreSQL + schema/角色隔离**：共享实例必须配 DB 权限层，把隔离失败从静默泄露变为显式错误；唯一的跨业务线约束（群归属唯一）由 `org.chat_ownership` 唯一约束承担。
- **只有一张 `task` 表、一张 `outbox` 表、一张 `audit_log` 表**：入站排重走 `task.idem_key`，卡片事件幂等走 `card_task` CAS，不设第二套队列或审计表。
- **调度归 Runtime 自建**：框架内置调度是会话内的，无法在无人 @ 时执行。机制（调度、重试、留痕）归 Runtime，业务逻辑（比对、刷新、通知）归 Capability。
- **TodoPort 只做企微原生待办**：不实现本地台账替身——本地记录不会出现在当事人的待办列表里，比直接报不可用更难被发现。
- **语义判断走结构化输出而非 ReAct 循环**：顺序固定、temperature=0、失败显式；ReAct 只用于对话 Agent。
