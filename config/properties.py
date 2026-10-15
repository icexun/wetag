"""Typed configuration property classes — Spring @ConfigurationProperties equivalent.

Usage (like @Autowired injection in Spring):
    from config.properties import ModelProperties

    model_props = ModelProperties.instance()
    print(model_props.name)         # → config chain lookup for MODEL_NAME
    print(model_props.temperature)  # → config chain lookup for MODEL_TEMPERATURE
"""

from config import ConfigurationProperties, Value


class ServerProperties(ConfigurationProperties):
    """Server configuration."""

    port = Value("PORT", default=9000)
    host = Value("HOST", default="0.0.0.0")
    memory_collection_name = Value("MEMORY_COLLECTION_NAME")


class ModelProperties(ConfigurationProperties):
    """LLM model configuration. Any OpenAI-compatible endpoint."""

    name = Value("MODEL_NAME", default="gpt-4o-mini")
    api_key = Value("API_KEY")
    base_url = Value("BASE_URL", default="https://api.openai.com/v1")
    temperature = Value("MODEL_TEMPERATURE", default=0.7)
    parameter_rules = Value("MODEL_PARAMETER_RULES")
    # 部分 OpenAI 兼容网关不支持 stream=true，会回 event:error，表现为 Agent 空回复且无异常
    stream = Value("MODEL_STREAM", default=False)
    custom_headers = Value("CUSTOM_HEADERS")
    custom_query = Value("CUSTOM_QUERY")


class AgentProperties(ConfigurationProperties):
    """Agent behavior configuration."""

    prompt = Value("PROMPT", default="You are a helpful AI assistant.")
    # 人设模板：{{KEY}} 按配置展开；某行占位符未配置时整行省略
    persona_file = Value("PERSONA_FILE", default="config/persona.md")
    bot_name = Value("BOT_NAME", default="WeTag")
    prompt_variables = Value("PROMPT_VARIABLES")
    max_iters = Value("REACT_MAX_ITERS", default=50)
    tool_call_timeout = Value("TOOL_CALL_TIMEOUT", default=300.0)
    skills_dir = Value("SKILLS_DIR")


class ToolProperties(ConfigurationProperties):
    """Tool and MCP server configuration."""

    api_tools = Value("API_TOOLS")
    mcp_tools = Value("MCP_TOOLS")
    sandbox_mcp_tools = Value("SANDBOX_MCP_TOOLS")
    mcp_init_timeout = Value("MCP_INIT_TIMEOUT", default=60.0)
    # 飞书 / 企微协作工具单次返回给模型的上限（字符），超出截断并提示缩小范围
    office_result_max_chars = Value("OFFICE_TOOL_RESULT_MAX_CHARS", default=20000)


class WorkspaceProperties(ConfigurationProperties):
    """业务线身份（D-2：一业务线一进程，隔离总开关）。

    ``workspace_id`` 决定 DB schema 与角色、记忆 collection、大仓根目录与凭据分组。
    §4.2 的等式要求 ``bot_id`` 与进程绑定，任一环不等立即拒绝执行。
    """

    workspace_id = Value("WORKSPACE_ID")
    organization_id = Value("ORGANIZATION_ID", default="default")
    bot_id = Value("WECOM_BOT_ID")
    bot_secret = Value("WECOM_BOT_SECRET")
    http_port = Value("WETAG_HTTP_PORT", default=8080)
    bot_bindings = Value("BOT_BINDINGS")
    """aibotid → 空间的静态映射表（JSON）。形如
    ``{"bot_demo": {"scope_type": "WORKSPACE", "workspace_id": "demo"}}``。
    单业务线部署留空即可——进程自身的 workspace_id + bot_id 就是完整绑定。
    """
    integrations = Value("INTEGRATIONS", default="tapd,lark,wecom")
    """启用的外部集成（tapd / lark / wecom）。YAML 写 ``integrations: [wecom]``。
    未启用的集成不装配端口、不注册工具、不起轮询 Job。
    """


class WecomProperties(ConfigurationProperties):
    """企微智能机器人长连接凭据与运行参数。"""

    bot_id = Value("WECOM_BOT_ID")
    bot_secret = Value("WECOM_BOT_SECRET")
    ws_url = Value("WECOM_WS_URL")
    heartbeat_interval_ms = Value("WECOM_HEARTBEAT_INTERVAL_MS", default=30000)
    reconnect_interval_ms = Value("WECOM_RECONNECT_INTERVAL_MS", default=1000)
    max_reconnect_attempts = Value("WECOM_MAX_RECONNECT_ATTEMPTS", default=-1)
    request_timeout_ms = Value("WECOM_REQUEST_TIMEOUT_MS", default=10000)
    session_window_enforce = Value("SESSION_WINDOW_ENFORCE", default=True)
    """主动推送是否强制 24 小时会话窗校验。M0 实测可跳窗后关掉，无需改代码。"""
    session_window_hours = Value("SESSION_WINDOW_HOURS", default=24)
    single_chat_enabled = Value("SINGLE_CHAT_ENABLED", default=False)
    """私聊是否纳入本期（待确认 #15）。兜底只做群聊，私聊回引导语。"""
    single_chat_guide = Value(
        "SINGLE_CHAT_GUIDE",
        default="WeTag 当前只在群聊中提供服务，请在需求群内 @ 我。",
    )


