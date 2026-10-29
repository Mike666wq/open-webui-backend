# 供应商与模型管理 —— API 契约

> 本文件是 `open-webui-backend` 与 `open-webui-frontend` 之间的**权威契约**。
> 后端仓库为权威副本，前端仓库保存同内容副本。实现前先改这里，再改代码。
>
> 参照 Cherry Studio 的「模型服务」信息架构重做，取代旧的「目录 / 已发现 / 已启用」模型。

---

## 1. 术语

| 术语                                | 含义                                         | 取代旧概念                          |
| ----------------------------------- | -------------------------------------------- | ----------------------------------- |
| **提供商类型（protocol）**          | 决定「怎么调」的协议族                       | 无（旧实现隐含在 `provider_id` 里） |
| **提供商模板（provider template）** | 预置厂商预设：名称、类型、默认地址、认证方式 | 旧 `PROVIDER_CATALOG` 裸数组        |
| **模型白名单（curated whitelist）** | 每个提供商一份，由用户勾选哪些模型可用       | 旧「已发现 / 已启用」二元状态       |
| **候选（candidate）**               | 从上游拉到、但尚未加入白名单的模型           | 旧「目录」内容                      |
| **厂商级启用开关**                  | 整个提供商开 / 关                            | 旧无对应物（只能逐模型关）          |

---

## 2. 协议枚举

| protocol             | 说明                                          | 本轮状态                |
| -------------------- | --------------------------------------------- | ----------------------- |
| `openai-compatible`  | OpenAI 兼容 `/chat/completions`               | **已实现**              |
| `openai-responses`   | OpenAI `/responses`（ChatGPT 订阅账号走这条） | **已实现**              |
| `anthropic-messages` | Anthropic `/v1/messages`                      | 登记，`supported=false` |
| `gemini`             | Google Gemini `generateContent`               | 登记，`supported=false` |
| `azure-openai`       | Azure OpenAI                                  | 登记，`supported=false` |
| `bedrock-converse`   | AWS Bedrock Converse                          | 登记，`supported=false` |
| `local`              | 本地推理（Ollama / LM Studio 等）             | 登记，`supported=false` |

`supported=false` 的类型**必须**在添加连接时被明确拒绝并给出原因，**绝不允许**按 OpenAI 兼容协议错误调用。

---

## 3. 提供商模板分组

| group      | 名称           | 说明                               |
| ---------- | -------------- | ---------------------------------- |
| `official` | 官方           | OpenAI、ChatGPT                    |
| `cn`       | 国产           | MiniMax、DeepSeek、硅基流动、百炼  |
| `gateway`  | 聚合与特殊网关 | New API、One API、AiHubMix、DMXAPI |
| `other`    | 其他           | 尚未实装的协议类型                 |

---

## 4. 错误分类（五类）

后端 `catalog_error()` 与所有前端错误展示**必须**使用同一套分类：

| category           | 含义                         | 触发条件                             |
| ------------------ | ---------------------------- | ------------------------------------ |
| `connection`       | 本站与厂商之间的连接建立失败 | DNS 解析失败、TCP 超时、TLS 握手失败 |
| `provider_network` | 连接被中间层拦截             | SSRF 防线拦截、代理拒绝              |
| `authentication`   | 凭据或权限被拒               | 上游 401 / 403                       |
| `rate_limit`       | 限流或配额耗尽               | 上游 429 / 402                       |
| `upstream`         | 厂商自身服务异常             | 上游 5xx、非法响应体、响应过大       |

错误对象结构（JSON）：

```json
{
	"category": "provider_network",
	"status": 502,
	"http_status": 502,
	"upstream_status": null,
	"retryable": true,
	"at": 1793081473,
	"hint": "连接被服务器的安全策略拦截，请检查地址解析结果与代理配置。",
	"stage": "catalog",
	"reason": "address_blocked",
	"suggested_action": "check_server_network"
}
```

- `hint` 是**可直接展示的中文短句**，由后端产出；前端只渲染，不自行拼装。
- `status` = `upstream_status or http_status`。
- `upstream_status` 只有在上游真的返回了非 2xx 时才有值；本地网关错误（502）**不算**上游错误。

### 4.1 阶段、原因与建议动作

三个**新增可选字段**，与既有字段并存。旧客户端只读 `category/status/hint`，行为不变。

