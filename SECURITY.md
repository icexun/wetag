# 安全策略 / Security Policy

## 报告漏洞

请**不要**通过公开 Issue 报告安全问题。使用 GitHub 的 [Private vulnerability reporting](../../security/advisories/new) 提交，附上复现步骤与影响范围。我们会在 7 天内确认收到。

Please do **not** open public issues for security problems. Use GitHub [private vulnerability reporting](../../security/advisories/new) with reproduction steps and impact. We aim to acknowledge within 7 days.

## 安全模型

- **业务线隔离**：每个业务线独立进程、独立 schema 与 DB 角色；跨业务线访问在 DB 权限层即被拒绝，并按安全事件告警。
- **CLI 白名单**：Agent 只能调用 `platform/cli/registry.py` 登记的命令，参数经 schema 校验；删除类命令、本地文件路径参数不开放。
- **凭据**：只经子进程环境变量注入，不进命令行参数与日志；日志与审计中的输出经脱敏截断。
- **审批闸门**：高危写操作须群内审批后执行。
- **外部内容**：读取到的文档、邮件、消息均视为不可信数据，不作为指令执行。

## 部署建议

- `.env`、`config/workspaces/*.yaml`、`mcp.json` 不要入库。
- 生产进程不要配置 `PG_ADMIN_DSN`。
- CLI 授权使用最小必要权限。
