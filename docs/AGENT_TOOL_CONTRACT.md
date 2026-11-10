# Agent 工具契约 —— 笔记与知识库管理

> 本文件是 `open-webui-backend` 与 `open-webui-frontend` 之间的**权威契约**。
> 后端仓库为权威副本，前端仓库保存同内容副本。**实现前先改这里，再改代码**。
>
> 对应工作项：P-007。适用范围：内置（builtin）agent 工具中与**笔记**、**知识库**相关的部分。

---

## 1. 定位

聊天中的 agent 通过**原生 function calling** 调用后端内置工具。工具由后端 `utils/tools.py:get_builtin_tools()` 按模型配置与用户权限动态注入，调用后在后端进程内执行，**不经过 HTTP 路由**。

本契约只规定**工具层**的行为。笔记与知识库的 HTTP API 契约不受本文件约束，仍以其路由实现为准；但工具内部的权限判定**必须**与 HTTP 路由使用同一套判定函数（见第 9 节）。

### 1.1 硬性原则

| 编号 | 原则 | 含义 |
| --- | --- | --- |
| P1 | **不可越权** | agent 永远不能做发起人自己做不了的事。权限判定复用 HTTP 路由的同一函数，禁止在工具里自建第二套规则。 |
| P2 | **删除需二次确认** | 任何删除操作必须凭一次性令牌执行；无令牌一律拒绝，不允许「模型自己决定删」。 |
| P3 | **删除可恢复** | 删除一律回收站语义，结果文案必须写明可恢复期限。 |
| P4 | **不假报成功** | 状态区分 `accepted` 与 `success`；异步未完成的不得报完成。 |
| P5 | **失败可解释** | 失败必须返回机器可读 `code` 与人可读 `message`，不得吞错或返回空。 |
| P6 | **单一事实来源** | 工具能力、权限、错误分类只在本文件与后端代码定义一次；前端不维护第二份清单。 |

---

## 2. 术语

| 术语 | 含义 |
| --- | --- |
| **内置工具（builtin tool）** | 后端 `backend/open_webui/tools/builtin.py` 中的 Python 函数，通过 `get_builtin_tools()` 注入 |
| **工具类别（category）** | 模型 `info.meta.builtinTools` 下的开关名，如 `notes` / `knowledge` / `knowledge_manage` |
| **发起人（caller）** | 当前请求的 `__user__`，工具的权限主体 |
| **确认令牌（confirmation token）** | 删除前由预览工具签发的一次性凭据 |
| **回收站（recycle bin）** | `utils/resource_service.py` 的软删机制，默认保留 7 天 |
| **资源键（resource key）** | 令牌绑定的规范化资源标识，见 7.2 |

---

## 3. 现状基线（2026-11-10 实测）

在 `origin/main`（后端 `cca7906`）上调用后端自身 `get_builtin_tools()` 枚举所得：

| 场景 | 笔记工具 | 知识库工具 |
| --- | --- | --- |
| 普通聊天 | `search_notes` `view_note` `write_note` `replace_note_content` | 7 个**只读**：`list_knowledge_bases` `search_knowledge_bases` `query_knowledge_bases` `search_knowledge_files` `query_knowledge_files` `grep_knowledge_files` `view_knowledge_file` |
| 模型挂知识库集合 | 同上 4 个 | 6 个（**缺** `list_knowledge_bases` `search_knowledge_bases` `query_knowledge_bases`） |
| 模型挂笔记 | 同上 4 个 | 4 个（`list_knowledge` `search_knowledge_files` `query_knowledge_files` `grep_knowledge_files`） |
| 笔记内嵌聊天 | 同上 4 个（强制开启） | **0 个** |

**基线结论**：知识库对 agent 完全只读，不存在任何写工具；笔记缺少删除工具。

本契约定义的变更即在此基线上补齐，且**不改变**上表已有工具的行为（回归要求）。

---

## 4. 工具清单总表

### 4.1 既有工具（行为不变，仅登记）

