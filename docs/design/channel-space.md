# Channel 与 Space

Channel（`app/gateway/`）只做企微协议与消息可靠性，Space（`app/space/`）负责 Bot → 业务线 → 群 → 会话事项 的归属。目录见 `code-layout.md`。

## 1. 代码落点

| 职责 | 落点 |
| --- | --- |
| 长连接、心跳、重连、被踢退出 | `gateway/client.py`（官方 `wecom-aibot-python-sdk`） |
| 协议帧 → AgentEvent、入站编排 | `gateway/inbound.py`、`gateway/event.py` |
| 引用消息识别 | `gateway/quote.py` |
| 按钮回调 → Task | `gateway/actions.py` |
| 反馈落库 | `gateway/feedback.py` |
| 发送原语、回复窗口 | `gateway/outbound.py`、`gateway/reply_window.py` |
| 模板卡片渲染 / 安全绑定 | `gateway/cards/builder.py` / `gateway/cards/binding.py` |
| `card_task` / `feedback_record` | `gateway/repository.py`（`migrations/0004`） |
| `aibotid → 业务线` 静态映射 | `space/resolver.py` |
| `org.chat_ownership` 原子登记 | `space/ownership.py` |
| `chat_group` 登记与出站归属查询 | `space/registry.py` |
| SpaceContext 构造 | `space/context_factory.py` |
| 事项路由 / 状态 / 纠正样本 | `space/topic/router.py` / `lifecycle.py` / `correction.py` |
| `chat_group` / `topic` / `topic_correction` | `space/repository.py`（`migrations/0003`） |

装配在 `app/container.py` 的 `build_channel()`，Runtime 传入连接池与 `PersistentTaskSink`（落 `task` 表并入队）。

## 2. 契约

| 抽象 | 声明 | 依赖方 | 实现方 |
| --- | --- | --- | --- |
| `TaskSink` / `SubmitResult` | `contracts/task.py` | Channel | Runtime（`PersistentTaskSink`） |
| `SpaceContextProvider` / `InboundRef` | `contracts/ports.py` | Channel | `space/context_factory.py` |
| `ChatDirectory` / `ChatBinding` | `contracts/ports.py` | Channel | `space/registry.py` |
| `TopicClassifier` | `contracts/ports.py` | Space | Capability（`intelligence/topic_route.py`） |
| `ConfigVersionProvider` | `space/config_assembler.py` | Space | Control（Rules 装配） |
| `Database` | `platform/db/pool.py` | Channel / Space | `PostgresDatabase` |

## 3. 入站

```
帧 → aibotid 必须等于本进程 bot_id（否则拒绝）
   → 归一化为 AgentEvent（原始帧保留在 raw）
   → InboundRef → Space：chat_ownership 登记 → chat_group upsert（刷新 last_inbound_at）→ 事项路由
   → TaskSink.submit（msgid 落 task.idem_key，DUPLICATE 即重复投递）
```

- 入站只做校验、登记、生成 priority=1 的 Task，不做业务判断，长连接读循环不阻塞。
- 排重不设独立表，唯一去重点是 `task.idem_key`。
- 按钮事件只推送一次且须 5 秒内响应：先在 `card_task` 上 CAS 抢占处理权，再把原卡片更新为受理态，最后投递 `card_action` Task。
- `card_task.allowed_actors` 限定可点击的人（审批卡片限定审批人），空数组表示群内任意成员。
- 反馈原样落 `feedback_record`，同一用户对同一回复的重复反馈由唯一约束吞掉；事项归属从 `card_task` 回查。

## 4. 归属与事项路由

- `aibotid` 是业务线归属的唯一权威来源。单业务线部署只需 `WORKSPACE_ID` + `WECOM_BOT_ID`；多 Bot 用 `BOT_BINDINGS` 声明映射，进程仍与业务线 1:1，且 `WORKSPACE_ID` 不得与映射矛盾。不建立用户、群名到业务线的映射。
- 群首次 @ 时 `INSERT ... ON CONFLICT (chat_id) DO NOTHING` 后回读：已归属其他业务线则本 Bot 不执行任务。**单聊不登记**：单聊 `chat_id` 即用户 id，同一人与多个业务线 Bot 单聊是正常场景。
- 单聊默认关闭（`SINGLE_CHAT_ENABLED=false`），只回引导语。
- 事项路由按可靠度递减：L1 引用消息 → L2 外部标识（TAPD 编号 / 飞书链接）→ L2.5 超短应答归最近事项 → L3 语义判定（`TopicClassifier`，未注入或无把握时跳过）→ L4 新建。宁可新建，不错误合并。
- 自然语言纠正只落 `topic_correction` 供离线评测，不即时迁移消息。
- 事项超时无活动归档（`TOPIC_IDLE_ARCHIVE_HOURS`），结论提炼归 Control 记忆策略层。

## 5. 出站

Channel 不持有出站队列；Outbox（Runtime）调用 `outbound.py` 的发送原语。发送原语负责两件必须贴着协议做的事：

1. **归属校验**：目标会话经 `ChatDirectory` 查到的归属必须是本业务线且已激活，否则显式拒绝，不寻找替代目标。
2. **回复窗口降级**：回复须透传原始帧的 `req_id`，`reply_window.py` 按 `event_id` 暂存帧；事件回复 5 秒、流式消息 10 分钟窗口过期后自动改走主动推送（`aibot_send_msg`）。

Channel 不 import Space：入站递出 `InboundRef`（Space 因此不认识 `AgentEvent`），出站取回 `ChatBinding`（Channel 因此不读 `chat_group`），绑定在 `container.py`。

首次进群欢迎语留 `welcome_handler` 钩子，未注入时不回复。

## 6. 本地运行

```bash
cp config/workspaces/demo.yaml config/workspaces/<id>.yaml   # 按需修改
export WORKSPACE_ID=demo WECOM_BOT_ID=... WECOM_BOT_SECRET=...

# 首次：以超级用户建库，再用管理员连接建 schema、角色并跑迁移
psql -d postgres -f deploy/db/bootstrap.sql
PG_ADMIN_DSN=postgresql://<admin>:...@<host>:5432/wetag \
    python scripts/init_workspace.py --workspace=demo --password=... --apply-org
python scripts/verify_isolation.py ...                         # 可选：隔离验收

export PG_DSN=postgresql://wetag_ws_demo:...@<host>:5432/wetag
python -m app.bootstrap
```

启动只校验迁移版本（`platform/db/migration_check.py`），不匹配拒绝启动；迁移应用由 `init_workspace.py` 承担，因为建 `org` schema 与角色授权需要管理员权限，业务线角色对 `org` 只有 `SELECT` 与 `chat_ownership` 的 `INSERT`。

多 Bot 映射示例：

```json
{"bot_a": {"scope_type": "WORKSPACE", "workspace_id": "team_a"},
 "bot_b": {"scope_type": "WORKSPACE", "workspace_id": "team_b"},
 "bot_org": {"scope_type": "ORGANIZATION", "workspace_id": "demo"}}
```
