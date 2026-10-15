---
name: wecom-office
description: 企业微信通讯录、消息、日程、会议、文档、在线表格、智能表格、微盘、邮件。调用 wecom_* 协作工具（非待办）前必读。
---

# 企业微信协作

## 工具

- 人与会话：`wecom_user_search`（取 userid）、`wecom_chat_list`（近 7 天会话，取 chat_id）、`wecom_message_send`
- 日程：`wecom_schedule_list` / `wecom_schedule_get` / `wecom_schedule_create`
- 会议：`wecom_meeting_list` / `wecom_meeting_get`（含纪要）/ `wecom_meeting_create`
- 文档：`wecom_doc_search` / `wecom_doc_read`
- 在线表格：`wecom_sheet_info` → `wecom_sheet_read` / `wecom_sheet_append_row`
- 智能表格：`wecom_smartsheet_info` → `wecom_smartsheet_records` / `wecom_smartsheet_add` / `wecom_smartsheet_update`
- 微盘：`wecom_disk_search`
- 邮件：`wecom_mail_search` / `wecom_mail_get` / `wecom_mail_send`（无附件）

## 规则

1. 时间格式 `YYYY-MM-DD HH:MM:SS`；人员先 `wecom_user_search` 取 userid，同名时让用户确认。
2. 日程不带会议链接；要线上会议用 `wecom_meeting_create`。
3. 回复当前群直接回答，不用 `wecom_message_send`。
4. 智能表格先 `wecom_smartsheet_info` 看字段，记录值按字段标题填。
5. 返回含 `help_message` 时原样转告用户（通常是授权指引）；`ok=false` 说明失败步骤。
6. 对用户只说名称，不暴露 docid、userid、record_id 等内部 ID。