| 字段               | 取值                                                                                                                                                                                                       | 含义             |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `stage`            | `catalog` / `oauth_refresh` / `inference`                                                                                                                                                                  | 失败发生在哪一步 |
| `reason`           | `invalid_credentials` / `token_expired` / `insufficient_permissions` / `rate_limited` / `quota_exhausted` / `address_blocked` / `dns_failure` / `tls_failure` / `timeout` / `upstream_failure` / `unknown` | 为什么失败       |
| `suggested_action` | `reconnect` / `replace_key` / `check_permissions` / `check_server_network` / `retry_later` / `inspect_connection`                                                                                          | 运维下一步做什么 |

硬性约束：

- **阶段语义**：目录调用内部发生的 OAuth 刷新失败标 `oauth_refresh`，不标 `catalog`；推理失败标 `inference`。
- **未知不得伪装**：证据不足时 `reason` 用 `unknown`；**不得**把所有 403 说成凭据失效（403 = `insufficient_permissions`，动作 `check_permissions`）。
- **本地拦截**的 `upstream_status` 必须是 `null`。
- **动作必须匹配认证方式**：OAuth 连接给 `reconnect`，API Key 连接给 `replace_key`；403 一律 `check_permissions`。
- 旧错误对象（只有 `category`）前端**必须**能显示：按 category 回退出 reason / action / 摘要。

### 4.2 HTTP 错误响应

FastAPI 的默认处理器只回 `{"detail": ...}`，会把三个新字段丢掉。厂商类错误的 HTTP 响应统一为：

```json
{
	"detail": "Provider rejected the configured credentials or permissions",
	"error": { "category": "authentication", "reason": "insufficient_permissions", "...": "..." }
}
```

- 只有**真正的厂商错误**才带 `error`；参数校验、权限校验、找不到连接等仍然只回 `detail`，**不得**被包装成厂商错误。
- `GET /api/models` 的 `catalogs[]` 里每个连接的 `error` 同结构。

---

## 5. SSRF 防线与代理 fake-ip

- 厂商出站请求**必须**使用 `open_webui/utils/provider_http.py` 里的 `ProviderTCPConnector`（新 fork 自有模块）。
- **不得**修改上游文件 `open_webui/utils/apps.py`。
- 默认严格拒绝私网 / 回环 / 链路本地 / 组播 / 保留段（含 RFC 2544 `198.18.0.0/15`）。
- 需要放行代理 fake-ip 段时，部署方显式设置 `PROVIDER_ALLOWED_IP_RANGES=198.18.0.0/15`（逗号分隔 CIDR，默认空 = 不放行）。
- 被拦截时**必须**归类为 `provider_network`，`reason=address_blocked`，动作 `check_server_network`，`upstream_status` 为 `null`，**不得**折叠成 `network`。
- 恢复步骤（两条路线、前提、验证与回滚）见 `docs/PROVIDER_RECOVERY.md`。放行网段**只**解除本地拦截，**不**保证代理接管、登录成功或模型调用成功。
- `trust_env=False`：只设置宿主机 `HTTP_PROXY` / `HTTPS_PROXY` **不**构成可用代理。需要显式代理契约时按升级件处理，**不得**静默开启全局环境代理。

---

## 6. 接口

所有路径前缀 `/api/v1/providers`。

### 6.1 `GET /` —— 提供商模板目录

```json
{
	"object": "list",
	"data": [
		{
			"id": "openai",
			"name": "OpenAI",
			"protocol": "openai-compatible",
			"group": "official",
			"group_label": "官方",
			"auth_types": ["api_key"],
			"default_base_url": "https://api.openai.com/v1",
			"docs_url": "https://platform.openai.com/docs",
			"supported": true,
			"unsupported_reason": null
		}
	],
	"groups": [
		{ "id": "official", "label": "官方", "order": 0 },
		{ "id": "cn", "label": "国产", "order": 1 },
		{ "id": "gateway", "label": "聚合与特殊网关", "order": 2 },
		{ "id": "other", "label": "其他", "order": 3 }
	]
}
```

模板清单：

