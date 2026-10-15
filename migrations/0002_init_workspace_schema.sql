-- ws_{workspace} 模板：task / outbox / audit_log / runtime_state
-- task.idem_key 建 UNIQUE，去重用 ON CONFLICT DO NOTHING（§9.4-4）。
-- 另含 APScheduler 自建的 apscheduler_jobs，必须落各自 schema（§9.4-1）。
-- 以 @ws_schema@ 为参数由 scripts/init_workspace.py 渲染后执行（每条业务线跑一次）。

CREATE SCHEMA IF NOT EXISTS @ws_schema@;

CREATE TABLE IF NOT EXISTS @ws_schema@.task (
    id              uuid PRIMARY KEY,
    workspace_id    text NOT NULL,
    job_name        text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('deterministic','agentic')),
    trigger_source  text NOT NULL CHECK (trigger_source IN ('mention','schedule','card_action','derived')),
    parent_id       uuid,
    idem_key        text,
    chat_id         text,
    topic_id        text,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority        smallint NOT NULL DEFAULT 5,
    status          text NOT NULL CHECK (status IN ('PENDING','RUNNING','WAIT_CONFIRM','SUCCEEDED','FAILED','DEAD')),
    attempt         smallint NOT NULL DEFAULT 0,
    max_attempt     smallint NOT NULL DEFAULT 3,
    progress        text,
    error           text,
    rule_version    text,
    lease_expire_at timestamptz,
    scheduled_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    token_cost      integer,
    trace_id        text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_task_idem   ON @ws_schema@.task (idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX        IF NOT EXISTS ix_task_poll   ON @ws_schema@.task (status, priority, scheduled_at);
CREATE INDEX        IF NOT EXISTS ix_task_lease  ON @ws_schema@.task (lease_expire_at) WHERE status = 'RUNNING';
CREATE INDEX        IF NOT EXISTS ix_task_parent ON @ws_schema@.task (parent_id);

CREATE TABLE IF NOT EXISTS @ws_schema@.outbox (
    id            uuid PRIMARY KEY,
    workspace_id  text NOT NULL,
    chat_id       text NOT NULL,
    topic_id      text,
    merge_key     text,
    msg_type      text NOT NULL CHECK (msg_type IN ('markdown','template_card')),
    priority      text NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent','normal')),
    payload       jsonb NOT NULL,
    update_of     text,
    status        text NOT NULL CHECK (status IN ('PENDING','MERGED','SENT','SUSPENDED','DROPPED','FAILED')),
    not_before    timestamptz NOT NULL,
    suspended_at  timestamptz,
    attempt       smallint NOT NULL DEFAULT 0,
    drop_reason   text,
    sent_msg_id   text,
    task_id       uuid,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_outbox_due       ON @ws_schema@.outbox (status, not_before);
CREATE INDEX IF NOT EXISTS ix_outbox_merge     ON @ws_schema@.outbox (chat_id, merge_key) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS ix_outbox_suspended ON @ws_schema@.outbox (chat_id) WHERE status = 'SUSPENDED';

CREATE TABLE IF NOT EXISTS @ws_schema@.audit_log (
    id             bigserial PRIMARY KEY,
    workspace_id   text NOT NULL,
    trace_id       text,
    task_id        uuid,
    chat_id        text,
    topic_id       text,
    actor          text,
    action         text NOT NULL CHECK (action IN ('tool_call','memory_write','external_req','approval')),
    target         text,
    params_digest  text,
    result_digest  text,
    outcome        text NOT NULL,
    rule_version   text,
    approval_id    uuid,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_task    ON @ws_schema@.audit_log (task_id);
CREATE INDEX IF NOT EXISTS ix_audit_trace   ON @ws_schema@.audit_log (trace_id);
CREATE INDEX IF NOT EXISTS ix_audit_created ON @ws_schema@.audit_log (created_at);

CREATE TABLE IF NOT EXISTS @ws_schema@.runtime_state (
    workspace_id text NOT NULL,
    state_key    text NOT NULL,
    state_value  jsonb NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, state_key)
);
