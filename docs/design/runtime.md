# WeTag Runtime：运行时与异步调度

Runtime 包在 AgentScope 外面，不改 Agent 内核，只负责：什么时候跑、跑几个、失败怎么办、结果怎么送出去。`ReActAgent` 是请求驱动、会话内的执行单元，没有跨会话的定时能力，因此「无人 @ 时也能干活」必须由 Runtime 承担。

Runtime 对上层只认两个抽象：`Job`（做什么）和 `Envelope`（要发什么）。比对、刷新、通知等业务逻辑在 Capability。

## 1. 进程内结构

```mermaid
flowchart TB
    subgraph EXT[外部]
        WX[企微长连接]
        CLI[lark-cli / tapd-cli / wecom-cli / git / rg]
    end
    subgraph RT["wetag 进程（单 asyncio loop）"]
        GW[Channel]
        SCH[Scheduler<br/>APScheduler]
        Q[TaskQueue]
        WK[WorkerPool]
        CAP[CapabilityRouter / AgentRunner]
        OB[Outbox]
        PG[(PostgreSQL<br/>ws_&lt;id&gt; + org)]
    end
    WX -->|@ / 按钮 / 反馈| GW --> Q
    SCH -->|到点塞 Task| Q
    Q --> WK --> CAP --> CLI
    WK -->|JobResult.envelopes| OB -->|发送原语| GW --> WX
    WK <--> PG
    OB <--> PG
    SCH <--> PG
```

启动顺序（`app/bootstrap.py`）：装配 Capability 并探测端口 → `Container.build`（连接池、迁移版本校验、Job 注册）→ `restore_pending` 把 PENDING 任务重新入队 → 启动 Scheduler 并处理错过的轮次 → 启动 Channel → `TaskGroup` 运行 WorkerPool、Outbox 派发循环、HTTP。每个长驻协程顶层自带兜底，异常不回抛 TaskGroup。

所有入口统一收敛成 `task` 表的一行；重试、超时、留痕、限流只实现一次，`task` 同时是 Analytics 的事实表。

## 2. 任务模型

### 2.1 确定性 Job 与 Agentic Task

确定性 Job 零 token，高频跑；只有需要语义判断（对话回复、需求结构化、事项 L3 判定）才调用模型。Job 基类：

```python
class Job(ABC):
    name: str
    kind: TaskKind = TaskKind.DETERMINISTIC
    timeout_s: float | None = None
    default_cron: str | None = None

    async def run(self, ctx: JobContext) -> JobResult: ...
```

`JobResult.derived_tasks` 是链式触发的唯一通道，`JobResult.envelopes` 是出站的唯一通道。外部能力经 `require_port(ctx, name)` 获取，未绑定抛 `PortUnavailable`，任务显式失败而非假成功。

### 2.2 状态机

```
PENDING ──CAS 领取──> RUNNING ──┬──> SUCCEEDED（落 derived_tasks 与 envelopes）
   ▲                           ├──> WAIT_CONFIRM（ApprovalRequired，不计 attempt）
   │                           ├──> FAILED → 退避 → PENDING（attempt < max）
   │                           └──> DEAD（attempt 达上限 / IsolationViolation / 审批超时）
   └── 启动恢复 / lease_reaper 回收过期租约
```

| 情形 | 处置 |
| --- | --- |
| 普通异常、超时 | `attempt+1`，退避 `min(60 × 2^attempt, 1800)` 秒，达 `MAX_ATTEMPT`（默认 3）进 `DEAD` + 告警 |
| `RateLimited` | `attempt+1`，退避基数放大到 600 秒 |
| `PoolExhausted` / `StorageUnavailable` | 回 `PENDING` 短退避 + 告警，不计 attempt |
| `ApprovalRequired` | `WAIT_CONFIRM`，审批通过后重新入队 |
| `IsolationViolation` | 直接 `DEAD` + 安全告警，不重试 |

- **幂等**：`idem_key` 唯一索引。定时轮次键 = `sha256(ws + job + 轮次分钟)`；`doc_poll` 派生键 = 文档 + 版本；入站消息键 = `msgid`。
- **租约**：执行中每 30 秒续租；`lease_reaper` 每分钟把过期的 RUNNING 重置为 PENDING，attempt 保留。
- **超时**：Agentic 默认 600 秒，确定性默认 300 秒，Job 可自带 `timeout_s`。

### 2.3 并发与互斥（`platform/concurrency/locks.py`）

- `chat_locks`：同一 `chat_id` 串行，跨群并行。会话事项依赖累积上下文，同群并行会互相读到写了一半的状态。
- `repo_lock` / `index_lock`：按仓库、按索引条目互斥。
- `llm_semaphore`：进程内所有模型调用共享 `LLM_MAX_CONCURRENCY`，饱和时排队而非失败；全机并发 = 该值 × 进程数。

`RUNTIME_WORKERS` 只是协程上限，真正的成本闸门是 LLM 信号量。

