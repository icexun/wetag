---
name: feishu-doc
description: 读取、创建、修改、追加和搜索飞书云文档。用户提供飞书文档、Wiki、电子表格链接时使用。
---

# 飞书云文档

使用已注册的飞书工具（lark-cli 实现）。

## 工具

- `feishu_doc_fetch(doc_url)`：读文档/Wiki/表格，返回带 block id 的 Markdown
- `feishu_doc_create(title, content_markdown, parent_token?)`：建文档，返回链接
- `feishu_doc_append(doc_url, content_markdown)`：末尾追加 Markdown
- `feishu_doc_update(doc_url, command, content_markdown, pattern?, block_id?)`：原位修改
- `feishu_doc_search(query)`：搜文档，返回标题与链接

## 规则

1. `doc_url` 用完整链接，不要截断 token。
2. 建文档：`feishu_doc_create` 一次完成（标题+Markdown 正文）；`parent_token` 是文件夹或 wiki 节点 token，可选。
3. 改已有内容优先 `feishu_doc_update` 原位修改，不要只在末尾追加变更说明：
   - 改句子/表格单元格：`command="str_replace"`，`pattern` 填旧文本（必须在文中唯一，多取上下文），`content_markdown` 填新文本。
   - 整块替换：`command="block_replace"`，`block_id` 从 fetch 结果取。
   - 在某块后插入：`command="block_insert_after"`。
4. 工具返回 `{"ok": false, ...}` 时说明失败步骤，不要声称成功。
5. 创建/追加/修改成功后返回文档标题和链接。