| id             | 名称          | protocol           | group    | 认证    | 默认地址                                            |
| -------------- | ------------- | ------------------ | -------- | ------- | --------------------------------------------------- |
| `openai`       | OpenAI        | openai-compatible  | official | api_key | `https://api.openai.com/v1`                         |
| `chatgpt`      | ChatGPT       | openai-responses   | official | oauth   | —                                                   |
| `minimax`      | MiniMax       | openai-compatible  | cn       | api_key | `https://api.minimax.chat/v1`                       |
| `deepseek`     | DeepSeek      | openai-compatible  | cn       | api_key | `https://api.deepseek.com/v1`                       |
| `siliconflow`  | 硅基流动      | openai-compatible  | cn       | api_key | `https://api.siliconflow.cn/v1`                     |
| `bailian`      | 百炼          | openai-compatible  | cn       | api_key | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `newapi`       | New API       | openai-compatible  | gateway  | api_key | —                                                   |
| `oneapi`       | One API       | openai-compatible  | gateway  | api_key | —                                                   |
| `aihubmix`     | AiHubMix      | openai-compatible  | gateway  | api_key | —                                                   |
| `dmxapi`       | DMXAPI        | openai-compatible  | gateway  | api_key | —                                                   |
| `kimi`         | Kimi Code     | openai-compatible  | cn       | oauth   | `https://api.kimi.com/coding/v1`                    |
| `anthropic`    | Anthropic     | anthropic-messages | other    | api_key | `https://api.anthropic.com`                         |
| `gemini`       | Google Gemini | gemini             | other    | api_key | —                                                   |
| `azure-openai` | Azure OpenAI  | azure-openai       | other    | api_key | —                                                   |
| `bedrock`      | AWS Bedrock   | bedrock-converse   | other    | api_key | —                                                   |
| `ollama`       | 本地推理      | local              | other    | api_key | `http://localhost:11434/v1`                         |

`supported=false` 的模板：`unsupported_reason` 必须非空。

### 6.2 连接

```
POST /connections
{
  "provider_template": "minimax",
  "protocol": "openai-compatible",
  "name": "我的 MiniMax",
  "auth_type": "api_key",
  "api_key": "sk-...",
  "config": { "base_url": "https://api.minimax.chat/v1" },
  "visibility": "private",
  "access_control": []
}
```

`provider_template` 必须存在于模板目录且 `supported=true`，否则 400。旧客户端仍可传 `provider_id`，其值按模板 id 解析。

```
PUT /connections/{id}/enabled
{ "enabled": false }
```

关闭后该连接的全部模型在 `/models` 与聊天选择器中都不可见。

### 6.3 模型白名单

```
POST /connections/{id}/models/fetch
→ {
    "candidates": [
      { "upstream_id": "gpt-4o", "display_name": "GPT-4o", "capabilities": ["chat"] }
    ],
    "error": null,
    "fetched_at": 1793081473
  }
```

只写入**候选池快照**（`provider_catalog_snapshot`），**不改变白名单、不改变任何模型状态**。失败时 `candidates` 为空数组，`error` 为五类错误对象。

```
GET /connections/{id}/models
→ {
    "data": [
      {
        "upstream_id": "gpt-4o",
        "display_name": "GPT-4o",
        "uses": { "chat": "openai-chat" },
        "enabled": true,
        "source": "provider",
        "verification": { "chat": { "status": "untested" } },
        "stale": false,
        "in_catalog": true
      }
    ]
  }
```

```
POST /connections/{id}/models
{ "upstream_ids": ["gpt-4o"], "display_names": { "gpt-4o": "GPT-4o" } }
```

加入白名单，默认 `enabled=true`、`source='provider'`（来自候选）或 `'manual'`（手工填 ID）。

```
PUT /connections/{id}/models/{upstream_id}
{ "display_name": "...", "uses": { "chat": "openai-chat" }, "enabled": true }
```

P-005 S1 扩展此 PUT 请求：可选 `capabilities` 为对象或 `null`。对象仅允许
`vision`、`file_upload`、`web_search`、`file_context` 四个键，值为 JSON 布尔值或
`null`；未知键、非对象和其他值拒绝（400）。省略表示不修改；顶层 `null`
清除全部显式声明；对象按键更新，`true` / `false` 设置显式声明，单键 `null`
删除该键声明（恢复供应商默认或未知），其他已声明键保留；`{}` 不改动。
`false` 不能当作未声明。
声明写在 `provider_model.model_info.capabilities`，原 `provider_model.capabilities` 保持
供应商能力标签的 list 语义，不迁移、不新增列。保留 `model_info` 的其他键。
写权限（裁决 A）：`capabilities` 声明沿用连接管理权限，即
`_connection_with_manage_access` 的现有语义——shared 连接仅管理员可写（403），
private 连接仅 owner 可写（其他用户 404）；不新增管理员查看或管理他人
私有连接的能力。

