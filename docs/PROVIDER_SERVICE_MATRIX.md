# 供应商服务矩阵 —— 聊天 / 语音转写 / 语音合成 / 生图

> 本文件是 `open-webui-backend` 与 `open-webui-frontend` 之间的**权威副本**（两仓逐字一致）。
> 矩阵由后端 `open_webui/utils/provider_registry.py` 的 `TEMPLATES[*]['services']` 产出；
> 前端**不得**维护另一份「协议都支持媒体」的清单。
>
> 配套契约见 `docs/PROVIDER_CONTRACT.md`（第 11–16 节）。

---

## 1. 五个层级，各管各的

| 层级           | 负责什么                                                   | 权威来源                                   |
| -------------- | ---------------------------------------------------------- | ------------------------------------------ |
| **供应商模板** | 预设、区域、认证方式、**支持哪些服务**                     | `provider_registry.TEMPLATES[*]['services']` |
| **连接**       | 某个所有者的账户 / Key / 访问范围                          | `provider_connection`                      |
| **服务**       | chat / transcription / speech / image 的出站协议、地址、限制 | `provider_connection.config['services']`    |
| **模型**       | 该服务下的**明确选择**                                     | `provider_model.upstream_id` + `uses`      |
| **适配器**     | 请求 / 响应转换                                            | `provider_capabilities.ADAPTERS`            |

**服务是否可用 = 模板支持 × 认证方式 × 适配器 × 模型声明的交集。**
任何一层缺证据，结论就是「未确认」，**不得**默认落到 chat。

---

## 2. 本期支持矩阵

`✓` = 已登记并有可用适配器；`✗` = 明确不支持（带原因）；`—` = 本期不新增（另行派工）。

| 模板 / 认证        | 聊天                       | 语音转写                | 语音合成                | 生图                        | 列表策略                        |
| ------------------- | -------------------------- | ----------------------- | ----------------------- | --------------------------- | ------------------------------- |
| MiniMax API Key     | 保留现有聊天适配            | ✓ `minimax-transcription` | ✓ `minimax-speech`     | ✓ `minimax-image`（**仅文生图**） | 聊天远程；媒体**预设 / 手工**，互不为前置 |
| OpenAI API Key      | 保留 Chat / Responses       | ✓ `openai-transcription` | ✓ `openai-speech`      | ✓ `openai-image`             | 远程可选 + 手工                  |
| 硅基流动 API Key    | 保留                        | ✓ `openai-transcription` | ✓ `openai-speech`      | ✓ `siliconflow-image` / `openai-image` | 分类远程列表 + 手工     |
| DeepSeek API Key    | 保留                        | ✗ 未提供                | ✗ 未提供                | ✗ 未提供                     | 聊天远程 / 手工                  |
| 百炼 API Key        | 保留原生文本目录 + 兼容推理  | — 本期不新增            | — 本期不新增            | — 本期不新增                 | 媒体独立适配另派                 |
| ChatGPT / Kimi OAuth| 保留原范围                  | ✗ 账号仅覆盖文本推理     | ✗ 同左                  | ✗ 同左                      | 不因媒体选项扩大订阅权限        |
| 自定义 / 聚合 API Key| 用户明确声明的兼容协议       | ✓ 已登记适配器          | ✓ 同左                  | ✓ 同左                      | remote / manual                  |
| 其他未实装模板      | 保持当前支持标记与原因      | ✗ 按服务声明            | ✗ 同左                  | ✗ 同左                      | 不伪装 OpenAI 兼容调用          |

**关键规则**

- `openai-compatible` 只描述**聊天**协议，**不蕴含**任何媒体能力。
- OAuth 连接的媒体服务**一律不可用**：用户不能靠改 `uses` 或 endpoint 把订阅账号转成媒体权限。
- 未知媒体模型**不默认进聊天**：没有证据的 `uses` 为空，标「待分类」。
- 选「兼容模式」**不等于**保证全部能力；实际以该次调用为准。

---

## 3. 服务声明字段

每个模板的每个 purpose 声明如下字段（`GET /api/v1/providers` 随模板一起下发）：

