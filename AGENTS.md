# WeTag 开发约定

面向在本仓库写代码的 Agent / 贡献者。机器人运行时人设在 `config/persona.md`，不在这里。

## 架构

`app/` 依赖方向：`gateway(Channel) → space → capability → control`；`runtime` 横切，`analytics` 只读。
`contracts/` 与 `platform/` 可被任何模块 import，二者不得 import 业务模块。详见 `docs/design/code-layout.md`。

## 约定

- 外部系统只经 `contracts/ports.py` 的端口访问；CLI 命令须在 `platform/cli/registry.py` 登记，删除类命令不登记
- 新增 Agent 写工具须在 `control/approval/classifier.py` 定风险等级
- 阈值、开关一律走配置（`config/properties.py`），不写成代码常量
- 不提交任何租户相关信息（域名、项目 ID、token）；部署配置放 `config/workspaces/<id>.yaml`（gitignored）

## 验证

```bash
pytest            # 需 PG 的用例在未配置 PG_TEST_DSN / 本地 5432 不可用时报错，可先起 PostgreSQL
pyflakes app
```