模型返回的 `model_info.capabilities` 是显式声明；`info.meta.capabilities` 是最终判定，
逐字段按「显式 bool > 供应商目录元数据 > 未知（字段缺失）」解析。
`vision` 的供应商证据是目录 `capabilities` 含 `VU` / `Visual Understanding`，
也兼容旧模型自身保存的同类标签；仅当目录快照 `fingerprint` 与当前连接
`catalog_fingerprint` 一致时采用目录标签，端点或凭据变化后的旧快照不作为能力证据；
目录没有视觉标签不能推断为 `false`。
`file_upload`、`file_context` 缺供应商证据时保持缺失；平台自身的 `web_search`
保持现有默认 `true`（与供应商原生搜索功能无关），可由显式 bool 覆盖。
`GET /api/models` 中服务商模型的 `info.meta.capabilities` 与单模型解析使用同一函数，
两者不能把未知视觉值填为 `false`。旧模型 alias 的 `info.meta.capabilities.vision`
也采用同一个服务商视觉判定（未知时移除该键），但保留 workspace 保存的其他能力、
元数据与原有模型访问检查；普通 workspace 模型能力默认值不变。
聊天的 OpenAI `image_url` 内容在服务商出站请求中原样透传，不做格式转换。

```
DELETE /connections/{id}/models/{upstream_id}
```

移出白名单（不动上游）。

### 6.4 兼容旧接口

保留并映射到新表：

| 旧接口                                  | 新行为                                                                     |
| --------------------------------------- | -------------------------------------------------------------------------- |
| `PUT /connections/{id}/model-enabled`   | → `provider_model.enabled` + 同步 `Model.is_active`                        |
| `PUT /connections/{id}/model-uses`      | → `provider_model.uses`                                                    |
| `POST /connections/{id}/models/refresh` | 等价于 `models/fetch`（不再自动改白名单），保留一个版本后删除              |
| `GET /models?purpose=`                  | 判定改为 `connection.is_enabled && model.enabled && purpose in model.uses` |

---

## 7. 可见性判定（后端唯一权威）

一个模型对某用户可见，当且仅当：

1. `provider_connection.is_active` 为真；
2. `provider_connection.is_enabled` 为真（厂商级开关）；
3. `provider_model.enabled` 为真；
4. `Model.is_active` 未被用户在工作区关闭（附加开关，OR 语义）；
5. `purpose in model.uses`；
6. 用户对该连接有访问权（private 仅 owner，shared 按 grant）。

前端**不得**做独立的 provider 启停判断，只消费后端结果。

---

## 8. 迁移期双读兜底

读模型时：

```
provider_model 中该连接的记录非空 → 用新表
否则若 config 里还有旧 _model_* 键 → 走 legacy 兜底（行为与改造前完全一致）
否则 → 空
```

写路径统一只写新表。任一连接的数据搬迁失败时，其旧 `config` 保持不动，模型不会丢失。

---

## 9. 恢复入口（前端强制）

失败必须**按连接**呈现，并且给出可执行的入口。

| 位置               | 必须有                                  | 禁止                                 |
| ------------------ | --------------------------------------- | ------------------------------------ |
| 模型选择器         | 按连接的短摘要 + 可达的「查看连接」操作 | 把长错误整段堆在模型列表上方         |
| 连接详情           | 短摘要 + 建议动作按钮 + 可展开的长说明  | 只有一句笼统的失败提示               |
| 连接详情（管理员） | 部署说明：DNS / 代理核查与两条恢复路线  | 把环境变量与配置文件路径写给普通用户 |
| 普通用户           | 权限适当的提示 + 自己能执行的恢复       | 泄露部署细节或他人连接               |

- 短摘要由 `stage` + `reason` 组成（例如「获取模型列表时地址被本地安全策略拦截」），长说明取 `hint`。
- 「查看连接」必须跳到该连接并选中它，不能只打开一个泛化页面。
- 模型选择器里的告警**不得**挤掉搜索框与模型操作；多连接失败时每个连接各自可点。
- 重新登录**沿用原连接**（`connection_id`），不得新建第二个连接；换 Key 不得清空白名单、改名、用途、启停与稳定 ID。

## 10. 前端术语对照（强制）

| 禁止出现                                  | 必须使用                                 |
| ----------------------------------------- | ---------------------------------------- |
| 目录 / 刷新目录 / 目录状态 / 目录刷新失败 | 获取模型列表 / 重新获取 / 上次获取       |
| 已发现 N · 已启用 M                       | 可用模型 N 个（另有候选 M 个待添加）     |
| `openai-chat` / `responses` 等适配器标识  | 聊天 / 语音转写 / 语音合成 / 生图        |
| 能力筛选 + 启用筛选（两个下拉）           | 单个用途筛选；候选池与白名单分区天然区分 |