| 字段                 | 取值                                             | 含义                                             |
| -------------------- | ------------------------------------------------ | ------------------------------------------------ |
| `supported`          | `true` / `false`                                 | 该模板是否提供此服务                             |
| `unsupported_reason` | `string` / `null`                                | 不支持的原因（给用户看，不含部署细节）           |
| `adapters`           | `string[]`                                       | 该服务允许的适配器 ID                             |
| `auth_types`         | `string[]`                                       | 该服务允许的认证方式                             |
| `discovery`          | `remote` / `preset` / `manual` / `none`          | 模型从哪来（**不是**「有没有模型」）             |
| `model_requirement`  | `required` / `defaulted` / `none`                | 模型参数是否必传                                 |
| `endpoint`           | `{kind, url}`                                    | 默认地址（`kind` 见第 4 节）                     |
| `preset_models`      | `[{upstream_id, display_name, uses}]`             | 官方预设模型                                     |
| `limits`             | `{max_bytes, max_seconds, input_formats, ...}`   | 输入限制                                         |
| `options`            | `{field: [allowed values]}`                      | 该适配器允许的选项与取值                         |
| `operations`         | `string[]`（**可选**，仅在有多种操作时出现）      | 该适配器能做的操作（如 `generate`、`edit`）       |

### `operations`：适配器能做什么

一个服务默认只有**一种**操作，名字就是用途本身，此时**不出现** `operations`。
生图是唯一有多种操作的服务，所以它单独声明：

| adapter            | `operations`          | 界面含义                                   |
| ------------------ | --------------------- | ------------------------------------------ |
| `openai-image`     | `['edit', 'generate']`| 能生成，也能编辑                           |
| `siliconflow-image`| `['generate']`        | **只能生成**                               |
| `minimax-image`    | `['generate']`        | **只能生成**                               |

- 模板层只声明「这个模板的生图服务可能做什么」；
  连接层（`resolved_services[*].operations`）声明「**这个连接真正用的那个适配器**能做什么」。
- 前端据此决定**是否展示编辑入口**：生图可用**不**等于编辑可用。
- 后端在出站前再次判定（`adapter_supports_operation`），前端只是提前说明。

### `model_requirement` 三态

| 值          | 含义                                                              |
| ----------- | ----------------------------------------------------------------- |
| `required`  | 模型必传，调用方必须显式选定                                     |
| `defaulted` | 有官方默认值，但**仍然必须发送**；不能当成「无模型」              |
| `none`      | 协议真的没有模型参数                                              |

MiniMax ASR 是 `defaulted`：`model=asr-1.0` 仍必须出现在 multipart 里。
本期**没有**真正 model-less 的厂商 API，**不得**为其伪造模型行或假 ID。

### `discovery` 四态

| 值       | 界面动作             | 是否发起远程 `/models` |
| -------- | -------------------- | ---------------------- |
| `remote` | 「获取模型列表」     | 是                     |
| `preset` | 「使用官方预设」（默认） | **否**           |
| `manual` | 「手工添加」         | 否                     |
| `none`   | 不提供模型下拉       | 否                     |

`preset` / `manual` / `none` 的服务**不自动请求远程列表，也不写伪造目录错误**。
`remote` 失败只描述该服务，既有候选与白名单不删除。

---

## 4. 地址两模式

| `kind`         | 含义                                             | 拼装                       |
| -------------- | ------------------------------------------------ | -------------------------- |
| `base_url`     | 含版本前缀的 API 根地址                          | 根地址 + 适配器登记的相对路径**一次** |
| `endpoint_url` | 完整请求地址                                     | **原样使用一次**，不再追加  |

MiniMax 媒体默认根 `https://api.minimax.cn/v1`，分别追加：

| 服务   | 相对路径            | 完整地址示例                                        |
| ------ | ------------------- | --------------------------------------------------- |
| 转写   | `/speech_to_text`   | `https://api.minimax.cn/v1/speech_to_text`         |
| 合成   | `/t2a_v2`           | `https://api.minimax.cn/v1/t2a_v2`                 |
| 生图   | `/image_generation` | `https://api.minimax.cn/v1/image_generation`       |

硬性约束：

- **不得**按末尾字符串随意剥离或追加 URL；适配器只追加自己登记的相对路径**一次**。
- 用户把完整操作地址误填成 `base_url` 时，界面说明两种模式并提供修正；
  后端**不**默默生成 `/speech_to_text/speech_to_text` 之类的重复后缀。
- 最终出站 URL 必须过校验：拒绝 userinfo、查询串、片段与未登记协议；
  DNS 连接检查、TLS 与禁重定向沿用 `provider_http.py`，**不**因校验过旧 chat base_url 就跳过媒体目的地址。

---

## 5. 状态分层（四层，互不替代）

| 层                     | 取值                                                | 含义                                   |
| ---------------------- | --------------------------------------------------- | -------------------------------------- |
| **配置有效**           | `valid` / `invalid` / `missing`                    | 凭据与服务地址写得对不对               |
| **远程列表状态**       | `ok` / `failed` / `not_applicable` / `unknown`      | `/models` 拉取结果（预设服务 = `not_applicable`） |
| **免费探测状态**       | `ok` / `failed` / `not_available`                    | 有没有免费鉴权探测（多数厂商没有 → `not_available`） |
| **逐模型逐用途实际测试** | `untested` / `passed` / `failed`                  | 真正发过一次推理                       |

