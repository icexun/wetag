# migrations

PG schema 的唯一变更途径。启动时由 `app/platform/db/migration_check.py`
校验版本号，不匹配则拒绝启动——独立部署的 DB 不能靠删文件重建。

## 约定

- 文件名 `NNNN_描述.sql`，序号连续递增，只增不改。
- 期望版本号写在 `version.py`，取**已有内容的**最高编号。
- 除注释外没有语句的占位文件会被跳过，且不写 `schema_version`——先记上版本会让
  日后补写的 DDL 永远不再执行。补写内容时同步抬高 `EXPECTED_SCHEMA_VERSION`。
- 所有时间列用 `timestamptz`，时区统一 UTC。
- `0007_roles_and_grants.sql` 单独成文件，需可单独重跑与审阅。

## 执行顺序

1. `deploy/db/bootstrap.sql`（建库与角色，仅首次）
2. `scripts/init_workspace.py --workspace=demo` 按序应用 `0001` → `0007`；
   `0001` 与 `0007` 需要管理员角色
3. `scripts/verify_isolation.py`（隔离验收）

启动时 `app/platform/db/migration_check.py` 只校验版本，不自动补齐。