后端字段名 `uses` 保留不变（内部结构），只改**用户可见文案**。

---

## 11. 供应商、连接、服务、模型与适配器（五个层级）

| 层级           | 负责什么                                        | 权威来源                                     |
| -------------- | ----------------------------------------------- | -------------------------------------------- |
| **供应商模板** | 预设、区域、认证方式、**支持哪些服务**          | `provider_registry.TEMPLATES[*]['services']`  |
| **连接**       | 某个所有者的账户 / Key / 访问范围                | `provider_connection`                         |
| **服务**       | chat / transcription / speech / image 的出站协议、地址、限制 | `provider_connection.config['services']` |
| **模型**       | 该服务下的**明确选择**                          | `provider_model.upstream_id` + `uses`         |
| **适配器**     | 请求 / 响应转换                                 | `provider_capabilities.ADAPTERS`              |

- 保留根 `protocol` 的旧聊天语义与旧值，**不再**用它断言全部媒体支持。
- **服务是否可用 = 模板支持 × 认证方式 × 适配器 × 模型声明的交集**；
  无法确认的模型进入「待分类」，**不**默认 chat。
- 同一 Key 可承载多用途，但每个用途可有自己的地址；不同账号 / 区域 / Key 用不同连接，
  **不**自动跨连接或跨区发送已有凭据。

完整矩阵、字段取值与本期支持范围见 `docs/PROVIDER_SERVICE_MATRIX.md`（两仓逐字一致）。

### 11.1 模板身份解析（A1）

**临时校验与正式保存必须走同一个解析函数**，结果必须完全相同：

```
resolve_template(provider_template, provider_id) -> {id, protocol, default_base_url, services, ...}
```

- `provider_template` 优先；缺省时才回退 `provider_id`。
- `provider_id` 缺省值 `custom` **不得**覆盖已选中的 MiniMax / 百炼 / DeepSeek 模板。
- 连接落库时 `provider_id = template.id`，`provider_template = template.id`，
  `protocol = template.protocol`；临时校验用同一份解析结果构造临时连接。

### 11.2 适配器枚举

| purpose        | 允许的适配器                                                |
| -------------- | ----------------------------------------------------------- |
| `chat`         | `openai-chat`、`responses`                                  |
| `transcription`| `openai-transcription`、`minimax-transcription`              |
| `speech`       | `openai-speech`、`minimax-speech`                           |
| `image`        | `openai-image`、`siliconflow-image`、`minimax-image`        |

`uses` 的每次校验（`POST /connections`、`POST /connections/{id}/models`、
`PUT .../model-uses`、`PUT .../models/{upstream_id}`）**使用同一套语义**：

1. purpose 必须在 `ADAPTERS` 里，adapter 必须在该 purpose 的集合里；
2. adapter 必须出现在**该模板该 purpose 的 `services[*].adapters`** 里
   （模板不支持该 purpose 时直接拒绝并给出 `unsupported_reason`）；
3. 认证方式必须被该服务声明允许（**OAuth 一律拒绝媒体**）；
4. **不得**从 `POST /connections` 绕过注册表，也**不得**给 OAuth 连接添加媒体用途。

---

## 12. 服务配置与地址解析

### 12.1 `config.services`

在现有 `provider_connection.config` JSON 内新增**可选** `services` 对象，**不新增 ORM 表、不改 Alembic 链**：

```json
{
  "services": {
    "transcription": {
      "enabled": true,
      "adapter": "minimax-transcription",
      "endpoint": { "kind": "base_url", "url": "https://api.minimax.cn/v1" },
      "options": { "response_format": "json", "language": "zh" }
    }
  }
}
```

| 字段      | 约束                                                                 |
| --------- | -------------------------------------------------------------------- |
| `enabled` | 布尔；省略视为 `true`                                                |
| `adapter` | 必须属于该模板该 purpose 的 `services[*].adapters`                    |
| `endpoint`| `{kind: 'base_url' \| 'endpoint_url', url: string}`                    |
| `options` | **只能**是该适配器明确允许的字段与取值；未知字段在出站前拒绝         |

- **省略 `services` 的旧连接原样读取**：原 `base_url` / `api_type` 与原模型 `uses` 全部保留。
- 新服务默认值**只**用于新建连接或用户明确启用该原生服务；
  **不**重写旧连接已有域名与 OpenAI 兼容媒体设置。
