-- card_task / feedback_record（归 Channel）
-- 不含 event_inbox 与 channel_outbox：两者已按裁决 #6、#7 取消，
-- 排重靠 task.idem_key，出站队列归 Runtime。
-- @ws_schema@ 由 app/platform/db/schema.py 的 render_migration 替换为 ws_{workspace}。

-- 卡片安全绑定。按钮回调的幂等由本表的 CAS 状态迁移保证（裁决 #6）。
CREATE TABLE IF NOT EXISTS @ws_schema@.card_task (
    task_id         TEXT        PRIMARY KEY,
    workspace_id    TEXT        NOT NULL,
    bot_id          TEXT        NOT NULL,
    chat_id         TEXT        NOT NULL,
    topic_id        TEXT        NOT NULL,
    allowed_actions TEXT[]      NOT NULL DEFAULT '{}',
    -- 空数组表示群内任意成员可点；审批类卡片在此限定到具体审批人。
    allowed_actors  TEXT[]      NOT NULL DEFAULT '{}',
    status          TEXT        NOT NULL DEFAULT 'ACTIVE',
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    acted_by        TEXT,
    acted_key       TEXT,
    acted_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_card_task_chat
    ON @ws_schema@.card_task (chat_id, created_at DESC);

-- 准确性反馈（PRD A-5），Loop 数据面的来源之一。
-- 同一用户对同一条回复的重复反馈按唯一约束吞掉，不做应用层先查后写。
CREATE TABLE IF NOT EXISTS @ws_schema@.feedback_record (
    id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    response_id   TEXT        NOT NULL,
    chat_id       TEXT        NOT NULL,
    topic_id      TEXT,
    actor_user_id TEXT        NOT NULL,
    result        TEXT        NOT NULL,
    reason        TEXT[]      NOT NULL DEFAULT '{}',
    comment       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_feedback UNIQUE (response_id, actor_user_id, result)
);

CREATE INDEX IF NOT EXISTS idx_feedback_topic
    ON @ws_schema@.feedback_record (topic_id);