| 工具名 | 类别 | 读/写 | 说明 |
| --- | --- | --- | --- |
| `search_notes` | `notes` | 读 | 按关键词/时间检索笔记 |
| `view_note` | `notes` | 读 | 读取单篇笔记全文 |
| `write_note` | `notes` | 写 | 新建笔记（默认私有） |
| `replace_note_content` | `notes` | 写 | 替换/局部改写笔记正文 |
| `list_knowledge_bases` | `knowledge` | 读 | 列出可见知识库 |
| `search_knowledge_bases` | `knowledge` | 读 | 按名称/描述检索知识库 |
| `query_knowledge_bases` | `knowledge` | 读 | 知识库语义检索 |
| `list_knowledge` | `knowledge` | 读 | 列出某知识库内文件 |
| `search_knowledge_files` | `knowledge` | 读 | 跨库文件名检索 |
| `grep_knowledge_files` | `knowledge` | 读 | 精确/正则全文检索 |
| `query_knowledge_files` | `knowledge` | 读 | 文件内容语义检索 |
| `view_knowledge_file` | `knowledge` | 读 | 分页读取知识库文件正文 |
| `view_file` | `files` | 读 | 按 file_id 读取文件 |
| `kb_exec` | `knowledge` | 读 | 类 shell 只读文件系统门面（`ENABLE_KB_EXEC=false` 时不注入） |

### 4.2 本项新增工具

| 工具名 | 类别 | 读/写 | 需令牌 | 阶段 |
| --- | --- | --- | --- | --- |
| `preview_delete` | `notes` / `knowledge_manage` | 读 | — | S1 |
| `delete_note` | `notes` | 写 | ✅ | S1 |
| `create_knowledge_base` | `knowledge_manage` | 写 | — | S2 |
| `update_knowledge_base` | `knowledge_manage` | 写 | — | S2 |
| `add_text_to_knowledge` | `knowledge_manage` | 写 | — | S2 |
| `create_knowledge_directory` | `knowledge_manage` | 写 | — | S2 |
| `remove_file_from_knowledge` | `knowledge_manage` | 写 | ✅ | S2 |
| `delete_knowledge_base` | `knowledge_manage` | 写 | ✅ | S2 |

**新增类别 `knowledge_manage`**（模型 `info.meta.builtinTools.knowledge_manage`）：

- 与 `knowledge`（只读浏览）**分开**，便于管理员单独关闭写能力。
- 默认值 `True`，与既有类别的「未声明即开启」约定保持一致；真正的安全边界是第 9 节的用户权限判定，而非这个开关。
- 管理员在模型设置中置 `false` 后，本类别下所有工具在任何聊天形态下都不注入。

**明确不做**（本项范围外）：批量导入、外链知识源（external）、权限共享（`access/update`）、目录重命名/移动、文件跨库移动。这些涉及他人可见性或破坏性重排，另开工作项。

---

## 5. 返回信封

### 5.1 新增工具统一信封

新增工具**一律**返回如下结构（`JSONCodec.dumps`，`ensure_ascii=False`）：

```json
{
  "status": "success | accepted | confirmation_required | failed",
  "code": "OK | ...见第 6 节...",
  "message": "面向用户的一句话说明（中文），包含可执行的下一步",
  "data": { }
}
```

- `data` 在 `failed` 时可为 `null` 或仅含诊断字段。
- `message` 必须能让用户直接读懂，不得出现 Python 异常文本。
- **既有工具不改造**（避免回归）；新工具不得模仿 `{"error": "..."}` 旧形状。

### 5.2 状态语义

| status | 含义 | 何时使用 |
| --- | --- | --- |
| `success` | 操作已同步完成 | 建库、改名、建目录、删笔记（回收站提交成功） |
| `accepted` | 已受理，后台仍在处理 | 文本入库（需要向量化处理） |
| `confirmation_required` | 需要用户确认，尚未执行任何破坏性动作 | 删除前，或缺/错/过期令牌 |
| `failed` | 未完成，且不会自行完成 | 权限不足、资源不存在、处理失败、保护资源 |

**P4 硬性要求**：`add_text_to_knowledge` 只能返回 `accepted`，不得返回 `success`。

