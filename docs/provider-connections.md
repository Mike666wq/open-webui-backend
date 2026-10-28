# 厂商账号与 API 连接

新版连接入口采用“选择厂商 → 认证 → 验证并选择模型 → 保存”。旧的 OpenAI、Ollama 和浏览器直连配置继续保留，不需要迁移已有连接或修改历史聊天。

## 支持范围

- ChatGPT：官方 Sign in with ChatGPT，订阅账号仅供绑定它的用户使用。管理员自己的订阅也不能共享。
- Kimi Code：实验性设备码 OAuth 兼容接入，仅供个人交互式编程使用，始终私有。第三方接入资格仍需由厂商确认，普通聊天会员资格不代表可以使用这条通道。
- OpenAI、DeepSeek、百炼文本 API：API Key 连接。管理员可以将连接共享给指定用户或分组。
- 自定义 OpenAI 兼容服务：可配置地址，选择 Chat Completions 或 Responses 协议。不支持模型列表的服务可手动填写模型 ID。

个人设置中的厂商账号由后端保存，不依赖浏览器直连开关。管理员通过管理面板的连接页面设置共享 API。私有连接、凭据和模型目录按所有者隔离；同名模型也使用不同的稳定内部 ID。

## 首次连接 ChatGPT

1. 打开个人设置 → 连接 → 添加厂商，选择 ChatGPT。
2. 在电脑终端运行界面提供的授权辅助命令，按提示输入一次性配对码。电脑只需要 Python 3，无需运行整个项目。
3. 在打开的官方 OpenAI 页面登录并授权。辅助工具只监听本机 `127.0.0.1` 回调，凭据通过 HTTPS 传给自己的后端。
4. 回到设置页面确认状态，再刷新模型并开始对话。日常使用和令牌刷新由后端完成。

配对码十分钟过期，只能领取一次。辅助工具不会读取 Codex 登录缓存，不需要在聊天里提供密码或 Token。手机/APK 可日常聊天；首期首次账号授权需要在有浏览器的电脑上完成。

断开连接会清除本地令牌，并尝试撤销厂商授权；界面会明确提示远程撤销是否确认。为再次登录保留客户端注册映射。后端必须保留稳定的 `WEBUI_SECRET_KEY` 和数据库，避免重启后丢失解密能力或注册标识。

这条接入路径面向个人开源自托管使用。公开商业托管服务需要另行确认厂商接入资格，不应把个人订阅建立为共享账号池。

## API 验证和模型

验证模型目录不一定代表模型推理额度可用；首次保存后请发送一条测试消息。手动模型模式不伪装成密钥验证成功。密钥被拒绝、服务不可达、模型列表不支持会分别提示；修改密钥、地址或模型范围后会清除旧目录。

百炼的 API Key、区域和工作空间必须匹配。可使用区域预设，也可粘贴控制台提供的地址；模型目录使用厂商原生接口，与 OpenAI 兼容推理路径分别处理。

ChatGPT 仅显示官方目录允许列出的模型；不自动回退到按量计费 API。订阅登录不等于获得 ChatGPT 网页全部功能。ChatGPT OAuth 只走 Responses；不支持生图工具、音频输入、音频转写 API、TTS 或 Realtime。Kimi Code OAuth 同样不声明支持这些媒体 API。

## 联网搜索和外部工具

供应商聊天模型可以使用 Open WebUI 已配置的联网搜索。这里的菜单和模型能力
`web_search` 表示允许使用平台搜索工具，不以厂商目录是否声明原生 `web-search`
为前提。ChatGPT 账号、API Key 和手动填写的兼容模型均遵循这一规则；已有连接
不需要重新登录或重新创建。

管理员仍需启用并配置 Open WebUI 的搜索引擎，用户需有联网搜索权限，并在聊天
中开启搜索。原生函数调用模式下，后端向模型提供 `search_web` 和 `fetch_url`
函数，执行工具后把结果交回同一供应商模型继续生成；兼容模式仍走原有搜索上下文
注入流程。搜索实际使用服务器配置的服务，不会自动切换为厂商网页产品的搜索。
其他工作区、外部工具及代码解释器继续使用各自的配置、权限和执行环境。

## Kimi Code 设备码登录