硬性约束：

- 首次保存允许「**已配置、未测试**」。没有免费探测时**据实标注**「未验证」。
- **不得**偷偷发付费推理来补一个「连接成功」。
- **不得**把 `/models` 失败当成所有服务连接失败；某一服务失败不覆盖其他服务。
- 目录候选 / 官方预设 / 手工声明**都不等于**实际调用通过。

---

## 6. MiniMax 原生协议要点（实现对照）

### 6.1 ASR `POST /v1/speech_to_text`

- `Bearer` 鉴权，`multipart/form-data`（boundary 由 multipart 生成器产生）。
- 必传 `model=asr-1.0` 与文件对象（`file`，不是文件名）。
- 只接受 `response_format=json|verbose_json`，`stream=false`。
- `language` 走**请求头**，不是同名 form 字段（同名 form 字段会被忽略）。
  未指定 / 混合语言时**省略**，**不**用多次调用逐语言试探。
- 输入 ≤ `50 × 1024 × 1024` 字节且 ≤ `500` 秒；支持 wav / aiff / flac / alac(m4a) / mp3 / aac / opus / ogg，
  **无裸 PCM**，不支持音频流式输入。
- `verbose_json` 的 `segments[]` 带 `start/end/speaker`；speaker 只在单片内有意义，跨片保持 namespace。
- `diarized_json`、字幕与 `stream` 组合**不在本期**，界面隐藏或校验拒绝。

### 6.2 同步 TTS `POST /v1/t2a_v2`

- 必传 `model`（speech 系列）与 `text`；`voice_setting.voice_id` 在提供该对象时必填。
- `audio_setting.format=mp3`、`stream=false`、`output_format=hex` → `data.audio` 是 **hex**，必须解码。
- `base_resp.status_code` 非 0 = **业务失败**，即使 HTTP 200 也不算成功。
- 音色查询 `POST /v1/get_voice`（无 model，`voice_type` 默认 `system`）是**只读元数据**，不激活音色。
- voice ID **独立于 model**；不得用 OpenAI / 硅基流动音色冒充 MiniMax 音色。
- 本期**不**声明原生 SSE / WebSocket 能力。

### 6.3 文生图 `POST /v1/image_generation`

- 必传 `prompt` 与 `model`；`prompt` ≤ 1500 字符；`n` 范围 1–9。
- `width`/`height` 512–2048 且为 8 的倍数；与 `aspect_ratio` 同传时 `aspect_ratio` 优先。
  **不**发送 OpenAI 的 `size` 字符串作为未知字段。
- `response_format=url`（默认）→ `data.image_urls[]`（24 小时临时地址）；
  `base64` → `data.image_base64[]`。
- `metadata.success_count` / `failed_count` 表达部分失败：保留有效结果并标 partial，**全部失败不得记 passed**。
- 临时 URL **不**作为永久聊天附件。
- **text-to-image 不推导支持 edit**：MiniMax native 本期拒绝编辑。

### 6.4 区域

国内 `api.minimax.cn`、国际 `api.minimax.io`；TTS 另有 `api-bj.minimaxi.com` / `api-uw.minimax.io`。
**国际区仅在用户主动选择已证实预设时才启用**，**不**自动改区。

---

## 7. 错误码映射

| MiniMax 业务码 | 映射                                     |
| --------------- | ---------------------------------------- |
| `1004` / `2049` | `reason=invalid_credentials`              |
| `1008`          | `reason=quota_exhausted`                 |
| `1002`          | `reason=rate_limited`                    |
| `2013`          | `reason=invalid_parameters`（新增）      |
| 其它            | `reason=unknown`（不猜套餐 / 区域 / Key） |

HTTP 200 的业务失败用 `upstream_http_status=200` + 真实业务码表达，
**不**伪造上游 HTTP 401 / 429。

---

## 8. 本期**不**承诺的能力（界面必须明示）

- MiniMax 原生 SSE / WebSocket、实时麦克风识别。
- SRT / VTT 字幕导出、长文本异步合成。
- 音色克隆、音乐 / 视频生成。
- MiniMax 原生图生图或图片编辑。
- 百炼原生语音 / 图片、Gemini / Anthropic / Bedrock / local 的媒体接入。
- Bridge 实时语音链路（独立问题，另行规划）。

## P-003：MiniMax 同步 TTS 载荷

同步 `t2a_v2` 使用顶层 `stream: false` 与 `output_format: "hex"`；
`audio_setting` 保存音频参数（如 `format: "mp3"`）。业务失败先识别，
成功音频才解码。配置与音色查询不会发起推理。
