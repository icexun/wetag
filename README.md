# WeTag

[English](README.en.md) | 中文

运行在**企业微信智能机器人**上的协作助理。在群里 @ 它即可问答、建待办、读写文档与表格、查改需求，并订阅需求/文档变更通知。外部系统通过官方 CLI 接入：企业微信 `wecom-cli`、飞书 `lark-cli`、TAPD `tapd-cli`，按需启用。

## 产品设计

### 定位：群协作 Agent

个人 Agent 服务一个人；WeTag 服务**一个群里的多角色协作**——产品、研发、测试、业务方在同一个上下文里推进事项。

| | 个人 Agent | WeTag 群协作 Agent |
|---|---|---|
| 服务对象 | 单个用户 | 群内多角色 |
| 上下文 | 个人会话 | 群 → 事项，跨人共享 |
| 写操作 | 用户自己负责 | 按风险分级审批，群内可见、可审计 |
| 知识沉淀 | 个人记忆 | 群 / 业务线 / 组织分层记忆与 Rules |
| 触发方式 | 被动问答 | 被动问答 + 变更主动推送 |

![产品架构](docs/images/product-architecture.svg)

### 典型流程

```mermaid
sequenceDiagram
    participant P as 产品
    participant W as WeTag
    participant T as TAPD
    participant F as 飞书文档
    participant D as 研发/测试
    P->>W: @WeTag 按这个 PRD 建需求
    W->>T: 建需求卡（低危写，执行后告知）
    W-->>P: 回复卡片链接
    P->>W: @WeTag 把验收标准补进 PRD
    W-->>P: 高危写：群内审批卡片
    P->>W: 批准（或命中 Auto Mode 规则直通）
    W->>F: 更新文档
    loop 定时轮询
        W->>F: 检查文档变更
        W->>T: 检查需求字段变更
    end
    W-->>D: 变更主动推送到群
```

### 特点

- **群协作 Agent**：以群为协作单元，消息自动归入事项（Topic），多人共享同一份上下文。
- **事项路由**：引用消息 → TAPD 编号 / 飞书链接 → 语义判定 → 新建；原则是宁可新建，不错误合并。
- **业务线硬隔离**：组织 / 业务线 / 群 / 事项四层空间；业务线之间进程、数据库 schema 与角色、凭据全部独立。
- **审批闸门 + Auto Mode**：写操作四级分档，高危写先批后写；Auto Mode 规则可对指定操作免审批直通，审计照记。
- **分层记忆与 Rules**：群 / 业务线 / 组织三层记忆，Rules 按组织 → 业务线 → 群 → 事项逐层覆盖。
- **反馈闭环**：👎 反馈定期提炼成业务线回复规约，下一条消息即生效。护栏：同类反馈达阈值才提炼、限条数与长度、只写自有命名空间不覆盖人工规则、每次变更留痕并群内通知。
- **CLI 优先、端口隔离**：外部系统只经官方 CLI 白名单执行，业务层只依赖 `contracts/ports.py`；集成可插拔。

| 风险档 | 示例 | 处理 |
|---|---|---|
| 只读 | 读文档、查需求、检索代码 | 直接执行 |
| 低危写 | 建待办、建日程、建需求 | 先执行，后告知 |
| 高危写 | 发消息、发邮件、写共享表格、改他人文档 | 群内审批后执行 |
| 删除 | 任何删除 | 不开放，直接拒绝 |

| 记忆层 | 范围 | 写入方式 |
|---|---|---|
| 群 | 本群事实与约定 | 对话中直接写入，低置信度先进待审 |
| 业务线 | 业务线内所有群 | 同上 |
| 组织 | 全部业务线 | 仅提案，需人工确认 |

## 能力

| 集成 | 能力 |
|---|---|
| 企业微信（必需） | 群聊 @ 问答（长连接）、待办、通讯录、消息、日程、在线会议、文档、在线表格、智能表格、微盘、邮件 |
| 飞书（可选） | 云文档读/建/改/搜、电子表格、多维表格、任务、日历、审批查询、知识库；文档变更通知 |
| TAPD（可选） | 需求/缺陷/任务查建改、评论、迭代、成员、变更历史；需求变更通知 |
| 本地代码仓（可选） | 只读代码检索 |

平台能力：

- **审批闸门**：写操作按风险分级，高危写（发消息、发邮件、写共享表格等）先在群里出审批卡片，批准后才执行；删除类操作不开放。
- **多业务线隔离**：一个业务线一个进程、一个 PostgreSQL schema 和专属 DB 角色；凭据只注入当前业务线。
- **可靠投递**：所有入口收敛为持久化 Task，出站经 Outbox，失败显式标记、不静默。
- **语义记忆与规约**：群级/业务线级记忆，👎 反馈自动提炼回复规约。