class TopicProperties(ConfigurationProperties):
    """会话事项路由参数。"""

    idle_archive_hours = Value("TOPIC_IDLE_ARCHIVE_HOURS", default=24)
    max_candidates = Value("TOPIC_MAX_CANDIDATES", default=5)
    title_max_length = Value("TOPIC_TITLE_MAX_LENGTH", default=40)


class PgProperties(ConfigurationProperties):
    """PostgreSQL 连接（§9.4）。

    ``dsn`` 是业务线专属角色，连接串中不存在访问他人 schema 的权限；
    ``admin_dsn`` 仅供 ``scripts/init_workspace.py`` 与集成测试建库使用，
    生产进程不应配置它。
    """

    dsn = Value("PG_DSN")
    admin_dsn = Value("PG_ADMIN_DSN")
    pool_min = Value("PG_POOL_MIN", default=2)
    # §9.4-2：池大小 × 进程数受 max_connections 约束
    pool_max = Value("PG_POOL_SIZE", default=10)
    connect_timeout_s = Value("PG_CONNECT_TIMEOUT_S", default=5.0)
    command_timeout_s = Value("PG_COMMAND_TIMEOUT_S", default=15.0)
    statement_timeout_ms = Value("PG_STATEMENT_TIMEOUT_MS", default=10000)
    # §9.3：获取连接等待超过该阈值即判定池耗尽，任务回 PENDING 而非无限等待
    acquire_timeout_s = Value("PG_ACQUIRE_TIMEOUT_S", default=5.0)
    # 短事务软阈值：超过即打 WARN 带调用栈，用于抓「把 Workflow 包进一个事务」的违规
    tx_soft_limit_ms = Value("PG_TX_SOFT_LIMIT_MS", default=2000)


class CliProperties(ConfigurationProperties):
    """外部 CLI 可执行文件与资源限制（§4.5 CliExecutor）。"""

    lark_bin = Value("LARK_CLI_BIN", default="lark-cli")
    tapd_bin = Value("TAPD_CLI_BIN", default="tapd-cli")
    wecom_bin = Value("WECOM_CLI_BIN", default="wecom-cli")
    rg_bin = Value("RG_BIN", default="rg")
    git_bin = Value("GIT_BIN", default="git")
    timeout_s = Value("CLI_TIMEOUT_S", default=30.0)
    max_output_bytes = Value("CLI_MAX_OUTPUT_BYTES", default=8388608)
    max_attempts = Value("CLI_MAX_ATTEMPTS", default=3)
    # 凭据：按 workspace 注入，只注入当前业务线的一组
    lark_token = Value("LARK_CLI_TOKEN")
    tapd_token = Value("TAPD_TOKEN")
    tapd_endpoint = Value("TAPD_API_ENDPOINT", default="https://api.tapd.cn")


class CapabilityProperties(ConfigurationProperties):
    """Capability 能力阈值（全部外置，不写成代码常量）。"""

    # 文档 fetch 并发，与 LLM 并发分开计量
    fetch_concurrency = Value("FETCH_CONCURRENCY", default=3)
    llm_max_concurrency = Value("LLM_MAX_CONCURRENCY", default=4)
    # 语义判断温度。默认 0 保可复现；部分推理模型的网关拒绝 temperature≠1.0
    # （间歇 400），此类环境配 1.0——推理模型的 temperature 被引擎忽略，可复现性不受影响。
    deterministic_temperature = Value("CAPABILITY_TEMPERATURE", default=0.0)
    # 单次结构化调用的上限。需求结构化要一次性回十几个字段外加验收标准，实测 90 秒
    # 不够——而超时的后果不是慢，是那个语义节点 ok=False、结论整体变成未完成。
    llm_timeout_s = Value("LLM_TIMEOUT_S", default=180.0)
    # 文档快照向量。留空即退到进程内词法向量（不联网、可复现）；配上模型名才走网关。
    # EMBEDDING_DIM 必须与所配模型的真实维度一致，否则库里混进两种不可比的向量。
    embedding_model = Value("EMBEDDING_MODEL")
    embedding_dim = Value("EMBEDDING_DIM", default=1024)
    embedding_timeout_s = Value("EMBEDDING_TIMEOUT_S", default=20.0)
    # 大仓：一业务线一个聚合大仓，只读
    repo_root = Value("REPO_ROOT")
    repo_branch = Value("REPO_BRANCH", default="main")
    repo_shallow_depth = Value("REPO_SHALLOW_DEPTH", default=1)