- `options` 缺省时使用适配器声明的默认值；缺省的 `model` 参数**不**等于「不传」。

### 12.2 地址两模式（A5）

| `kind`         | 语义                                     | 拼装                                   |
| -------------- | ---------------------------------------- | -------------------------------------- |
| `base_url`     | 含版本前缀的 API 根地址                  | 根地址 + 适配器登记的相对路径**一次**   |
| `endpoint_url` | 完整请求地址                             | **原样使用一次**，不再追加任何路径      |

- 适配器登记的相对路径（**唯一来源**）：

  | adapter                | 相对路径                |
  | ---------------------- | ----------------------- |
  | `openai-transcription` | `/audio/transcriptions` |
  | `openai-speech`        | `/audio/speech`         |
  | `openai-image`         | `/images/generations`   |
  | `siliconflow-image`    | `/images/generations`   |
  | `minimax-transcription`| `/speech_to_text`       |
  | `minimax-speech`       | `/t2a_v2`               |
  | `minimax-image`        | `/image_generation`     |

- **不得**按末尾字符串随意剥离或追加 URL。
- 用户把完整操作地址误填成 `base_url` 时，界面说明两种模式并提供修正；
  后端**不**默默生成重复后缀。
- 最终出站 URL（直填 endpoint 与生成结果下载地址）都要过校验：
  拒绝 userinfo、查询串、片段与未登记协议；沿用 DNS 连接检查、TLS 与禁重定向。
- 服务详情展示**脱敏**的最终 URL 预览，不含凭据。

---

## 13. 无列表创建与模型用途（A3 / A4）

### 13.1 三种发现方式

界面必须分别提供「获取模型列表」「使用官方预设」「手工添加」三条路径，
**默认是「使用官方预设」**，不是从远程取得。

| discovery | 是否请求远程 `/models` | 失败语义                       |
| --------- | ----------------------- | ------------------------------ |
| `remote`  | 是                      | 只描述该服务，既有候选/白名单不删除 |
| `preset`  | **否**                  | `not_applicable`，不写伪造错误 |
| `manual`  | 否                      | 同上                           |
| `none`    | 否                      | 同上                           |

`preset` / `manual` / `none` 的服务**不自动请求远程列表**，界面**不报「模型列表失败」**。

### 13.2 `POST /connections` 的 `model_selections`

```json
POST /api/v1/providers/connections
{
  "provider_template": "minimax",
  "name": "我的 MiniMax",
  "api_key": "sk-...",
  "config": {
    "services": {
      "transcription": {
        "enabled": true,
        "adapter": "minimax-transcription",
        "endpoint": { "kind": "endpoint_url", "url": "https://api.minimax.cn/v1/speech_to_text" }
      }
    }
  },
  "model_selections": [
    {
      "upstream_id": "asr-1.0",
      "display_name": "asr-1.0",
      "uses": { "transcription": "minimax-transcription" },
      "source": "preset"
    }
  ]
}
```

- `source ∈ preset | manual | provider`；`preset` / `manual` 是**可追溯的声明**，**不是**验证成功。
- 与旧 `config.model_ids` **并存兼容**；同一请求里两者同时存在且**不一致**时**拒绝**（400），
  **不得**默默覆盖。
- 后端逐条校验用途 / 适配器与模板服务、鉴权、区域一致（见 §11.2）。
- **创建连接、初始白名单、用途与共享授权在同一事务提交**；任何一项失败不留下半个连接。
- 保存后该模型保持 `untested` 状态；`asr-1.0` 被明确选中后成为**转写白名单**，
  **不**进入聊天列表。

### 13.3 旧数据兼容

- **没有证据 / 没有显式用途的新模型 `uses={}`**，标「待分类」；**不**再以缺候选默认 chat。
- 旧客户端明确使用原 chat 连接的兼容路径**保留既有行为**，
  但必须与新「未知」区别测试；**不得**以兼容为名批量改旧白名单。
- 旧连接、旧设置与已有非 provider 引擎**不**被自动迁移或清空。

---

## 14. 统一媒体调用语义（A10）

**显式测试与日常功能必须使用同一个适配器选择、地址解析、输入约束与响应规范。**
后端提供唯一入口 `open_webui/utils/provider_media.py`，任何一方都**不得**另复制一套 OpenAI 拼路径逻辑。

```
provider_media.call_media_service(service, *, model, credential, input) -> normalized
```

