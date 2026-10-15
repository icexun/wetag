# 贡献指南 / Contributing

欢迎 Issue 与 PR。提交前请阅读 [`AGENTS.md`](AGENTS.md)（开发约定）与 [`docs/design/code-layout.md`](docs/design/code-layout.md)（目录与依赖方向）。

## 开发流程

1. Fork 并基于 `main` 建分支。
2. 本地起 PostgreSQL（`docker compose up -d db`），安装依赖：`pip install -r requirements.txt`。
3. 改动须带测试；外部 CLI 用 `FakeSpawn` 回放输出，不依赖本机安装。
4. 提交前通过：

   ```bash
   PG_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/wetag pytest
   pyflakes app tests
   ```

5. PR 描述写清动机与影响面。

## 约定要点

- 依赖方向：`gateway → space → capability → control`；`runtime` 横切，`analytics` 只读；`contracts` / `platform` 不 import 业务模块。
- 外部系统只经 `contracts/ports.py` 的端口；CLI 命令须在 `platform/cli/registry.py` 登记，删除类命令不登记。
- 新增 Agent 写工具须在 `control/approval/classifier.py` 定风险等级。
- 阈值、开关走 `config/properties.py`，不写成代码常量。
- 不提交任何租户信息（域名、项目 ID、token）。
- 迁移文件只增不改，新增 `migrations/000N_*.sql`。

## English

Issues and PRs are welcome. Please read `AGENTS.md` and `docs/design/code-layout.md`, add tests for every change (replay CLI output with `FakeSpawn`), and make sure `pytest` and `pyflakes app tests` pass. Never commit tenant data (domains, project IDs, tokens). Migrations are append-only.
