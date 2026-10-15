-- 首次建库，在 migrations 之前执行一次。
-- 以超级用户运行：psql -d postgres -f deploy/db/bootstrap.sql
-- 业务线角色（wetag_ws_<id>）由 scripts/init_workspace.py 经 0007 创建。
-- 幂等，可重复执行。

\set QUIET on

-- CREATE DATABASE 不能在事务/DO 块中，用 \gexec 兜底
SELECT 'CREATE DATABASE wetag'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'wetag')
\gexec
