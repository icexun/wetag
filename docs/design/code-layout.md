# WeTag 代码目录

本文定目录与文件的归属与依赖方向；设计理由见 `overall.md`。目录结构的目标是让违反依赖方向的代码「看起来就是错的」。

## 1. 顶层

```
wetag/
├── main.py              # 单 Agent 调试入口（不承担生产启动）
├── utils.py             # create_agent / 共享 Toolkit 装配（注册本地工具与 MCP）
├── requirements.txt
├── mcp.example.json     # MCP 受控补充的服务器声明样例
├── .env.example         # 环境变量样例
├── config/              # 进程级配置读取层（properties.py、persona.md、workspaces/*.yaml）
├── app/                 # 全部业务代码
├── migrations/          # PG schema 迁移（唯一变更途径）
├── skills/              # AgentScope Skill 资产；feishu-* / tapd-* / wecom-* 随集成启停
├── deploy/              # supervisor 模板、DB 首次初始化
├── scripts/             # 运维脚本
└── tests/               # 与 app/ 一一对应
```

生产入口是 `app/bootstrap.py`。`config/` 放在仓库根：`app/` 内所有模块与 `main.py` 都依赖它。配置解析顺序：进程环境 → `.env` → `config/workspaces/<WORKSPACE_ID>.yaml` → `config/wetag.env` → 代码默认值。

## 2. `app/`

```
app/
├── bootstrap.py         # 装配、起 asyncio 循环、挂 HTTP
├── container.py         # 按 WORKSPACE_ID 构造各模块与 Port 绑定；装配期探测端口可用性
├── contracts/           # 跨模块契约，任何模块可 import
├── platform/            # 技术底座，任何模块可 import
├── gateway/             # Channel
├── space/               # Space
├── capability/          # Capability
├── control/             # Control
├── runtime/             # Runtime
└── analytics/           # Analytics
```

### 2.1 `contracts/`

只含数据结构与 Protocol，无业务依赖。

```
contracts/
├── space_context.py     # SpaceContext 与 workspace 断言
├── task.py              # Task / TaskSpec / TaskKind / TriggerSource / TaskStatus
│                        #   WorkflowKind / JobKind / AgenticTask / DeterministicJob / TaskSink
├── envelope.py          # Envelope / MsgType / Priority
├── job_result.py        # JobResult：derived_tasks + envelopes；incomplete(reason)
├── ports.py             # DocumentPort / RequirementPort / TodoPort / CodeRepositoryPort
│                        #   + Space 侧契约 TopicClassifier / SpaceContextProvider / ChatDirectory
├── ids.py               # WorkspaceId / ChatId / TopicId / TaskId / DocumentId 等命名类型
└── errors.py            # IsolationViolation / PortUnavailable / ApprovalRequired / RateLimited 等
```

`IsolationViolation` 单独成类：DB 权限错误与归属不符按安全事件处理，不进通用重试路径。

### 2.2 `platform/`

```
platform/
├── db/
│   ├── pool.py          # 每进程一个 asyncpg 池；search_path / 超时走 server_settings
│   ├── repository.py    # Repository 基类：_bind() 语句层校验、_guard_rows() 行级校验、短事务
│   ├── schema.py        # ws_<id> / wetag_ws_<id> 命名、标识符白名单（全仓唯一拼标识符处）
│   └── migration_check.py  # 启动校验迁移版本与 checksum
├── cli/
│   ├── executor.py      # CliExecutor：参数数组执行、白名单、凭据最小注入、审计
│   ├── registry.py      # 命令白名单（删除类不登记）
│   ├── result.py        # CliResult 与摘要脱敏
│   └── error_map.py     # 错误码归一化 → 重试 / 告警 / 降级
├── observability/
│   ├── entry_span.py    # OpenTelemetry Entry Span（可选依赖，缺失即 no-op）
│   ├── trace_context.py # trace_id 传播；thread_id 命名空间 sched: / chat:
│   └── alerting.py      # 告警出口（结构化日志 + 可注册 sink）
├── memory/working.py    # PG 持久化 Agent 工作记忆（agentscope MemoryBase 实现）
├── concurrency/locks.py # chat_locks / repo_lock / index_lock / LLM 信号量
└── degradation.py       # 降级开关与新鲜度（落 runtime_state）
```

`cli/` 放 platform：CliExecutor 关注进程安全而非业务。`locks.py` 放 platform：锁同时被 Runtime 与 Capability 使用。

### 2.3 `gateway/`（Channel）

```
gateway/
├── client.py            # 官方 wecom-aibot-python-sdk 长连接
├── inbound.py           # aibotid 校验 → 归一化 → 转 Task
├── event.py             # AgentEvent（Channel 内部形态）
├── outbound.py          # 发送原语：归属校验、回复窗口过期降级为主动推送；不持有队列
├── reply_window.py      # 暂存可回复的原始帧（5s 事件回复 / 10min 流式窗口）
├── cards/builder.py     # 模板卡片（处理中 / 确认 / 告警，多条目渲染）
├── cards/binding.py     # card_task 安全绑定与 CAS 状态迁移
├── actions.py           # 按钮回调 → card_action Task
├── feedback.py          # feedback_record 落库
├── quote.py             # 引用消息识别（事项路由 L1）
└── repository.py        # card_task / feedback_record
```