---

## 6. 错误码

| code | 触发条件 | 建议下一步（写进 message） |
| --- | --- | --- |
| `OK` | 成功 | — |
| `PERMISSION_DENIED` | 发起人无对应权限 | 请拥有者授权，或联系管理员 |
| `NOT_FOUND` | 资源不存在或已被回收 | 先检索确认资源仍在 |
| `PROTECTED_RESOURCE` | 资源共享中，服务端返回 409 `protected_resource` | 先解除共享/确认影响再删 |
| `CONFIRMATION_REQUIRED` | 令牌缺失、过期、已使用或与资源不匹配 | 重新调用 `preview_delete` |
| `INVALID_ARGUMENT` | 参数缺失或非法（空名称、非法 id 等） | 修正参数 |
| `FEATURE_DISABLED` | `notes.enable` 等特性开关关闭 | 联系管理员开启 |
| `PROCESSING_FAILED` | 文件处理/向量化失败 | 查看文件状态或重试入库 |
| `INTERNAL` | 未预期异常（已记日志） | 稍后重试，或查看服务端日志 |

**禁止**把 `PERMISSION_DENIED` 与 `NOT_FOUND` 混用：无权访问的资源按 `NOT_FOUND` 返回会导致用户无法区分「不存在」与「无权限」，本契约要求**无权时明确返回 `PERMISSION_DENIED`**（与既有 HTTP 路由的 401/403 语义一致）。

---

## 7. 删除二次确认

### 7.1 流程（强制）

```
用户要求删除
      │
      ▼
preview_delete(resource_type, resource_id)      ← 只读，不产生任何破坏
      │  status = "confirmation_required"
      │  data.confirmation_token = "<opaque>"
      │  data.impact = { ... }
      ▼
模型把 impact 呈现给用户，取得用户明确同意
      │
      ▼
delete_*(..., confirmation_token="<opaque>")
      │  令牌校验通过 → 执行删除（回收站）
      ▼
status = "success"，message 写明「已移入回收站，7 天内可恢复」
```

### 7.2 令牌规范

| 项 | 规定 |
| --- | --- |
| 生成 | `secrets.token_urlsafe(32)`，不得可预测 |
| 绑定 | `(user_id, resource_key)` 二元组 |
| 资源键格式 | `note:<note_id>`；`knowledge:<knowledge_id>`；`knowledge_file:<knowledge_id>:<file_id>` |
| 有效期 | 300 秒 |
| 次数 | **一次性**，校验通过后立即作废（无论后续删除是否成功） |
| 存储 | 进程内带过期的字典，容量上限 1000 条，超限按插入顺序淘汰最旧 |
| 多实例 | 当前为**单进程语义**：多副本部署时令牌不跨进程有效，删除会返回 `CONFIRMATION_REQUIRED`。这是已知边界，见第 12 节 |
| 泄露防护 | 令牌不得写入日志、不得出现在 `message` 之外的结构里 |

### 7.3 校验规则

令牌校验**全部**满足才执行：

1. 令牌存在且未过期；
2. 令牌属主 `user_id` == 当前发起人；
3. 令牌绑定的 `resource_key` == 本次调用的资源键；
4. 令牌未被使用过。

任一不满足 → `status: "confirmation_required"`, `code: "CONFIRMATION_REQUIRED"`，**且绝不做任何删除**。

### 7.4 `preview_delete` 的参数与返回

```python
async def preview_delete(
    resource_type: str,              # 'note' | 'knowledge' | 'knowledge_file'
    resource_id: str,                # note_id / knowledge_id / file_id
    knowledge_id: Optional[str] = None,  # resource_type='knowledge_file' 时必填
    __request__: Request = None,
    __user__: dict = None,
) -> str
```

返回 `data.impact`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `resource_type` | str | 同入参 |
| `resource_id` | str | 同入参 |
| `knowledge_id` | str \| null | 文件删除时的所属库 |
| `title` / `name` | str | 资源可读名称 |
| `file_count` | int \| null | 知识库：受影响文件数 |
| `directory_count` | int \| null | 知识库：受影响目录数 |
| `is_shared` | bool | 是否存在 `access_grant`（公共/他人授权） |
| `recoverable_until` | str \| null | ISO-8601 UTC，回收站到期时间 |
| `warnings` | list[str] | 例如「该知识库被 N 个模型引用」 |