| 归一化结果              | 形状                                                        |
| ----------------------- | ----------------------------------------------------------- |
| 转写                    | `{text: str, duration: float \| None, segments: [...]}`      |
| 合成                    | `{audio: bytes, content_type: str}`                          |
| 生图                    | `{images: [{data: bytes \| None, url: str \| None, content_type}]}` |

### 14.1 MiniMax ASR（A6 / A7）

- `POST speech_to_text`，`Bearer` 鉴权，multipart 文件上传。
- multipart **必有** `model=asr-1.0` 与音频文件；boundary 正确。
- `language` 作为**该适配器的请求头**；`json` / `verbose_json` 结果规范化。
- **不**发送 `diarized_json`、SSE、SRT/VTT，**不**把多语言当自动重试循环。
- 输入限制：**≤ 50 MiB 且 ≤ 500 秒**；普通转写与录音**同时**遵守。
- 浏览器 WebM 等未声明容器按现有受限 ffmpeg 流程转码；
  **缺工具时在出站前给出明确不可用原因**，**不能**只改扩展名。

### 14.2 录音分片（A7）

- MiniMax 录音分片目标 **480 秒**（≤ 500，留编码边界余量）；
  通用 **600 秒**上限仍对其他未声明更小限制的适配器成立。
- 新任务快照保存实际 `chunk_seconds` 与服务 / 适配器 / 模型 / 参数指纹；
  公共总片数、偏移、循环边界、检查点恢复**读同一快照**。
- 已有任务**缺** `chunk_seconds` 时按旧 **600** 解释。
- 切换模型 / 服务 / 分片几何使旧检查点不兼容时**必须明确重开**，
  **不能**暗中重放可能已计费的片段。
- 片段结果按序合并且不重复；**不**假造说话人跨片对应。

### 14.3 MiniMax 同步 TTS（A8）

- `POST t2a_v2`；携带有效 `model`、`text`、`voice_setting` 与 `audio_setting`，
  `audio_setting.format=mp3`；`stream=false` 与 `output_format=hex` 位于请求顶层。
- **非零 `base_resp` 先转错误**；合法 `data.audio` 按 hex 解码并校验实际音频。
- **不得**把 JSON、错误文字或非法 hex 写成 mp3。
- 系统音色查询（`POST /v1/get_voice`）与手工 voice ID 可用；
  **不**硬塞 OpenAI alloy 或硅基流动音色。
- 本期服务元数据标 `streaming=false`。

### 14.4 MiniMax 文生图（A9）

- `POST image_generation`；`model` 为已登记预设，`prompt` 必填，`n` 受支持范围约束。
- 尺寸映射为该 model 支持的 `width`/`height` 或 `aspect_ratio`；
  **不**发送 OpenAI `size` 字符串作为未知字段。
- 首期默认请求 `base64`；若使用 URL 模式则解析 `data.image_urls` 并通过**受控下载**，
  **不**把 Key 转发给外部 CDN。
- 业务失败 / 部分失败**不**伪装全成功；全部失败**不得**记 `passed`。
- 未支持的编辑、尺寸或参数在**发出请求前**拒绝。

### 14.5 状态与错误（A11）

- 某一服务的目录 / 鉴权 / 业务失败**不覆盖**其他服务。
- HTTP 200 中的非零 `base_resp` **不当成功**。
- Key / 服务地址 / 适配器 / 对应测试参数变化使相关测试**过期**；
  结果写回前比对当前指纹，**迟到响应不覆盖新状态**。指纹**不得公开**。
- 五类旧错误仍能显示。

---

## 15. 错误字段扩展（可选，兼容缺省）

在 §4.1 的三个字段之外**新增可选字段**，旧客户端忽略即可：

| 字段                 | 含义                                             |
| -------------------- | ------------------------------------------------ |
| `upstream_code`      | 上游业务码（如 MiniMax `1004`）                  |
| `upstream_http_status` | 上游**真实** HTTP 状态（业务失败时为 `200`）   |
| `trace_id`           | 上游追踪 ID（脱敏后展示）                        |
| `purpose`            | 媒体错误的用途（`transcription`/`speech`/`image`） |

- 原 `upstream_status` 对真正非 2xx **仍保留**。
- HTTP 200 的业务失败用 `upstream_http_status=200` + 真实业务码表达，
  **不**伪造上游 HTTP 401 / 429。
- 新增 `reason`：`invalid_parameters`、`content_rejected`，
  由固定脱敏 `hint` 说明参数或内容，沿用 `inspect_connection` 动作。
  本地输入校验仍为**本地 4xx**，**不是**厂商故障。
