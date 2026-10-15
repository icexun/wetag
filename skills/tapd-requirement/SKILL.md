---
name: tapd-requirement
description: TAPD 需求/缺陷/任务的查询与读写规范。调用任何 tapd_* 工具前必读。
---

# tapd-requirement

工具走官方 tapd-cli。`workspace_id` 省略即当前业务线项目，不向用户索要。

## 工具

- 查：`tapd_story_list` / `tapd_bug_list` / `tapd_task_list`（传 `story_id` / `bug_id` / `task_id` 查详情）、`tapd_story_changes`、`tapd_story_related_bugs`、`tapd_comment_list`、`tapd_iteration_list`、`tapd_member_list`
- 字段定义：`tapd_fields(entity="story"|"bug")`
- 写：`tapd_story_create/update`、`tapd_bug_create/update`、`tapd_task_create/update`、`tapd_comment_add`

## 规则

1. 自定义字段只能用 `custom_field_*` 键；先 `tapd_fields` 按中文名取键，不凭印象拼。
2. 状态用中文 `v_status`；人员填昵称（`tapd_member_list` 可查），多人 `;` 分隔。
3. 需求/缺陷/任务的 `description` 是 HTML（`<p>` `<h3>` `<ul><li>` `<br>`，转义 `& < >`）；评论内容用 Markdown。
4. 以工具返回的回读值汇报，不把入参当已写入值；`ok=false` 时说明失败步骤。
5. 汇报带返回里的 `url`。
6. 没有删除能力，用户要删时说明请其在 TAPD 页面操作。
