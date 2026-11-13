# Workspace Loop Contract — 用户 / 对话 / 笔记 / 知识库闭环

## Goal

把当前「前端拼调用」的碎链路收束成服务端闭环：用户权限可感知、对话内容可沉淀为笔记、笔记可幂等同步到知识库、知识库索引状态可回显、对话检索能基于当前有效权限读取知识。

本文件先落盘作为实现契约，后续代码、测试和验收都以此为准。

---

## 1. 统一原则

1. **服务端是唯一写入协调者**：笔记快照写入知识库不再由前端编排 `upload → rename → content update → KB add/update → note meta`。
2. **canonical fingerprint 由后端统一计算**，前端只展示状态，不再自行决定 `synced`。
3. **成功必须包含完整状态**：文件存在、已关联 KB、索引状态不是 failed、note.meta 映射已落库。
4. **失败必须结构化**：包含 `stage/code/message/retryable`，不得返回 `[object Object]` 或泛化失败。
5. **权限与能力预检前置**：用户角色、notes 功能、知识库写权限、外部 KB、embedding 配置、向量库能力都先检查，再产生任何文件副作用。
6. **对话可沉淀为笔记**：提供从聊天消息创建笔记并可一键同步到知识库的入口。
7. **知识回流对话**：同步后的知识库可由现有 chat attachment / RAG 路径读取；对话侧列表失败必须显示错误和重试，不再吞成空列表。

---

## 2. Canonical fingerprint

后端和前端 helper 共用同一口径：

```ts
sha256(JSON.stringify([canonicalTitle, content]))
```

- `canonicalTitle = (title ?? '').trim() ? title : 'Untitled'`
- `content = note.data?.content?.md ?? ''`
- JSON 必须是 UTF-8、无多余空格、非 ASCII 不转义：`ensure_ascii=False`, `separators=(',', ':')`
- 禁止使用本地化 `Untitled`，避免切换语言导致快照反复 changed。

`note.meta.note_knowledge[kb_id]` 结构保持兼容并扩展：

```json
{
  "file_id": "...",
  "fingerprint": "...",
  "updated_at": 1790000000000,
  "index_state": "completed|pending|failed",
  "index_error": null,
  "state": "synced|indexing|failed"
}
```

旧结构 `{file_id, fingerprint, updated_at}` 必须可读，readiness 负责补齐状态。

---

## 3. Backend endpoints

> 所有路由必须注册在 `/notes/{id}` catch-all 之前，避免 `loop` / `from-chat` 被当成 note id。

### 3.1 用户级闭环预检

```http
GET /api/v1/notes/loop/readiness
```

Response:

```json
{
  "ready": true,
  "reason_code": null,
  "user": {
    "id": "user-id",
    "role": "user|admin",
    "notes_enabled": true,
    "workspace_knowledge": true,
    "features_notes": true
  },
  "embedding": {
    "ready": true,
    "engine": "openai",
    "model": "BAAI/bge-m3",
    "bypass": false,
    "reason_code": null
  },
  "knowledge_bases": [
    { "id": "kb-id", "name": "C语言", "write_access": true, "external": false }
  ],
  "features": {
    "notes": true,
    "knowledge": true,
    "chat_import": true
  }
}
```

规则：

- 丢弃分页限制，返回当前用户可见/可写的全部 KB，供 modal 和 chat picker 使用。
- `external = (kb.meta or {}).source === 'external'`，外部 KB 不得出现在 writable 列表里。
- `embedding.ready` 判断：
  - `rag.bypass_embedding_and_retrieval=true` 时 ready，但标记 `bypass=true`；
  - 外部 engine 必须为 `openai|ollama|azure_openai`，且必须有 model；
  - 本地 engine 为空时，如果 `USE_SLIM=true` 则 `embedding_not_configured`；
  - 不要求把 key 明文返回给前端。

### 3.2 单篇笔记知识快照状态

```http
GET /api/v1/notes/{note_id}/knowledge/readiness
```

Response:

```json
{
  "ready": true,
  "reason_code": null,
  "note": { "id": "...", "title": "...", "write_access": true },
  "current_fingerprint": "...",
  "embedding": { "...": "..." },
  "knowledge_bases": [ { "...": "..." } ],
  "snapshots": [
    {
      "knowledge_id": "...",
      "name": "C语言",
      "file_id": "...",
      "fingerprint": "...",
      "updated_at": 1790000000000,
      "state": "synced|changed|indexing|failed|unavailable",
      "index_state": "completed|pending|failed|unknown",
      "index_error": null,
      "can_write": true,
      "exists": true,
      "external": false
    }
  ]
}
```

`state=synced` 的硬条件：

1. snapshot 存在且 `file_id` 存在；
2. KB 仍存在且当前用户可写；
3. `KnowledgeFile(kb_id, file_id)` 关联存在；
4. file 未被回收；
5. fingerprint 等于 `current_fingerprint`；
6. `data.status` / `meta.index_state[kb]` 不处于 `failed`；
7. 外部 KB 一律不可 synced。

否则按具体原因返回 `changed / indexing / failed / unavailable`。

### 3.3 幂等同步笔记到知识库

```http
POST /api/v1/notes/{note_id}/knowledge/{knowledge_id}/sync
Content-Type: application/json

{ "force": false }
```

Response:

```json
{
  "ok": true,
  "action": "noop|created|updated|repaired",
  "state": "synced|indexing|failed",
  "note_id": "...",
  "knowledge_id": "...",
  "file_id": "...",
  "fingerprint": "...",
  "updated_at": 1790000000000,
  "index_state": "completed|pending|failed",
  "index_error": null,
  "cleanup_status": "not_needed|orphan_cleaned|none",
  "message": "..."
}
```

服务端流程：