### 2.4 FastAck（`runtime/fastack.py`）

用户触发的任务启动 `FAST_ACK_SECONDS`（默认 5s）计时：先完成则直接发结果；超时则先发回执，`PROGRESS_INTERVAL_SECONDS > 0` 时周期原地更新进度（默认 0 = 不发），结束时原地更新为终态或失败原因。

双通道：优先走 `response_url` 流式回复（同一条消息原地刷新，窗口 10 分钟、重启失效）；窗口过期或发送失败后改走 Outbox 主动推送，保证终态必达。回执、进度、终态均为 `urgent` 且无 `merge_key`。用户可见文案不带 `task_id`。

### 2.5 AgentRunner（`runtime/worker/agent_runner.py`）

`agent_reply` 任务 → 一次 AgentScope 执行：

- Rules 中间件按 组织 → 业务线 → 群 → 事项 装配 system_prompt，`rule_version` 写入 task 与审计；
- 审批闸门注册为 `Toolkit` 中间件，任务上下文经 contextvars 绑定，并发任务互不串线；
- thread_id 命名空间分离：会话任务 `chat:{chat_id}:{topic_id}`，定时任务 `sched:{ws}:{job}:{run_id}`（一次性）；
- 工作记忆由 `PgMemory` 持久化到 `agent_working_memory`，重启不丢。

## 3. 调度器（`runtime/scheduler/`）

APScheduler `AsyncIOScheduler` + `SQLAlchemyJobStore(tableschema=ws_<id>)`，jobstore 用独立的小同步连接池，不承载业务查询。每个 Job 固定：

- `coalesce=True`：停机期间多次错过合并为一次；
- `max_instances=1`：上轮未结束本轮跳过；
- `misfire_grace_time`（`MISFIRE_GRACE_SECONDS`，默认 300）：宽限期内补跑，超出跳过；
- 错过 / 跳过事件写 `runtime_state`，可查询不静默。

Scheduler 不执行业务，只构造 `TaskSpec(trigger=schedule, priority=5)` 落库入队。cron 一律走配置 `JOB_CRON_<NAME>`，缺省取 `default_cron`；无 cron 的派生型 Job 只登记不调度。

### 3.1 Job 清单（`scheduler/registry.py`）

| Job | 类型 | 默认 cron | 超时 | 说明 |
| --- | --- | --- | --- | --- |
| `doc_poll` | 确定性 | `*/3 * * * *` | 60s | 监听清单批量取元信息比对版本，有变化派生 `index_refresh`；`stale` 行按小时自愈重刷 |
| `index_refresh` | 派生 | — | 600s | Capability 刷新 `doc_index` 并按章节比对推送订阅群（零 token） |
| `tapd_sync` | 确定性 | `*/5 * * * *` | 120s | 同步进行中需求、字段比对推送；额度水位降级 |
| `repo_sync` | 确定性 | `*/10 * * * *` | 300s | 记录代码仓最近同步时刻（快照新鲜度） |
| `todo_readback` | 确定性 | `*/15 * * * *` | 120s | 回读企微待办，逾期提醒 / 升级 / 归档 / 失效 |
| `loop_digest` | 派生处理器 | `0 9 * * mon` | 900s | Loop-3：👎 反馈 → 回复规约写 biz 层 Rules（UTC） |
| `lease_reaper` | 确定性 | `* * * * *` | 30s | 回收租约过期的僵尸任务 |
| `approval_expiry` | 确定性 | `* * * * *` | 30s | 超时审批置 `EXPIRED`，任务进 `DEAD`，审批卡片原地更新为「已超时取消」 |

`lease_reaper` 与 `approval_expiry` 依赖注入的仓储，由 `container.py` 接线后注册。`index_refresh` / `loop_digest` 是 `CapabilityJob`：Runtime 只做派发与留痕，处理器经 `capability_bridge.py` 进 `CapabilityRouter.handle_job`，未注入时显式 `PortUnavailable`。

### 3.2 TAPD 额度保护

TAPD 无查询剩余额度的接口，水位由本进程滑窗计数估算（`integration/requirement/quota.py`），收到服务端 `QUOTA_EXCEEDED` 直接归零。`tapd_sync` 每轮读水位：低于 `TAPD_QUOTA_SAFE` 降频到 `TAPD_SYNC_REDUCED_INTERVAL`；低于 `TAPD_QUOTA_DISABLED` 停跑，「进行中」判定退化为仅看文档更新时间；恢复后自动解除。降级状态落 `runtime_state`（`degrade:tapd_sync` / `quota:tapd`）并告警。

## 4. Outbox（`runtime/outbox/`）

企微约束：每会话约 30 条/分钟、1000 条/小时；主动推送要求会话已激活；只支持 `markdown` / `template_card`。

**submit（同步落库）**：形态校验（非法 `msg_type` 拒绝 + 告警）→ `urgent` 或无 `merge_key` 直通，否则进合并窗口。