## 快速开始（Docker）

1. 在企业微信管理后台创建**智能机器人**（API 模式，长连接），记下 Bot ID 与 Secret。
2. 准备配置：

   ```bash
   cp .env.example .env
   # 必填：API_KEY、BASE_URL、MODEL_NAME（任意 OpenAI 兼容端点）、
   #       WECOM_BOT_ID、WECOM_BOT_SECRET、WS_DB_PASSWORD
   ```

3. 启动（PostgreSQL + 建库迁移 + WeTag）：

   ```bash
   docker compose up -d
   docker compose logs -f wetag   # 启动日志会列出各集成的可用状态
   ```

4. 授权 CLI（登录态保存在 `cli-home` 卷）：

   ```bash
   docker compose exec -it wetag wecom-cli auth init   # 企业微信
   docker compose exec -it wetag lark-cli auth login   # 飞书（可选）
   ```

把机器人拉进群并 @ 它即可。只想用企业微信时，在 `.env` 设 `INTEGRATIONS=wecom`。

## 本地开发

需要 Python ≥ 3.11、PostgreSQL ≥ 14，以及要启用的 CLI。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 首次：建 org / 业务线 schema 与角色（管理员连接串）
PG_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/wetag \
  python scripts/init_workspace.py --apply-org --workspace demo --password <WS_DB_PASSWORD>

python -m app.bootstrap   # 生产入口：长连接 + Worker + 调度 + HTTP(:8080)
python main.py            # 仅 Agent 调试入口：OpenAI 兼容 HTTP(:9000)
```

测试：

```bash
pytest                                                    # 需 PG 的用例读 PG_ADMIN_DSN
PG_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/wetag pytest
pyflakes app tests
```

## 配置

优先级：进程环境变量 > `.env` > `config/workspaces/<WORKSPACE_ID>.yaml` > `config/wetag.env` > 代码默认值。全部参数见 [`config/properties.py`](config/properties.py)。

| 变量 | 说明 |
|---|---|
| `WORKSPACE_ID` | 业务线 ID，进程与之 1:1 |
| `API_KEY` / `BASE_URL` / `MODEL_NAME` | 模型（OpenAI 兼容） |
| `WECOM_BOT_ID` / `WECOM_BOT_SECRET` | 企业微信智能机器人 |
| `PG_DSN` | 业务线角色连接串 |
| `INTEGRATIONS` | 启用的集成，默认 `tapd,lark,wecom` |
| `LARK_CLI_TOKEN` | 飞书（可选，也可用 `lark-cli auth login`） |
| `TAPD_TOKEN` / `TAPD_API_ENDPOINT` | TAPD 个人令牌与 API 地址 |

业务线配置（TAPD 项目、飞书域名、代码仓路径等）写在 `config/workspaces/<id>.yaml`，以 [`demo.yaml`](config/workspaces/demo.yaml) 为模板；该目录除 demo 外不入库。

集成未启用或 CLI 不在 PATH 上时：对应端口标记不可用、不注册其工具与 skill、不调度其轮询任务，启动日志会写明原因。

## 技术架构

![技术架构](docs/images/tech-architecture.svg)

依赖方向 `gateway → space → capability → control`；`runtime` 横切，`analytics` 只读；`contracts` / `platform` 不依赖业务模块。

| 取舍 | 原因 |
|---|---|
| 企微智能机器人长连接 | 无需公网回调地址，内网即可部署 |
| 一业务线一进程 | 故障、凭据、数据天然隔离，扩容按业务线水平拆 |
| 所有入口收敛为持久化 Task | 群消息、定时任务、卡片回调走同一队列，可重试、可审计 |
| 确定性 Job 与 Agent 分离 | 轮询、同步不调模型，成本可控、结果可预期 |
| CLI 为主，业务层只依赖 Ports | 复用官方 CLI 的鉴权与能力；换实现不动业务代码 |

![业务线隔离](docs/images/isolation.svg)

- `app/gateway`：企微长连接协议、排重、卡片
- `app/space`：Bot → 业务线 → 群 → 会话事项的归属与路由
- `app/capability`：工作流、语义节点、外部系统端口与 Agent 工具
- `app/control`：规约、审批闸门、语义记忆、待办台账
- `app/runtime`：Task 队列、WorkerPool、调度、Outbox、审计
- `app/contracts` / `app/platform`：契约与技术底座（DB、CLI 执行器、可观测）

详见 [`docs/design/`](docs/design/overall.md)。

## 贡献与安全

- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 漏洞报告：[SECURITY.md](SECURITY.md)

## 许可证

[Apache-2.0](LICENSE)
