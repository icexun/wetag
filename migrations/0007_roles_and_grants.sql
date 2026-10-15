-- 每业务线角色 + schema 授权 + org 的 SELECT/INSERT 限权（D-4「必须新增的一层」）
-- 验收：用 ws_a 角色访问 ws_b 的表必须报权限错误（§10.2 第 0 项）。
-- 本文件需可单独重跑与审阅，不要并入建表脚本。
--
-- 角色口令由 init_workspace.py 通过 SET LOCAL wetag.ws_password 传入，
-- **不写在文件里**（凭据不入代码库）。

DO $$
DECLARE
    v_schema text := current_setting('wetag.ws_schema');
    v_role   text := current_setting('wetag.ws_role');
    v_pw     text := current_setting('wetag.ws_password', true);
BEGIN
    -- 1) 角色。已存在则只在显式传入口令时改口令。
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', v_role, v_pw);
    ELSIF v_pw IS NOT NULL AND v_pw <> '' THEN
        EXECUTE format('ALTER ROLE %I PASSWORD %L', v_role, v_pw);
    END IF;

    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), v_role);

    -- 2) 自己的 schema：USAGE + CREATE。
    --    CREATE 是必须给的——APScheduler 要在本业务线 schema 里自建
    --    apscheduler_jobs（§9.4-1，否则多进程互相抢任务）。
    --    风险显式接受：verify_isolation.py 用 `CREATE TABLE ws_b.evil` 证明
    --    它**只能**在自己的 schema 建表。
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', v_schema, v_role);
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
        v_schema, v_role);
    EXECUTE format(
        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', v_schema, v_role);
    -- 后续迁移新建的表自动带上同样权限
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', v_schema, v_role);
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
        'GRANT USAGE, SELECT ON SEQUENCES TO %I', v_schema, v_role);

    -- 3) org schema：只读 + chat_ownership 仅 INSERT。
    --    刻意不给 UPDATE/DELETE——认领过的群不能被另一条业务线改走或删掉，
    --    §4.2 的「一个群只归一条业务线」才是硬的。
    EXECUTE format('GRANT USAGE ON SCHEMA org TO %I', v_role);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA org TO %I', v_role);
    EXECUTE format('GRANT INSERT ON org.chat_ownership TO %I', v_role);
    EXECUTE format('REVOKE UPDATE, DELETE ON org.chat_ownership FROM %I', v_role);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON org.org_config FROM %I', v_role);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON org.schema_migration FROM %I', v_role);

    -- 4) public：一寸不给。search_path 里也没有它（platform/db/schema.py）。
    --    bootstrap.sql（超级用户）已 REVOKE ALL ON SCHEMA public FROM PUBLIC；
    --    迁移身份未必是 public 的 owner，所以这里只做「尽力收紧 + 硬断言」。
    BEGIN
        EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', v_role);
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE '跳过 REVOKE ON SCHEMA public（当前身份非 owner），改为断言';
    END;

    IF has_schema_privilege(v_role, 'public', 'CREATE') THEN
        RAISE EXCEPTION
            '角色 % 仍能在 public 建对象——先以超级用户执行 deploy/db/bootstrap.sql', v_role;
    END IF;

    -- 5) 连接级默认：即使有人漏设 search_path，也落在自己 schema 上。
    EXECUTE format('ALTER ROLE %I IN DATABASE %I SET search_path TO %I, org',
                   v_role, current_database(), v_schema);
    EXECUTE format('ALTER ROLE %I SET timezone TO ''UTC''', v_role);
    EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout TO ''15s''',
                   v_role);

    -- 6) 硬断言。GRANT 失败在 PG 里只是 WARNING——不断言就会出现
    --    「迁移全绿但角色连不上库」这种最难查的故障。
    IF NOT has_database_privilege(v_role, current_database(), 'CONNECT') THEN
        RAISE EXCEPTION
            '角色 % 没有 CONNECT 权限（迁移身份缺 WITH GRANT OPTION？）'
            '——先以超级用户执行 deploy/db/bootstrap.sql', v_role;
    END IF;
    IF NOT has_schema_privilege(v_role, v_schema, 'USAGE') THEN
        RAISE EXCEPTION '角色 % 对自己的 schema % 没有 USAGE', v_role, v_schema;
    END IF;
END
$$;
