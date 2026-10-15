-- requirement / doc_index / requirement_relation / chat_subscription /
-- idempotency_record / rule_change_log（归 Capability）
--
-- **不引入 pgvector**：embedding 用 real[] + 进程内暴力余弦，百条量级足够。

-- ---------------------------------------------------------------------------
-- requirement：TAPD 需求的本地投影。也是需求变更比对的上一版基线。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS @ws_schema@.requirement (
    id              bigserial   PRIMARY KEY,
    workspace_id    text        NOT NULL,
    requirement_id  uuid        NOT NULL DEFAULT gen_random_uuid(),
    tapd_id         text        NOT NULL,
    title           text        NOT NULL,
    status          text        NOT NULL,
    -- 动态归类自 ProjectConfig 的状态，**不硬编码**
    status_category text        NOT NULL
                    CHECK (status_category IN ('ACTIVE','SUSPENDED','TERMINAL')),
    category        text,
    owners          text[]      NOT NULL DEFAULT '{}',
    code_hints      text[]      NOT NULL DEFAULT '{}',
    -- 需求关联的飞书文档；doc_poll 据此把它纳入监听
    doc_id          text,
    raw             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    tapd_updated_at timestamptz,
    synced_at       timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT requirement_tapd_uniq UNIQUE (workspace_id, tapd_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS requirement_rid_uniq
    ON @ws_schema@.requirement (workspace_id, requirement_id);
CREATE INDEX IF NOT EXISTS requirement_active_idx
    ON @ws_schema@.requirement (workspace_id, status_category, tapd_updated_at DESC);

-- ---------------------------------------------------------------------------
-- doc_index：文档快照。只存章节摘录与哈希（不落全文），用于变更比对与检索。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS @ws_schema@.doc_index (
    id              bigserial   PRIMARY KEY,
    workspace_id    text        NOT NULL,
    document_id     text        NOT NULL,
    title           text,
    -- [{anchor, heading, excerpt, hash}]：hash 是整章正文的摘要，excerpt ≤ 200 字
    sections        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    embedding       real[],
    doc_revision    bigint      NOT NULL DEFAULT 0,
    content_hash    text,
    doc_modified_at timestamptz,
    -- 拉取失败即标 stale；刷新成功是唯一能摘掉它的动作
    stale           boolean     NOT NULL DEFAULT false,
    stale_reason    text,
    refreshed_at    timestamptz NOT NULL DEFAULT now(),
    archived_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT doc_index_uniq UNIQUE (workspace_id, document_id)
);

CREATE INDEX IF NOT EXISTS doc_index_live_idx
    ON @ws_schema@.doc_index (workspace_id, doc_modified_at DESC)
    WHERE archived_at IS NULL;

-- ---------------------------------------------------------------------------
-- requirement_relation：需求 ↔ 文档 ↔ 群/事项 的确定性关联（不做标题匹配）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS @ws_schema@.requirement_relation (
    id             bigserial   PRIMARY KEY,
    workspace_id   text        NOT NULL,
    requirement_id uuid        NOT NULL,
    relation_kind  text        NOT NULL
                   CHECK (relation_kind IN ('doc_ref','chat','topic','todo','repo')),
    target_ref     text        NOT NULL,
    source         text        NOT NULL DEFAULT 'tapd_field',
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT requirement_relation_uniq
        UNIQUE (workspace_id, requirement_id, relation_kind, target_ref)
);

-- ---------------------------------------------------------------------------
-- chat_subscription：群订阅的变更源。target='*' 表示该类全部对象。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS @ws_schema@.chat_subscription (
    id           bigserial   PRIMARY KEY,
    workspace_id text        NOT NULL,
    chat_id      text        NOT NULL,
    source       text        NOT NULL CHECK (source IN ('requirement','document')),
    target       text        NOT NULL,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chat_subscription_uniq UNIQUE (workspace_id, chat_id, source, target)
);

CREATE INDEX IF NOT EXISTS chat_subscription_target_idx
    ON @ws_schema@.chat_subscription (workspace_id, source, target);

-- ---------------------------------------------------------------------------
-- idempotency_record：外部写操作的幂等兜底（CLI 不支持幂等时由本表兜底）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS @ws_schema@.idempotency_record (
    key          text        PRIMARY KEY,
    workspace_id text        NOT NULL,
    operation    text        NOT NULL,
    status       text        NOT NULL DEFAULT 'in_flight'
                 CHECK (status IN ('in_flight','succeeded','failed')),
    attempt      integer     NOT NULL DEFAULT 1,
    result       jsonb,
    error_code   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    -- claim 后进程崩溃会留下 in_flight；到期后允许重新 claim
    expires_at   timestamptz NOT NULL DEFAULT now() + interval '7 days'
);

CREATE INDEX IF NOT EXISTS idempotency_record_ws_idx
    ON @ws_schema@.idempotency_record (workspace_id, operation, status);

-- ---------------------------------------------------------------------------
-- rule_change_log：Loop 自动变更存档，审计与回滚的依据
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS @ws_schema@.rule_change_log (
    id               bigserial   PRIMARY KEY,
    workspace_id     text        NOT NULL,
    kind             text        NOT NULL CHECK (kind IN ('prompt')),
    rule_version_old text,
    rule_version_new text,
    detail           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    sample_size      int,
    -- 自动变更固定为 loop_digest；人工改动留痕时写操作人 userid
    actor            text        NOT NULL DEFAULT 'loop_digest',
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rule_change_log_time_idx
    ON @ws_schema@.rule_change_log (workspace_id, created_at DESC);