**预览本身也要做权限判定**：无权删除时直接 `failed` + `PERMISSION_DENIED`，不签发令牌。

---

## 8. 回收站语义

- 执行删除时**一律**委托 `utils/resource_service.py:legacy_delete(user, kind, resource_id)`，不得直接 `delete_*_by_id` 硬删。
- `kind` 取值：`'note'` / `'knowledge'`。
- 知识库内**单个文件**的移除复用现有的 `knowledge_cleanup` 路径（与 HTTP `POST /knowledge/{id}/file/remove` 同源），不用 `legacy_delete`。
- 成功文案模板：`「已移入回收站，7 天内可恢复。」`（回收期以实际配置为准，文案取值来自配置而非写死）。
- 服务端返回 409 `{'reason': 'protected_resource'}` 时 → `failed` + `PROTECTED_RESOURCE`，`message` 必须给出可执行的解除方式，**不得重试**。

---

## 9. 权限判定（唯一权威）

工具内部的权限判定**必须**复用下列同一函数，不得新增第二套：

| 操作 | 判定 |
| --- | --- |
| 笔记读 | owner ∥ admin ∥ `AccessGrants.has_access(resource_type='note', permission='read')` |
| 笔记改 | owner ∥ admin ∥ `AccessGrants.has_access(resource_type='note', permission='write')`，由既有 `_has_write_access_to_note`（`builtin.py`）提供 |
| **笔记删** | **仅 owner**（见下方说明） |
| 知识库读 | owner ∥ admin ∥ `AccessGrants.has_access(resource_type='knowledge', permission='read')` |
| 知识库写/删 | owner ∥ admin ∥ `AccessGrants.has_access(resource_type='knowledge', permission='write')` |
| 建知识库 | `user.role == 'admin'` ∥ `has_permission(user_id, 'workspace.knowledge', ...)` |
| 文件存入知识库 | 知识库写权限 **且** 该文件对发起人可读（与 `routers/knowledge.py` 的 `file/add` 同源） |

**为什么「笔记删」只允许 owner**：删除最终必须经过 `utils/resource_service.py:owned_row`，该函数对 `note` 明确**不设管理员后门**（源码注释：*The management page deliberately does not add an administrator backdoor for inspecting another person's private content*），对非 owner 一律抛 404；写授权（write grant）同样会被拒。

因此工具**必须**与之对齐：非 owner（**包括管理员**、包括仅持写授权的协作者）一律返回 `PERMISSION_DENIED`，**不得**签发确认令牌。否则会发出一个必然失败的令牌，把「无权删除」伪装成「确认后即可删除」。

这与 HTTP `DELETE /api/v1/notes/{id}/delete` 的前置检查存在**已知不一致**（该路由的检查允许管理员/写授权，随后在 resource service 内 404）。本契约不对齐那个宽松检查，只对齐实际可达的行为；该不一致记录于第 12.3 节，不在本项修复范围内。

补充要求：

- 建库被 `workspace.knowledge` 拦截时返回 `PERMISSION_DENIED`，`message` 说明「需要知识库创建权限，请联系管理员」。
- **不得**因为工具调用而放宽 `BYPASS_ADMIN_ACCESS_CONTROL` 的既有语义。

---

## 10. 工具注入规则

`utils/tools.py:get_builtin_tools()` 的知识库分支改为**发现类工具与挂载内工具并存**，不再二选一。

### 10.1 只读工具

```
总是注入（当 builtinTools.knowledge 开启）：
    list_knowledge_bases, search_knowledge_bases, query_knowledge_files
若 model_knowledge 非空（模型挂了知识）：
    + list_knowledge, search_knowledge_files, grep_knowledge_files, view_knowledge_file
若 model_knowledge 为空：
    + query_knowledge_bases
若挂载类型包含 file / collection：
    + view_file, view_knowledge_file
若挂载类型包含 note：
    + view_note
```

