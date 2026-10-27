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