### 2.4 `space/`

```
space/
├── resolver.py          # aibotid → workspace 静态映射（BOT_BINDINGS）
├── ownership.py         # org.chat_ownership 原子登记
├── registry.py          # chat_group upsert；ChatDirectory 实现
├── topic/router.py      # 事项路由 L1 / L2 / L2.5，L3 经 TopicClassifier，L4 新建
├── topic/lifecycle.py   # 事项状态与活跃时间
├── topic/correction.py  # topic_correction 纠正样本
├── config_assembler.py  # 消费 Control 的配置版本，写入 SpaceContext
├── context_factory.py   # SpaceContext 唯一构造点
└── repository.py        # chat_group / topic / topic_correction / chat_ownership
```

### 2.5 `capability/`

```
capability/
├── router.py            # WorkflowKind / JobKind → 处理器分派；未登记即 incomplete
├── agents/
│   ├── factory.py       # 模型与 Agent 装配，共享 LLM 信号量，语义节点 temperature=0
│   ├── structured.py    # 结构化输出客户端（所有语义判断的唯一入口）
│   ├── lark_tools.py    # feishu_doc_fetch / create / append / update / search
│   ├── lark_office_tools.py   # 飞书表格、多维表格、任务、日历、审批（只读）、知识库、通讯录
│   ├── tapd_tools.py    # tapd_story/bug/task 查建改、评论、迭代、成员、字段定义
│   ├── wecom_tools.py   # wecom_todo_create
│   ├── wecom_office_tools.py  # 企微通讯录、消息、日程、会议、文档、表格、微盘、邮件
│   ├── subscription_tools.py  # wetag_subscribe_changes / unsubscribe / list_subscriptions
│   └── prompts/         # 随代码发布的固定提示词（structuring.md）
├── workflows/
│   ├── base.py          # Workflow 基类：顺序固定、事务与外部调用分离
│   ├── requirement_create.py  # 结构化 → 澄清闸门 → TAPD 建卡 → 飞书文档 → 关联落库
│   ├── doc_update.py    # 文档更新（乐观锁）与 doc_index 刷新
│   ├── tapd_watch.py    # tapd_sync 的同步处理器：需求投影 + 字段比对
│   └── change_notify.py # 章节 / 字段确定性比对、渲染、Notifier 按订阅展开 Envelope
├── intelligence/
│   ├── structuring.py   # 群聊讨论 → RequirementDraft；diff 确定性
│   ├── topic_route.py   # 事项路由 L3 语义判定
│   └── embedding.py     # GatewayEmbedder / LexicalEmbedder
├── integration/
│   ├── document/        # lark_cli.py（DocumentPort）、urls.py
│   ├── requirement/     # tapd_cli.py / normalize.py / quota.py / urls.py
│   ├── todo/wecom.py    # TodoPort 唯一实现
│   ├── repo/            # sync.py（浅克隆与快照）/ search.py（rg / git grep）/ service.py
│   └── idempotency.py   # 幂等护栏 claim → 外部写 → settle
└── repository.py        # requirement / doc_index / requirement_relation / chat_subscription /
                         #   idempotency_record / rule_change_log
```

每个 Port 一个子目录：切换实现时新增文件而不改文件。

### 2.6 `control/`

```
control/
├── rules/
│   ├── assembler.py     # 四层 Rules：组织 → 业务线 → 群 → 事项，产出 rule_version 指纹
│   └── middleware.py    # 执行前注入 system_prompt；超长拒绝并回退上一版
├── approval/
│   ├── classifier.py    # 只读 / 低危写 / 高危写 / 禁止 四级分档
│   ├── gate.py          # Toolkit 中间件：唯一的工具调用把关点
│   ├── auto_mode.py     # Auto Mode 自动批准规则（无法解析即视为未命中）
│   └── pending.py       # pending_approval 状态机
├── memory/
│   ├── collections.py   # 三层 collection 解析
│   ├── recall.py        # 当前群 + 当前业务线 + 组织级
│   ├── remember.py      # 分层写入、去重、回复后自动沉淀
│   ├── backend.py       # AgentRun 托管 collection 读写
│   └── policy/          # pending_review / archive_distill / conflict_mark
├── ledger/todo_ref.py   # 待办引用，只存归属与提醒计数
└── repository.py        # Rules 读写 + pending_approval / pending_memory / memory_conflict / todo_ref
```

Control 不拥有任何 worker。

### 2.7 `runtime/`

