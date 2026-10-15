-- ws_{workspace} 模板：Control 台账与审批表（归 Control，裁决 #19）
-- pending_approval 是异步审批的唯一状态载体（US7-8：状态在库不在内存）；
-- pending_memory / memory_conflict / todo_ref 为结构化台账；
-- agent_working_memory 持久化 Agent 对话上下文（重启不丢）。
-- 以 @ws_schema@ 为参数由 scripts/init_workspace.py 渲染后执行（每条业务线跑一次）。

CREATE TABLE IF NOT EXISTS @ws_schema@.pending_approval (
    id                uuid PRIMARY KEY,
    workspace_id      text NOT NULL,
    task_id           uuid NOT NULL,
    chat_id           text NOT NULL,
    topic_id          text,
    tool_name         text NOT NULL,
    tool_call_digest  text NOT NULL,
    tool_call_payload jsonb NOT NULL,
    risk_level        text NOT NULL DEFAULT 'high_write',
    status            text NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CONSUMED')),
    decided_by        text,
    decided_at        timestamptz,
    expires_at        timestamptz NOT NULL,
    card_msg_id       text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_approval_call
    ON @ws_schema@.pending_approval (task_id, tool_call_digest);
CREATE INDEX IF NOT EXISTS ix_pending_approval_expiry
    ON @ws_schema@.pending_approval (expires_at) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS @ws_schema@.pending_memory (
    id              uuid PRIMARY KEY,
    workspace_id    text NOT NULL,
    target_scope    text NOT NULL CHECK (target_scope IN ('biz','org')),
    content         text NOT NULL,
    confidence      numeric(3,2) NOT NULL,
    source_chat_id  text,
    source_topic_id text,
    status          text NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    decided_by      text,
    decided_at      timestamptz,
    memory_id       text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pending_memory_status
    ON @ws_schema@.pending_memory (status, created_at);

CREATE TABLE IF NOT EXISTS @ws_schema@.memory_conflict (
    id           uuid PRIMARY KEY,
    workspace_id text NOT NULL,
    memory_id_a  text NOT NULL,
    memory_id_b  text NOT NULL,
    layer        text NOT NULL CHECK (layer IN ('group','biz','org')),
    chat_id      text,
    status       text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
    resolution   text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_conflict_pair
    ON @ws_schema@.memory_conflict (workspace_id, memory_id_a, memory_id_b);

CREATE TABLE IF NOT EXISTS @ws_schema@.todo_ref (
    workspace_id   text NOT NULL,
    wecom_todo_id  text NOT NULL,
    chat_id        text NOT NULL,
    topic_id       text,
    source         text NOT NULL,
    remind_count   integer NOT NULL DEFAULT 0,
    last_remind_at timestamptz,
    status         text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ARCHIVED','INVALID')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, wecom_todo_id)
);
CREATE INDEX IF NOT EXISTS ix_todo_ref_active
    ON @ws_schema@.todo_ref (chat_id) WHERE status = 'ACTIVE';

-- marks 语义对齐 agentscope InMemoryMemory（一条消息可带多个标记）
CREATE TABLE IF NOT EXISTS @ws_schema@.agent_working_memory (
    id          bigserial   PRIMARY KEY,
    thread_id   text        NOT NULL,
    msg_id      text        NOT NULL,
    marks       text[]      NOT NULL DEFAULT '{}',
    msg         jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_working_memory_uniq UNIQUE (thread_id, msg_id)
);

CREATE INDEX IF NOT EXISTS agent_working_memory_thread_idx
    ON @ws_schema@.agent_working_memory (thread_id, id);

CREATE TABLE IF NOT EXISTS @ws_schema@.agent_working_memory_meta (
    thread_id   text        PRIMARY KEY,
    summary     text        NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