修复点：挂载后**保留** `list_knowledge_bases` / `search_knowledge_bases`，使 agent 仍能发现其他知识库。

### 10.2 写工具

```
当 builtinTools.knowledge 开启        ← 写操作以可读为前提
且 builtinTools.knowledge_manage 开启
且 user 具备对应权限（第 9 节，逐工具在调用时再判一次）
且 is_note_chat == False             ← 笔记内嵌聊天永不注入写工具
则注入：create_knowledge_base, update_knowledge_base, add_text_to_knowledge,
        create_knowledge_directory, remove_file_from_knowledge, delete_knowledge_base
```

**写依赖读**：管理员把 `knowledge`（只读）关掉时，写工具**一并**不注入。否则会留下「能写不能读」的怪状态——agent 写完无法确认结果，且这等同于绕过管理员的关闭意图。反过来「只读不写」由 `knowledge_manage=false` 表达，两者职责不重叠。

`preview_delete` 随「被其服务的删除工具」一同注入：
- `notes` 类别 → `preview_delete`（覆盖 `note`）+ `delete_note`
- `knowledge_manage` 类别 → `preview_delete`（覆盖 `knowledge` / `knowledge_file`）+ 知识库删除工具

同一请求中只注入一份 `preview_delete`。

### 10.3 笔记内嵌聊天（`is_note_chat=True`）

```
必定注入：search_notes, view_note, write_note, replace_note_content
若 builtinTools.knowledge 开启且用户有知识库读权限：
    + 第 10.1 节的只读知识库工具
永不注入：知识库写工具、delete_note
```

理由：笔记场景下的删除属于高危误操作，且用户此刻的注意力在编辑而非管理。

### 10.4 开关优先级（高 → 低）

1. 模型 `info.meta.capabilities.builtin_tools == false` → 全部不注入（既有语义）；
2. 对应 `builtinTools.<category> == false` → 该类别不注入；
3. 特性开关（`notes.enable` 等）；
4. 用户权限（第 9 节）。

---

## 11. 各工具参数与返回

### 11.1 `delete_note`（S1）

```python
async def delete_note(note_id: str, confirmation_token: str,
                      __request__: Request = None, __user__: dict = None) -> str
```

- 成功 `data`：`{"note_id": "...", "title": "...", "recycle_expires_at": "<ISO-8601 UTC>"}`
- 失败码：`PERMISSION_DENIED` / `NOT_FOUND` / `CONFIRMATION_REQUIRED` / `PROTECTED_RESOURCE`

### 11.2 `create_knowledge_base`（S2）

```python
async def create_knowledge_base(name: str, description: str = "",
                                __request__: Request = None, __user__: dict = None) -> str
```

- 成功 `data`：`{"knowledge_id": "...", "name": "...", "created_at": "<ISO-8601 UTC>"}`
- 失败码：`PERMISSION_DENIED` / `INVALID_ARGUMENT`（空名称）/ `FEATURE_DISABLED`
- 必须与 HTTP `POST /knowledge/create` 走同一校验（含 `filter_allowed_access_grants`）。

### 11.3 `update_knowledge_base`（S2）

```python
async def update_knowledge_base(knowledge_id: str, name: Optional[str] = None,
                                description: Optional[str] = None,
                                __request__: Request = None, __user__: dict = None) -> str
```

- 至少一个字段非空，否则 `INVALID_ARGUMENT`。
- 成功 `data`：`{"knowledge_id": "...", "name": "...", "description": "..."}`

### 11.4 `add_text_to_knowledge`（S2）

```python
async def add_text_to_knowledge(knowledge_id: str, filename: str, content: str,
                                directory_id: Optional[str] = None,
                                __request__: Request = None, __user__: dict = None) -> str
```