- 媒体错误标 `stage=inference`。

---

## 16. 本期新增 / 变更的接口

| 接口 | 变更 | 说明 |
| ---- | ---- | ---- |
| `GET /` | 响应新增 `service_contract_version: 1` 与模板 `services` 元数据 | 前端不再自带一份能力清单 |
| `GET /connections`、`GET /connections/{id}` | 响应新增 `resolved_services`（脱敏）与 `service_statuses` | 每个用途独立显示状态 |
| `POST /connections` | 请求新增**可选** `model_selections` | 与旧 `config.model_ids` 并存兼容 |
| `POST /connections/verify` | 与正式保存共用模板解析 | 修复身份丢失 |
| `GET /models?purpose=` | 支持 `purpose` 与可选 `operation` | 媒体未配置**不**回退到 chat |
| `POST /connections/{id}/models/fetch` | 请求新增**可选** `purpose` | 无参数保持旧行为 |
| `POST /connections/{id}/voices/fetch` | **新增** | 入参 `purpose='speech'`、`voice_type`；返回 `{data, error, fetched_at}` |
| `POST /connections/{id}/model-test` | 走统一媒体契约 | 新增音频上传、音色与受支持选项由测试对话框提交 |

`POST /connections/{id}/voices/fetch` 的约束：

- 不支持音色查询的适配器返回**明确本地 4xx**，**不发推理**。
- 访问者须具有连接使用权。
- `voice_type=all` 或自有克隆 / 生成音色**仅管理者**可查；
  普通授权使用者默认只见 `system` 或连接明确共享的音色。
- 查询**不是激活**：不在每次页面加载调用推理或音色激活。

---

## 17. 调用回执

- 回执保留稳定模型、`purpose`、结果与耗时。
- 能获取的时长 / 字符 / 图片数量用**对应单位**存进现有 `usage` JSON；
  **无计量则 `null`**，**不伪造** token 或费用。
- 记录**不得**包含上传音频、合成文本、图片内容、Key 或原始业务错误体。

---

## 18. 模型测试流与连接移除（P-003）

- 保留 `POST /connections/{id}/model-test` 及原 JSON 语义；新增
  `POST /connections/{id}/model-test-stream`，使用相同载荷、权限、适配器、
  配置指纹和测试结果保存规则。浏览器以带认证的 `fetch` POST 消费 SSE。
- 事件为 `{test_id, seq, phase, elapsed_ms, data}`；phase 为 `accepted`、
  `request_started`、`response_headers`、`output_delta`、`completed` 或 `failed`。
  耗时取服务端单调时钟，自该次测试开始；仅实际观测到的阶段才发出。
  建立流之前拒绝返回 HTTP 错误，建立之后失败以终态事件返回。
- 终态包含原有 `status`、`elapsed_ms`、`error`，并提供 `timings`：
  `upstream_headers_ms`、`first_output_ms`、`upstream_total_ms`；无法观测为 null。
  测试总耗时从首次管理权限检查和模型解析之后开始，包括后续凭据准备、上游请求与响应处理，
  不含结果写回；事件耗时使用同一计时起点，终态事件可包含写回时间。浏览器耗时单独标注。
- 支持流式的聊天模型仅发一次流式推理，以首个非空回答文本增量记录首输出；
  非流式不测首输出。取消、断流、超时或解析失败不得记成功、自动重试或重新推理；
  样例输出只临时展示，最多 8 KiB，不能存入调用记录。旧后端不支持流端点时，
  仅在确认尚未发送推理时回退 JSON 测试，并标注只有总耗时。
- API Key 连接通过原 DELETE 移除；OAuth 通过对应的断开接口，保留刷新与断开锁。
  本站删除成功与远端撤销是否确认分开报告；删除完成后的目录刷新失败单独报告。
  连接、模型和访问权限删除后不可用于新请求，保留历史聊天文本。
- MiniMax 媒体服务继续使用现有加密连接凭据与独立用途模型；不同 Key 应建立独立
  连接，不覆盖聊天凭据。转写与合成地址分别为 `/v1/speech_to_text` 和 `/v1/t2a_v2`，
  Base URL 和完整 URL 均不得重复追加路径；保存配置、列目录和查音色不触发推理。
- `POST /connections/{id}/models` 可选 `source` 为 `provider`（默认）、`manual` 或 `preset`；
  `preset` 只接受该用途的官方声明中的模型和适配器，保留来源，不影响旧请求。