1. 在个人设置 → 连接 → 添加厂商中选择 Kimi Code，开始设备授权。
2. 打开界面提供的 Kimi 官方验证页，登录自己的账号并确认设备码。手机和 APK 也可以完成这一步，无需本地辅助脚本。
3. 返回 Open WebUI 等待授权完成，再刷新模型目录并发送一条测试消息。

后端加密保存 device code 和访问/刷新令牌。设备码过期或取消后不能再次消费；并发轮询使用数据库租约，晚到的授权结果不能覆盖取消状态。刷新与断开使用数据库锁。客户端身份明确声明 `open_webui`，不会伪装成 Kimi 官方 CLI。断开会清除本地令牌；当前没有使用未经文档确认的远程撤销端点，用户可在 Kimi 设备/账号管理中撤销授权。

本实现参考 [Kimi CLI OAuth 源码](https://github.com/MoonshotAI/kimi-cli/blob/main/src/kimi_cli/auth/oauth.py)、[官方 OAuth 配置](https://github.com/MoonshotAI/kimi-code/blob/main/packages/oauth/src/constants.ts)和[客户端身份约定](https://github.com/MoonshotAI/kimi-code/blob/main/packages/oauth/src/identity.ts)，使用其公开客户端 ID 和 `https://auth.kimi.com/api/oauth/*` 设备授权协议。模型发现和推理走 `https://api.kimi.com/coding/v1`。厂商若拒绝本项目身份，需要单独申请/确认接入资格，不会静默切换到官方客户端身份或收费 API Key。遵循 [Kimi Code 使用准则](https://www.kimi.com/en/help/kimi-code/community-guidelines)，不得共享订阅账号池。

## 统一模型目录、图片与语音

聊天、录音转写、语音合成和生图现在使用同一个服务端模型目录。API Key 只需在
**管理面板 → 连接** 保存一次，然后在图片和语音设置中按能力选择模型；这两个页面
不再重复保存 Key 或 Base URL。

需要区分以下四种状态：

1. **连接成功**：只证明服务端能使用保存的凭据访问配置的地址。
2. **模型被发现**：证明厂商模型列表可以读取，不证明某个能力已经真实调用成功。
3. **能力已声明**：模型被厂商元数据或明确适配标记为聊天、转写、语音合成或生图；
   管理员手动声明也属于这一层，浏览器不会因此假装已经验证。
4. **测试通过**：管理员明确点击“测试模型”并完成一次可能计费的推理。测试结果与
   连接地址、加密凭据、模型和用途绑定；其中任一项改变后会回到“未测试”。

以硅基流动为例：

1. 在 **外部连接** 添加 SiliconFlow，填写 API Key 和 Base URL。
2. 获取模型并将模型分类为聊天、转写、语音合成或生图；无法识别的模型进入
   “待分类”，不会自动出现在聊天选择器。
3. 在 **语音** 中分别选择默认转写、语音合成模型和音色；在 **图片** 中选择默认
   生图模型。聊天选择器只读取带聊天用途的模型。
4. 录音页会预选管理员设置的转写模型，也允许切换本次任务使用的模型。上传后任务
   固定提交时的稳定模型引用；排队期间修改全局默认值不会换模型。
5. 保存配置不会自动调用模型。需要真实验证时，管理员主动点击“测试模型”；失败后
   系统不会自动重试可能已经计费的请求。

模型引用采用“连接 ID + 上游模型 ID”的稳定格式。服务端在每次聊天或媒体调用时都会
重新检查模型用途、适配器、连接启用状态和当前用户权限，不能只依赖前端筛选。私有
连接不会因为被设为默认值而自动共享；共享后也始终只返回连接名称，不返回密钥。

硅基流动适配使用其原生差异：

- 转写走 multipart `/audio/transcriptions`，只发送 `model` 和文件。当前官方接口
  不提供本项目可依赖的 `language`、`response_format`、时间戳或说话人参数，因此
  不能仅修改模型名就声称支持这些能力。
- 语音合成走 `/audio/speech`。系统预置音色必须使用
  `模型名:音色` 格式，例如 `FunAudioLLM/CosyVoice2-0.5B:alex`；当前适配提供
  Alex、Benjamin、Charles、David、Anna、Bella、Claire、Diana 八种预置音色。
- 生图走 `/images/generations`，请求使用 `image_size`，响应从 `images` 读取，而
  非 OpenAI 风格的分页结构。其他 OpenAI 兼容厂商仍使用各自适配器。

接口分别为 `/images/generations`、`/images/edits`、`/audio/speech` 和
multipart `/audio/transcriptions`。图片和音频传输使用连接层 DNS 检查并禁止跟随
重定向；媒体 API Key 不发给浏览器，也不写入生成文件元数据。

旧版 OpenAI 媒体配置不会被静默改写。管理员可先查看迁移预览；确认迁移后才创建
私有 API Key 连接并切换默认模型。迁移重复执行是幂等的，原配置在确认前保持可用。

ChatGPT 套餐登录仍只覆盖其官方允许的文本/多模态 Responses 请求；Kimi Code 登录
同样不会自动获得生图、转写、TTS 或 Realtime。Realtime 通话继续使用原有独立
OpenAI/Azure API Key 设置。[ChatGPT OAuth 限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)、
[OpenAI 图片 API](https://developers.openai.com/api/docs/guides/image-generation)、
[OpenAI 音频 API](https://developers.openai.com/api/docs/guides/audio)。
硅基流动的语音、转写和生图接口分别见
[语音合成](https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech)、
[语音转写](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post) 和
[图片生成](https://docs.siliconflow.cn/docs/userguide/capabilities/images)。

## 按服务接入：聊天、转写、合成、生图分开配置（P-002）

每个连接按四种用途分别配置：**聊天 / 语音转写 / 语音合成 / 生图**。后端对每个模板返回各服务的支持情况、鉴权限制、发现策略（远程列表 / 官方预设 / 手工）、参数与限制；服务是否可用由「模板支持 × 认证方式 × 适配器 × 模型声明」共同决定。OAuth 订阅连接（ChatGPT、Kimi Code）明确声明媒体服务不可用，订阅登录不会自动获得转写 / 合成 / 生图权限。

服务配置保存在连接的 `config.services` JSON 中，不新增数据库表：

- 省略 `services` 的旧连接原样读取，原 `base_url`、`api_type` 与模型用途不变。
- 存在 `services` 时表示「只启用列出的用途」；每项可设 `enabled`、`adapter`、`endpoint`、`options`，未知字段或不匹配的适配器 / 鉴权组合在出站前拒绝。
- 地址支持两种模式：`base_url` 是含版本前缀的 API 根，适配器只追加自己登记的相对路径一次；`endpoint_url` 是完整请求地址，原样使用一次。把完整操作地址误填为 `base_url` 时界面会说明两种模式，后端不默默生成重复后缀。服务详情展示脱敏后的最终 URL 预览。

### MiniMax 原生媒体

MiniMax 的转写 / 合成 / 文生图使用厂商原生接口，默认根地址 `https://api.minimax.cn/v1`，无需先获取远程模型列表即可添加与保存：

- **语音转写** `POST /v1/speech_to_text`：multipart 必带 `model=asr-1.0`（预设+默认模型，仍必须发送，不是 model-less）；`language` 走请求头，未指定时省略（混合识别）；输入不超过 50 MiB 且 500 秒。返回 json / verbose_json；segments 的 start/end/speaker 转入录音结果结构。
- **语音合成** `POST /v1/t2a_v2`：同步请求，返回 hex 音频解码为 MP3 后进入现有播放链路；`base_resp.status_code` 非零先转错误，HTTP 200 的业务失败不当成功。音色通过 `POST /v1/get_voice` 查询 system 类别，也支持手工 voice ID；不使用其他厂商的预设音色冒充。
- **文生图** `POST /v1/image_generation`：`model` 选 `image-01` 等已登记预设，`prompt` 必填，尺寸映射为该模型支持的宽高；结果从 `image_urls`（受控下载）或 `image_base64` 解析，部分失败保留有效结果并标明失败数。**生图可用不等于编辑可用**：服务声明带 `operations` 元数据（`openai-image` 为 `['edit','generate']`，MiniMax / SiliconFlow 生图仅 `['generate']`），前端据此决定是否展示编辑入口，后端在出站前按实际生效适配器再次判定。
- 业务码映射：1004/2049→凭据无效，1008→额度不足，1002→限流，2013→参数错误；未知码保持 unknown，不猜测原因。错误对象可带 `upstream_code`、`upstream_http_status`（HTTP 200 业务失败时为 200）、`trace_id` 与 `purpose`。
- 录音转写分片目标 **480 秒**（500 秒上限留余量），任务快照保存实际 `chunk_seconds` 与参数指纹；旧任务缺该字段按 600 秒解释，分片几何不兼容时明确重开，不自动重放可能已计费的片段。

接口返回的可选用途与限制以后端为权威，前端不维护另一份「协议都支持媒体」的清单。模型测试与日常调用使用同一套适配器、地址解析、输入约束与响应规范；转写测试可上传音频样本（≤2MiB 显式测试上限）、合成测试可选音色、图片测试可设支持尺寸，点击测试前会展示实际服务、模型与可能计费。

## 失败时怎么恢复

连接失败会按**真实阶段**与**真实原因**显示，而不是笼统的「失败」：

| 现象                                   | 阶段            | 原因                       | 界面给出的动作 |
| -------------------------------------- | --------------- | -------------------------- | -------------- |
| 提示某个域名解析到 `198.18.x.x` 被拦截 | 获取模型列表     | `address_blocked`          | 检查服务器网络 |
| 登录状态失效                           | 刷新登录状态     | `token_expired`            | 重新登录       |
| 账号缺少权限                           | 获取模型列表     | `insufficient_permissions` | 检查账号权限   |
| Key 被拒                               | 获取模型列表     | `invalid_credentials`      | 更换 API Key   |
| 限流 / 额度不足                        | 调用模型         | `rate_limited` / `quota_exhausted` | 稍后重试 |

要点：

- **目录失败不等于推理不可用**。厂商可以拒绝模型列表却仍能正常推理；真实请求仍按凭据鉴权。
- **403 不等于 Key 填错**，它表示账号缺权限，换 Key 解决不了。
- 重新登录**沿用原连接**，不会新建第二个连接；取消或失败不会清空原凭据。
- 换 Key **不会**清空白名单、改名、用途、启停状态与聊天里的模型引用。

网络侧（fake-ip / 保留网段）的取证步骤、两条恢复路线与回滚方式见 [恢复手册](PROVIDER_RECOVERY.md)。

## 验证与发布

后端 CI 的基础测试保持轻量；运行时测试使用临时数据库和合成凭据。集成测试通过临时回环服务检查真实 HTTP 转发、流式响应、私有隔离、共享权限撤销和 OAuth 配对重放，不调用收费厂商。

```sh
python -m unittest discover -s tests -v
python scripts/test_provider_runtime.py
python scripts/test_provider_chat_integration.py
```

后两项使用项目后端依赖，可在现有 GitHub Actions 运行时容器内执行。真实 ChatGPT 授权和厂商 Key 的最终人工验证仍需账号所有者参与；自动测试通过不表示已验证真实套餐资格。

开发与发布仍只有 frontend/backend 两个仓库：前端发布验证后的资源与 APK，后端再更新前端锁定版本、验证并构建镜像。本改造不需要恢复 client 仓库。前端通过后端配置中的 `enable_provider_connections` 能力标记显示新入口，因此先更新前端、稍后更新后端时仍保持旧页面可用。

协议细节见 [ChatGPT 授权说明](chatgpt-siwc-provider.md)。

### Administrator model switches

Saved model switches apply to stable provider model IDs as well as legacy models. Disabled provider chat models are omitted from each user's chat catalog and rejected again during request resolution, including calls from existing conversations. Enabling a model does not expand the provider connection's access permissions. Discovery caches do not cache this switch.

### P-003 语音使用与独立 Key

先保存服务地址，再显式选择并启用官方预设模型，最后进入录音页或管理员音频设置。
MiniMax 转写使用 `asr-1.0`，合成使用相应 speech 模型；聊天模型不能当作语音模型。
如聊天和语音需要不同 Key，使用“新建此语音连接”，新连接默认私有，只配置所选语音用途；
不覆盖聊天连接的 Key。音色可显式获取系统列表，也可手填 voice ID。真实套餐和密钥权限
仍须账号所有者另行验证，保存配置或查询音色不会触发付费推理。