```
runtime/
├── scheduler/engine.py  # APScheduler + SQLAlchemyJobStore（jobstore 落 ws_<id>）
├── scheduler/registry.py # Job 注册表；cron 取 JOB_CRON_<NAME>，缺省用 default_cron
├── jobs/
│   ├── base.py          # Job / CapabilityJob / LeaseReaperJob / ApprovalExpiryJob
│   ├── doc_poll.py      # 确定性：文档版本比对 → 派生 index_refresh
│   ├── tapd_sync.py     # 确定性：需求同步与额度降级
│   ├── repo_sync.py     # 确定性：代码仓同步新鲜度
│   ├── todo_readback.py # 确定性：待办回读、提醒、升级
│   ├── index_refresh.py # 派生型，处理器在 Capability
│   └── loop_digest.py   # 每周，处理器在 Capability（Loop-3）
├── worker/
│   ├── pool.py          # WorkerPool：CAS 领取、加锁、执行、留痕、重试
│   ├── queue.py         # 内存优先级队列 + 启动时从 task 表恢复
│   ├── lease.py         # 续租与僵尸回收
│   └── agent_runner.py  # agent_reply：Task → 一次 AgentScope 执行（挂审批闸门）
├── outbox/
│   ├── dispatcher.py    # 四道关卡编排
│   ├── merger.py        # 合并窗口
│   ├── session_guard.py # 会话态校验
│   └── throttle.py      # per-chat 双层令牌桶
├── capability_bridge.py # WorkflowKind / CapabilityJob → CapabilityRouter 的接线
├── fastack.py           # 5s 回执、可选进度、终态原地更新
├── audit.py             # audit_log 唯一写入面
├── state.py             # runtime_state：降级、额度、游标、连续失败
└── repository.py        # task / outbox / audit_log / runtime_state
```

### 2.8 `analytics/`

```
analytics/
├── queries/             # usage / push / todo / memory / cost（只读聚合）
├── loops/loop2_todo.py  # 待办跟进策略提案
├── loops/loop3_push.py  # 👎 → 回复规约 → biz 层 Rules
└── console/api.py       # 控制台 HTTP 路由
```

Analytics 没有自己的表；Loop-3 的规则写入经注入的 Control 写入端完成。

## 3. `app/` 之外

```
migrations/
├── 0001_init_org_schema.sql       # org：schema_migration / chat_ownership / org_config
├── 0002_init_workspace_schema.sql # task / outbox / audit_log / runtime_state
├── 0003_space_tables.sql          # chat_group / topic / topic_correction
├── 0004_channel_tables.sql        # card_task / feedback_record
├── 0005_capability_tables.sql     # requirement / doc_index / requirement_relation /
│                                  #   chat_subscription / idempotency_record / rule_change_log
├── 0006_control_tables.sql        # pending_approval / pending_memory / memory_conflict /
│                                  #   todo_ref / agent_working_memory(_meta)
├── 0007_roles_and_grants.sql      # 业务线角色、schema 授权、org 限权、search_path
└── version.py                     # EXPECTED_SCHEMA_VERSION 与 checksum 算法

deploy/
├── supervisor/wetag.conf.example  # 一 workspace 一进程模板
└── db/bootstrap.sql               # 建库（超级用户执行一次，幂等）

scripts/
├── init_workspace.py    # 建 schema、角色，按序应用迁移并记 checksum
├── verify_isolation.py  # 用 A 业务线角色访问 B 的 schema，期望权限错误
├── check_wecom_todo.py  # 企微待办域连通性检查
├── smoke_wecom.py       # 长连接冒烟
└── smoke_card.py        # 卡片发送冒烟
```

迁移以 `@ws_schema@` / `@ws_role@` 为占位符，`0001` 只在 org 跑一次，`0002`–`0007` 每个业务线各跑一次。已发布文件只增不改，表结构调整新增 `0008_*.sql`。

`skills/` 与 `capability/agents/prompts/` 的分界：Skill 是给对话 Agent 的可替换资产，prompts 是随代码发布的语义节点提示词。

## 4. Import 方向

| 源 | 允许 import | 禁止 import |
| --- | --- | --- |
| `contracts/` | 标准库、pydantic | 任何 app 模块 |
| `platform/` | `contracts/`、`config` | 任何业务模块 |
| `gateway/` | `contracts/`、`platform/` | `space/` 及以下 |
| `space/` | `contracts/`、`platform/` | 其余业务模块 |
| `capability/` | `contracts/`、`platform/`、`control/`（只读） | `gateway/`、`runtime/` |
| `control/` | `contracts/`、`platform/` | 其余业务模块 |
| `runtime/` | 全部（横切） | `analytics/` |
| `analytics/` | `contracts/`、`platform/` 与各模块只读查询 | — |

Channel 需要的归属解析与出站归属事实经 `contracts/ports.py` 的 `SpaceContextProvider` / `ChatDirectory` 反转，绑定在 `container.py`。最该在 CI 卡住的三条：`capability/` 不 import `gateway/`（出站只能经 Outbox）；`runtime/` 不 import `analytics/`；`contracts/` 不 import 任何 app 模块。
