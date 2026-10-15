-- chat_group / topic / topic_correction（归 Space，裁决 #8）
-- @ws_schema@ 由 app/platform/db/schema.py 的 render_migration 替换为 ws_{workspace}。

-- 群登记。裁决 #8：Channel 的 channel_conversation、Runtime 的 chat_state、
-- Capability 的 chat_group 三张同构表并为这一张，归 Space，其余模块只读。
CREATE TABLE IF NOT EXISTS @ws_schema@.chat_group (
    chat_id             TEXT        PRIMARY KEY,
    workspace_id        TEXT        NOT NULL,
    bot_id              TEXT        NOT NULL,
    chat_type           TEXT        NOT NULL,
    display_name        TEXT,
    first_actor_user_id TEXT        NOT NULL,
    activated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_inbound_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 会话事项。§4.1：概念名「会话事项」，标识 topic_id，不使用 session 命名。
CREATE TABLE IF NOT EXISTS @ws_schema@.topic (
    topic_id       TEXT        PRIMARY KEY,
    chat_id        TEXT        NOT NULL REFERENCES @ws_schema@.chat_group (chat_id),
    title          TEXT        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'ACTIVE',
    -- L2 路由的命中键：TAPD 编号、飞书文档 token 等外部标识。
    ref_keys       TEXT[]      NOT NULL DEFAULT '{}',
    -- L1 路由的命中键：本事项下企微侧已知的消息 id（含机器人回复所属回调）。
    known_msg_ids  TEXT[]      NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at    TIMESTAMPTZ
);

-- 候选集查询：同群未归档事项按最近活跃排序。
CREATE INDEX IF NOT EXISTS idx_topic_chat_active
    ON @ws_schema@.topic (chat_id, last_active_at DESC)
    WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_topic_ref_keys
    ON @ws_schema@.topic USING GIN (ref_keys);
CREATE INDEX IF NOT EXISTS idx_topic_known_msg_ids
    ON @ws_schema@.topic USING GIN (known_msg_ids);

-- 事项划分纠正样本。仅供离线评测，不喂 Prompt（§8.2）。
CREATE TABLE IF NOT EXISTS @ws_schema@.topic_correction (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id        TEXT        NOT NULL,
    from_topic_id  TEXT,
    to_topic_id    TEXT,
    actor_user_id  TEXT        NOT NULL,
    raw_text       TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
