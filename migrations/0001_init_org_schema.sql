-- org schema：迁移台账 + chat_ownership（chat_id UNIQUE）+ org_config
-- 各业务线角色对本 schema 只有 SELECT 加 chat_ownership 的 INSERT。
--
-- 只在 org 上跑一次（init_workspace.py --apply-org）。与 workspace 无关，
-- 故本文件不含 @ws_schema@ / @ws_role@ 占位符。

CREATE SCHEMA IF NOT EXISTS org;

-- 版本台账。落 org 而非各业务线 schema：业务线角色对 org 有 SELECT，
-- migration_check.py 用业务线连接就能读到自己该跑到哪一版。
CREATE TABLE IF NOT EXISTS org.schema_migration (
    filename     text        NOT NULL,
    version      integer     NOT NULL,
    scope        text        NOT NULL CHECK (scope IN ('org', 'workspace')),
    -- scope='org' 的行 workspace_id 为空串；同一份 workspace 迁移在每条业务线各记一行
    workspace_id text        NOT NULL DEFAULT '',
    checksum     text        NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (filename, workspace_id)
);

-- §4.2 等式的落库形态：一个群只能归一条业务线。chat_id 全局唯一是硬约束——
-- 两条业务线同时认领同一个群，第二条会被 UNIQUE 直接挡住。
CREATE TABLE IF NOT EXISTS org.chat_ownership (
    chat_id             text        PRIMARY KEY,
    workspace_id        text        NOT NULL,
    bot_id              text        NOT NULL,
    chat_type           text,
    first_actor_user_id text,
    claimed_at          timestamptz NOT NULL DEFAULT now(),
    claimed_by          text        NOT NULL DEFAULT current_user
);

CREATE INDEX IF NOT EXISTS chat_ownership_ws_idx
    ON org.chat_ownership (workspace_id);

-- 跨业务线共享的规则层与配置。业务线只读。
-- config_value 取 text：规则层内容是提示词片段，不是结构化配置。
CREATE TABLE IF NOT EXISTS org.org_config (
    config_key   text        PRIMARY KEY,
    config_value text        NOT NULL,
    version      integer     NOT NULL DEFAULT 1,
    updated_by   text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