**dispatch_due（扫描到期 PENDING 行）**：

1. **合并窗口**（`merger.py`）：同 `(chat_id, merge_key)` 在 `OUTBOX_MERGE_WINDOW`（默认 60s）内并入幸存行的 `payload.items`，`not_before` 保持首次值不顺延；被并入的行标 `MERGED` 留查；并发同键用 `pg_advisory_xact_lock` 串行。
2. **会话态校验**（`session_guard.py`）：群从未激活 → `DROPPED` + 告警；超 `SESSION_WINDOW_HOURS` 且 `SESSION_WINDOW_ENFORCE` 开 → `SUSPENDED`，下次入站补发，超 `OUTBOX_SUSPEND_RETENTION_DAYS`（默认 7）转 `DROPPED`；开关关闭则放行。
3. **令牌桶**（`throttle.py`）：per-chat 分钟桶 + 小时桶，普通 24/min、800/h，`urgent` 可透支到 30/min、1000/h。取不到令牌按 `retry_after` 重排，不丢弃。桶状态易失，出站事实全在 `outbox` 表。
4. **发送与重试**：失败按 1s → 5s → 30s → 300s 退避，达 `OUTBOX_SEND_MAX_ATTEMPT`（默认 4）标 `FAILED` + 告警；`IsolationViolation` 立即终止。

## 5. 持久化

表定义见 `migrations/0002_init_workspace_schema.sql`，均在 `ws_<id>` 下：

| 表 | 要点 |
| --- | --- |
| `task` | 上文字段；索引 `ux_task_idem`（部分唯一）、`ix_task_poll(status, priority, scheduled_at)`、`ix_task_lease`、`ix_task_parent` |
| `outbox` | `status ∈ PENDING / MERGED / SENT / SUSPENDED / DROPPED / FAILED`，含 `not_before`、`drop_reason`、`sent_msg_id` |
| `audit_log` | `action ∈ tool_call / memory_write / external_req / approval`，只存 `params_digest` / `result_digest`；全模块经 `runtime/audit.py` 写入 |
| `runtime_state` | `(workspace_id, state_key) → jsonb`：降级开关、额度、Job 游标、新鲜度、连续失败计数、群级 Rules |

Worker 不把整个 Task 包在一个事务里：领取、落结果各自是短事务，LLM 与外部调用在事务之外。

## 6. 可观测与失败处理

| 机制 | 实现 |
| --- | --- |
| 执行留痕 | Task 全生命周期落 `task`；工具调用、记忆写入、外部请求、审批落 `audit_log` |
| 连续失败 | 同一 `job_name` 连续 `FAIL_ALERT_STREAK`（默认 3）次失败 → 告警 + 控制台红标，成功后解除 |
| 新鲜度 | `FreshnessTracker` 记 `last_success_at:{job}`；过期标 `stale:{name}`，结论不得说「无变化」并须附快照时间 |
| 链路追踪 | 标准 OpenTelemetry（可选）：每个任务一个 Entry Span，`session_id` 取 `task_id`；未安装即 no-op |
| 告警出口 | `platform/observability/alerting.py`：结构化日志 + 可注册 sink（如企微 webhook） |

## 7. 配置项（`config/properties.py`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `WORKSPACE_ID` | 必填 | 业务线隔离总开关 |
| `PG_DSN` / `PG_POOL_SIZE` | — / 10 | 业务线角色的连接串与池上限 |
| `RUNTIME_WORKERS` | 4 | Worker 协程数 |
| `LLM_MAX_CONCURRENCY` | 4 | 单进程模型并发 |
| `FAST_ACK_SECONDS` / `PROGRESS_INTERVAL_SECONDS` | 5 / 0 | 回执阈值 / 进度间隔（0 不发） |
| `AGENTIC_TASK_TIMEOUT` / `DETERMINISTIC_TASK_TIMEOUT` | 600 / 300 | 超时秒数 |
| `MAX_ATTEMPT` / `FAIL_ALERT_STREAK` | 3 / 3 | 重试上限 / 连续失败告警阈值 |
| `MISFIRE_GRACE_SECONDS` | 300 | 调度宽限期 |
| `JOB_CRON_<NAME>` | 见 3.1 | 各 Job 的 cron |
| `OUTBOX_MERGE_WINDOW` | 60 | 合并窗口秒数 |
| `OUTBOX_RATE_PER_MINUTE` / `_PER_HOUR` | 24 / 800 | 普通消息令牌 |
| `SESSION_WINDOW_ENFORCE` / `SESSION_WINDOW_HOURS` | true / 见下 | 会话窗约束 |
| `TAPD_QUOTA_SAFE` / `TAPD_QUOTA_DISABLED` | 100 / 50 | TAPD 额度水位 |

`SESSION_WINDOW_HOURS` 的代码默认值在 Outbox 侧为 48、Channel 发送侧为 24，部署时应显式配置。
