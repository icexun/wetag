---
name: feishu-office
description: 飞书表格、多维表格、任务、日历、审批查询、知识库、通讯录。调用 feishu_sheet_* / feishu_base_* / feishu_task_* / feishu_calendar_* / feishu_approval_* / feishu_wiki_* / feishu_user_search 前必读。
---

# 飞书协作

以授权用户身份调用 lark-cli。

## 工具

- 表格：`feishu_sheet_read` / `feishu_sheet_search` / `feishu_sheet_write_csv`（`sheet_name`、`sheet_id` 给一个）
- 多维表格：`feishu_base_tables` → `feishu_base_fields` → `feishu_base_records` / `feishu_base_upsert`
- 人：`feishu_user_search` 取 open_id
- 任务：`feishu_task_create` / `feishu_task_my_open` / `feishu_task_search` / `feishu_task_complete`
- 日历：`feishu_calendar_agenda` / `feishu_calendar_freebusy` / `feishu_calendar_search` / `feishu_calendar_create`
- 审批（只读）：`feishu_approval_tasks` / `feishu_approval_instance`
- 知识库：`feishu_wiki_nodes` / `feishu_wiki_node_create`

## 规则

1. 写多维表格前先 `feishu_base_fields`，字段名、选项按返回填。
2. 负责人、参与人先 `feishu_user_search` 取 open_id，不猜。
3. 时间用 ISO 8601（带 +08:00）或 `YYYY-MM-DD`。
4. 审批不代为同意/拒绝，引导用户到飞书处理。
5. `ok=false` 且缺 scope：如实告知缺哪个权限，请用户重新授权；不重试。
6. 汇报以返回结果为准，带链接。