class RuntimeProperties(ConfigurationProperties):
    """Runtime 进程配置：阈值、频率、开关全部走配置，不写死（US2-4 / plan Decision 6）。

    JOB_CRON_* 为各常驻 Job 的 cron 表达式；改配置重启即生效，无需改代码。
    """

    workspace_id = Value("WORKSPACE_ID")
    workers = Value("RUNTIME_WORKERS", default=4)
    llm_max_concurrency = Value("LLM_MAX_CONCURRENCY", default=4)
    fast_ack_seconds = Value("FAST_ACK_SECONDS", default=5.0)
    # ≤0 关闭「仍在处理中」进度消息：推送通道无原地更新，每次进度都是新消息
    progress_interval_seconds = Value("PROGRESS_INTERVAL_SECONDS", default=0.0)
    agentic_task_timeout = Value("AGENTIC_TASK_TIMEOUT", default=600.0)
    deterministic_task_timeout = Value("DETERMINISTIC_TASK_TIMEOUT", default=300.0)
    max_attempt = Value("MAX_ATTEMPT", default=3)
    misfire_grace_seconds = Value("MISFIRE_GRACE_SECONDS", default=300)
    fail_alert_streak = Value("FAIL_ALERT_STREAK", default=3)
    # 常驻 Job 调度频率（cron 表达式，分 时 日 月 周）
    job_cron_doc_poll = Value("JOB_CRON_DOC_POLL", default="*/3 * * * *")
    job_cron_tapd_sync = Value("JOB_CRON_TAPD_SYNC", default="*/5 * * * *")
    job_cron_repo_sync = Value("JOB_CRON_REPO_SYNC", default="*/10 * * * *")
    job_cron_todo_readback = Value("JOB_CRON_TODO_READBACK", default="*/15 * * * *")
    job_cron_loop_digest = Value("JOB_CRON_LOOP_DIGEST", default="0 9 * * mon")
    job_cron_lease_reaper = Value("JOB_CRON_LEASE_REAPER", default="* * * * *")
    # TAPD 额度水位（US2-10）：低于 safe 降频，低于 disabled 停跑
    tapd_quota_safe = Value("TAPD_QUOTA_SAFE", default=100)
    tapd_quota_disabled = Value("TAPD_QUOTA_DISABLED", default=50)
    tapd_sync_reduced_interval = Value("TAPD_SYNC_REDUCED_INTERVAL", default=1800.0)


class OutboxProperties(ConfigurationProperties):
    """出站四道关卡配置：阈值、开关全部走配置，不写死（US5-9 / plan Decision 6）。"""

    merge_window_seconds = Value("OUTBOX_MERGE_WINDOW", default=60.0)
    rate_per_minute = Value("OUTBOX_RATE_PER_MINUTE", default=24)
    rate_per_hour = Value("OUTBOX_RATE_PER_HOUR", default=800)
    urgent_rate_per_minute = Value("OUTBOX_URGENT_RATE_PER_MINUTE", default=30)
    urgent_rate_per_hour = Value("OUTBOX_URGENT_RATE_PER_HOUR", default=1000)
    session_window_hours = Value("SESSION_WINDOW_HOURS", default=48.0)
    session_window_enforce = Value("SESSION_WINDOW_ENFORCE", default=True)
    suspend_retention_days = Value("OUTBOX_SUSPEND_RETENTION_DAYS", default=7)
    send_max_attempt = Value("OUTBOX_SEND_MAX_ATTEMPT", default=4)


class ControlProperties(ConfigurationProperties):
    """Control 配置：Rules 上限、审批超时、记忆阈值等（plan §配置表）。"""

    rules_max_chars = Value("RULES_MAX_CHARS", default=8000)
    approval_timeout_minutes = Value("APPROVAL_TIMEOUT_MINUTES", default=30)
    # Auto Mode 自动批准规则（US7-4）：JSON 列表，命中即免审批直通（审计仍记
    # rule_version）。形如 [{"tool": "feishu_doc_update", "params": {
    #   "doc_url": "^https://example\\.feishu\\.cn/wiki/.*",
    #   "command": "^(append|str_replace|block_insert_after|block_replace)$"}}]。
    # params 值按正则 fullmatch 匹配整个 str(参数值)（前缀圈定须自带 .* 尾巴）；
    # 不配 command 即该工具全 command 放行。不可解析时按未命中处理并告警，
    # 不 fail-open（US7-10）。
    auto_approve_rules = Value("AUTO_APPROVE_RULES", default="")
    memory_confidence_threshold = Value("MEMORY_CONFIDENCE_THRESHOLD", default=0.6)
    org_memory_collection_name = Value("ORG_MEMORY_COLLECTION_NAME")
    memory_recall_limit = Value("MEMORY_RECALL_LIMIT", default=10)
    memory_dedup_score = Value("MEMORY_DEDUP_SCORE", default=0.95)
    memory_archive_inactive_hours = Value("MEMORY_ARCHIVE_INACTIVE_HOURS", default=24.0)
    todo_escalate_threshold = Value("TODO_ESCALATE_THRESHOLD", default=3)