1. `check_notes_access`；
2. 读取 note，校验 note write access；
3. 校验 KB 存在、非 external、write access；
4. 校验 embedding/readiness；
5. 计算 canonical title/content/fingerprint；
6. 如果已有 mapping：
   - file 存在且已关联 KB → 复用 `file_id`；
   - file 不存在 / 关联丢失 → 尝试按 fingerprint/note id 修复，否则新建；
7. 如果新建：
   - 写存储与 `File` 记录；
   - 先建立 `KnowledgeFile` 关联，再做索引；
   - 任何失败清理孤儿文件或标记可重试状态；
8. 更新文件：
   - filename 与 content 同步；
   - 使用统一索引服务，避免双击双重 inline 索引；
9. 成功后写 `note.meta.note_knowledge[kb_id]`，合并 per-KB，不覆盖其他快照；
10. 返回 status；失败用结构化 detail：

```json
{
  "code": "embedding_not_configured|access_prohibited|external_knowledge_read_only|note_not_found|knowledge_not_found|file_link_failed|index_failed|meta_write_failed",
  "stage": "preflight|upload|attach|index|metadata|cleanup",
  "message": "human readable",
  "retryable": true,
  "file_id": "optional"
}
```

### 3.4 移除快照映射

```http
DELETE /api/v1/notes/{note_id}/knowledge/{knowledge_id}/snapshot?remove_file=false
```

- 默认只删除本地 `note_knowledge[kb_id]` 映射，不动 KB 文件；
- `remove_file=true` 且当前用户有 KB 写权限时，同时解除 KB 文件关联并按资源服务规则回收/清理；
- 返回 `{ok, removed_mapping, removed_file, file_id}`。

### 3.5 对话消息沉淀为笔记

```http
POST /api/v1/notes/from-chat
Content-Type: application/json

{
  "chat_id": "...",
  "message_id": "...",
  "title": "optional",
  "knowledge_id": "optional"
}
```

- 校验当前用户对 chat 的 read 权限；
- 从 chat 消息中抽取可见文本，过滤空消息；
- 创建 note：`data.content.md` 保存 Markdown；
- 如果提供 `knowledge_id`，复用 3.3 的 sync 逻辑，一次请求完成「对话 → 笔记 → 知识库」；
- 返回 `{note, snapshot}`。

---

## 4. Indexing state contract

`backend/open_webui/utils/knowledge_indexing.py`：

- `track_indexing()` 必须同时接受 `asyncio.Task` 或 coroutine；传 coroutine 时内部 `asyncio.create_task`，不得直接 `.add_done_callback()`。
- `_write_index_state()` 同时写：
  - `file.data.status = pending|completed|failed`
  - `file.data.error = reason or null`
  - `file.meta.index_state[kb_id] = {state, error, updated_at, collection_name}`
- `/files/{id}/process/status`、KB file list、note readiness 必须读同一套状态口径。
- app shutdown 必须 `await drain_indexing()`，避免重启丢任务。
- agent `add_text_to_knowledge()` 在 non-inline 模式不得因 coroutine/Task 类型失败；必须先建立 KB 关联再索引。

---

## 5. Frontend contract

### 5.1 NoteKnowledgeModal

- 打开时调用 `GET /notes/{id}/knowledge/readiness`；
- 保存/更新/强制更新调用 `POST /notes/{id}/knowledge/{kb}/sync`；
- 移除调用 `DELETE /notes/{id}/knowledge/{kb}/snapshot`；
- 不再调用 `uploadFile / renameFileById / updateFileDataContentById / addFileToKnowledgeById / updateFileFromKnowledgeById`；
- 使用 `describeApiError`；
- 使用 `$user` / `$config` 做 account-aware reset；
- writableBases 过滤服务端返回的 `write_access === true && !external`；
- 显示状态：`未入库 / 已同步 / 内容已修改 / 索引中 / 索引失败 / 无权限 / 知识库不可用`；
- 空态提供 Retry / Create knowledge base；
- 移除映射对所有 saved mappings 可用。

### 5.2 Notes list / editor

- Notes list/grid 的 Save-to-Knowledge 必须按 `write_access` 或服务端预检结果 gate；
- 所有 `snapshotState` 调用改用 canonical title helper；
- NoteEditor 的 chips 基于 readiness/snapshot state 显示，不自行用本地 hash 判成功。

### 5.3 Chat pickers

- `InputMenu/Knowledge.svelte`, `InputMenu/Notes.svelte`, `Commands/Knowledge.svelte` 不得吞 API 错误为 `null/[]`；
- 显示 inline error + Retry；
- `$user.id` 变化时清空 stale items 并 reload；
- 读取 `GET /notes/loop/readiness` 作为 KB/notes 功能预检的补充，失败时明确提示。

### 5.4 i18n

新增 zh-CN / en-US 键：

- `No writable knowledge bases`
- `Create a knowledge base`
- `Retry`
- `Remove snapshot`
- `Remove from knowledge base`
- `Indexing…`
- `Indexing failed`
- `No permission to save to knowledge`
- `Saving note before snapshot`
- `Snapshot linked; indexing in progress`
- `Conversation saved to note`
- `Conversation saved to knowledge base`
- `Knowledge loop not ready`

---

## 6. Acceptance evidence

实现完成后必须提供：

1. 后端 runtime tests / ruff / check_source 通过；
2. frontend vitest / check 通过；
3. 关键文件 diff 列表；
4. mint 线上数据状态：
   - `note.meta` fingerprint 与 canonical 一致；
   - C语言 KB 权限含 read/write；
   - embedding config ready；
   - file 已关联、状态 completed、向量可按 file_id 查询；
5. 对话侧：知识库/笔记 picker 失败有 retry，不再 silent empty。