- 内部：先落一个 `text/markdown` 文件 → 触发 `process_file` → 关联到知识库。
- 返回 **`accepted`**，`data`：`{"knowledge_id", "file_id", "filename", "status": "processing"}`。
- 后续可用既有的文件状态查询跟进；**不得**同步等待到超时后报成功。
- 失败码：`PERMISSION_DENIED` / `NOT_FOUND` / `INVALID_ARGUMENT` / `PROCESSING_FAILED`。

### 11.5 `create_knowledge_directory`（S2）

```python
async def create_knowledge_directory(knowledge_id: str, name: str,
                                     parent_id: Optional[str] = None,
                                     __request__: Request = None, __user__: dict = None) -> str
```

- 唯一约束冲突（同名同父）→ `INVALID_ARGUMENT`，`message` 说明已存在。

### 11.6 `remove_file_from_knowledge`（S2）

```python
async def remove_file_from_knowledge(knowledge_id: str, file_id: str,
                                     confirmation_token: str,
                                     __request__: Request = None, __user__: dict = None) -> str
```

- 资源键：`knowledge_file:<knowledge_id>:<file_id>`
- 复用 HTTP `file/remove` 的 `knowledge_cleanup` 路径（含 `confirm_shared` 语义）。
- 共享资源触发保护时 → `PROTECTED_RESOURCE`。

### 11.7 `delete_knowledge_base`（S2）

```python
async def delete_knowledge_base(knowledge_id: str, confirmation_token: str,
                                __request__: Request = None, __user__: dict = None) -> str
```

- 资源键：`knowledge:<knowledge_id>`
- 成功 `data`：`{"knowledge_id", "name", "file_count", "recycle_expires_at"}`

---

## 12. 时间、异步与已知边界

### 12.1 时间戳

- 数据库层：`note.*_at` 是**纳秒**（`time.time_ns()`），`knowledge.*_at` 是**秒**（`time.time()`）。
- **工具层统一输出 ISO-8601 UTC 字符串**（如 `2026-11-10T06:40:00Z`），不输出裸数字，杜绝单位混用。
- 既有工具的时间戳字段保持不变（不回归）。

### 12.2 异步

- `add_text_to_knowledge` 是唯一异步工具，状态 `accepted`。
- 其余工具同步完成。
- 工具**不得**内置长时间轮询；跟进交给模型按需调用既有状态查询工具。

### 12.3 已知边界（明确声明，不假装解决）

| 边界 | 影响 | 处置 |
| --- | --- | --- |
| 令牌为进程内存储 | 多副本部署时跨进程无效 | 当前单实例部署成立；多实例前须改为共享存储，届时本契约升版 |
| 删除是回收站而非永久 | 磁盘不会立即释放 | 由既有资源回收流程处理 |
| `knowledge_manage` 默认开启 | 升级后管理员模型立即具备写能力（仍受用户权限约束） | 安全边界在用户权限与令牌；管理员可逐模型关闭 |
| 权限判定为「调用时判定」 | 注入时用户可能有权限、调用时已被撤销 | 以调用时判定为准，返回 `PERMISSION_DENIED` |
| 笔记删除仅限 owner | 管理员无法代删他人笔记；HTTP 路由的前置检查更宽松但同样会失败 | 工具如实返回 `PERMISSION_DENIED`；如需管理员代删，应交由资源管理页的既有流程单独决策 |

---

## 13. 验收与变更

### 13.1 对应验收条目

| 契约条目 | P-007 验收 |
| --- | --- |
| 第 4.2 节工具清单 | K1–K4, K9 |
| 第 5 节信封与状态 | K2, K7 |
| 第 7 节令牌 | K4, K9 |
| 第 8 节回收站 | K5, K9 |
| 第 9 节权限 | K6, K10 |
| 第 10 节注入 | K11–K13 |

### 13.2 变更流程

1. 先改本文件的**后端权威副本**；
2. 同步到前端仓库的同名文件（内容逐字一致，仅仓库位置不同）；
3. 再改代码；
4. 后端 `backend/runtime_tests/` 补测试，前端补 `*.test.ts`；
5. 两仓文档版本不一致时，以**后端副本**为准，并在 PR 描述里注明同步提交。

### 13.3 版本

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-11-10 | 初版，对应 P-007 S1/S2 |
