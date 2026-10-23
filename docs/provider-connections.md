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

## 生图、语音合成与转写

可以复用已保存在服务器上的 OpenAI 或兼容 API Key，无需在各媒体设置中重复粘贴密钥：

1. 在管理面板 → 连接中创建 API Key 连接；如需给其他用户使用，配置相应用户/分组共享权限。
2. 在管理面板 → 图片中，将生成或编辑引擎改为 **已保存的厂商连接**，选择连接，并填写该厂商支持的图片模型 ID。
3. 在管理面板 → 音频中，将转写或语音合成引擎改为 **已保存的厂商连接**，选择连接，填写模型 ID；语音合成还需选择音色。
4. 保存后生成一张图片、合成一句语音或上传一小段音频验证真实模型权限。

接口分别为 `/images/generations`、`/images/edits`、`/audio/speech` 和 multipart `/audio/transcriptions`。模型/音色的实际支持取决于服务商；本实现不会根据同名聊天模型推断它具备媒体能力。媒体默认模型由管理员配置，个人设置当前不提供单独的媒体默认值。

每次调用均检查连接权限，包括语音缓存命中。选中管理员私有连接不会使其自动变成全站共享连接；未获授权的用户会被拒绝。媒体 API Key 不发给浏览器，不写入生成文件元数据，连接失效后不会回退使用另一把全局密钥。图片和音频传输使用连接层 DNS 检查并禁止跟随重定向。

Realtime 通话继续使用原有独立 OpenAI/Azure API Key 设置，本轮未改成保存连接模式。ChatGPT/Kimi 套餐令牌不会用于 Realtime。[ChatGPT OAuth 限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)、[OpenAI 图片 API](https://developers.openai.com/api/docs/guides/image-generation)、[OpenAI 音频 API](https://developers.openai.com/api/docs/guides/audio)。

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
