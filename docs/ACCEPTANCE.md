# 当前验收清单

核对日期：2026-10-28（对应规划件 P-001 r2 与 P-002）。共同进度见 [PROJECT_STATUS.md](PROJECT_STATUS.md)。
勾选只代表**该项的证据已经存在**；自动检查通过**不等于**人工验收完成，也不等于生产根因已定位。

## 已验证的自动交付

- [x] 五类 `category` 与既有字段（`status` / `http_status` / `upstream_status` / `retryable` / `at` / `hint`）保持不变，新增三个可选字段 `stage` / `reason` / `suggested_action`；旧客户端与旧错误对象照常显示。
  证据：`backend/runtime_tests/test_provider_http.py`（各状态的原因与动作、403 不要求换 Key、`198.18.0.28` 属本地拦截且 `upstream_status` 为空、DNS/TLS/超时各自原因、OAuth `invalid_grant`/429/503/网络不可达的分类）。
- [x] 目录鉴权错误**不再**把整条连接的模型全部下线；白名单里已启用且允许访问的模型仍可见，真实请求仍执行凭据鉴权（上游 401 照常报出），禁用模型仍被拒，撤销共享与非所有者仍被拒。
  证据：`backend/runtime_tests/test_provider_api_v2.py`。
- [x] 获取目录失败不删除白名单、不改改名 / 用途 / 启停 / 稳定 ID，不影响聊天引用。
  证据：`backend/runtime_tests/test_provider_api_v2.py`。
- [x] ChatGPT 重新登录更新**原连接**而不是新建连接；成功后旧目录错误失效；取消、失败与迟到响应不清空原凭据。
  证据：`backend/runtime_tests/test_chatgpt_siwc.py`。
- [x] 厂商类错误的 HTTP 响应带 `error` 分类对象；参数校验、权限校验等**不**被包装成厂商错误。
  证据：`backend/runtime_tests/test_provider_api_v2.py`（连接校验、临时连接校验、校验类 400）。
- [x] 显式放行 `198.18.0.0/15` 时匹配地址可进入连接流程，回环 / 其它私网 / 元数据地址仍被拒，TLS 校验与禁止重定向不变。
  证据：`backend/runtime_tests/test_provider_http.py`。

## P-002 供应商按服务接入（2026-10-28，规划件 P-002）

以下勾选的证据来自本地 Python 3.11 `.venv` 预检（`scripts/test_provider_runtime.py` 221 项、`unittest discover -s tests` 55 项、`test_provider_chat_integration.py`、`check_source.py` 全部通过）；该分支的 GitHub Actions run 尚未产生，合并与发布未做。

### 已验证的自动交付

- [x] 模板身份：临时校验与正式保存共享同一解析契约，选中的 MiniMax / 百炼 / DeepSeek 模板不再被缺省 `custom` 覆盖。
      证据：`backend/runtime_tests/test_provider_services.py`、`test_provider_connections.py`。
- [x] 服务声明：`GET /providers` 返回各模板按 chat/transcription/speech/image 的支持情况、鉴权限制、发现策略、`model_requirement`、preset 与限制；OAuth 模板的媒体服务声明不可用。
      证据：`backend/runtime_tests/test_provider_services.py`。
- [x] 无列表流程：MiniMax ASR/TTS/image 不请求 `/models` 即可保存；`asr-1.0` 明确选定后进入转写白名单并保持未测试；`model_requirement='defaulted'` 仍必发 `model=asr-1.0`，不是 model-less。
      证据：`backend/runtime_tests/test_provider_services.py`、`test_provider_minimax.py`。
- [x] 用途保存：`model_selections` 携带 display_name/uses/source，与旧 `config.model_ids` 并存但同时不一致时拒绝；手工用途保存为 manual+untested，刷新后不降级为 chat。
      证据：`backend/runtime_tests/test_provider_services.py`。
- [x] 地址规则：`base_url` 只追加一次登记相对路径、`endpoint_url` 原样使用一次；完整 ASR 地址恰好调用一次；错误地址在出站前给出可理解提示。
      证据：`backend/runtime_tests/test_provider_minimax.py`、`test_provider_http.py`。
- [x] ASR 契约与限制：multipart 必带 `model=asr-1.0`，language 走请求头；json/verbose_json 规范化；50MiB/500 秒上限；录音分片 480 秒并保存 `chunk_seconds` 快照，旧任务按 600 秒解释，检查点不兼容时明确重开。
      证据：`backend/runtime_tests/test_provider_minimax.py`、`test_recordings.py`。
- [x] TTS 契约：`t2a_v2` 携带 model/text/voice_setting/audio_setting，hex 解码校验后才入音频链路；`base_resp` 非零转错误，HTTP 200 业务失败不当成功（`upstream_http_status=200` + 业务码）；业务码 1004/2049/1008/1002/2013 分类映射。
      证据：`backend/runtime_tests/test_provider_minimax.py`。
- [x] 生图契约：文生图映射 model/prompt/尺寸/n，解析 `image_urls`/`image_base64`，partial 失败数保留；编辑操作（operation=edit）在出站前按实际生效适配器拒绝。
      证据：`backend/runtime_tests/test_provider_minimax.py`、`test_provider_services.py`（operations 声明）、`routers/images.py` 编辑分支。
- [x] 共用调用语义与权限回归：显式测试与日常调用走同一适配器/地址/约束/响应规范；禁用连接与模型在媒体下拉隐藏且调用时拒绝；共享撤销、缓存命中重新校验、SSRF/TLS 防线不放松。
      证据：`backend/runtime_tests/test_provider_media.py`、`test_provider_http.py`、`test_provider_api_v2.py`。

### 尚未完成的验收（P-002）

- [ ] 本分支 GitHub Actions 全绿（后端检查 + runtime 测试），PR 开启与合并。
- [ ] 真实 MiniMax 账号：短音频 ASR、超过 500 秒录音分片、TTS 播放、文生图保存——需要账号所有者触发，逐服务单独判定。
- [ ] 浏览器人工验收（服务选择、无列表保存、音色/文件测试对话框、390×844 等视口）与 Android 真机。
- [ ] 待取证：MiniMax ASR 语言请求头名（现用 `Language`，集中常量可改）；音色 `output_format=url` 字段名；国际区 `image-01-live` 枚举；生产镜像内 ffmpeg 可用性。
- [ ] P-001 遗留：真实 ChatGPT 授权、真实厂商凭据下的推理（见下节）。

## 尚未完成的验收（P-001 r2）

- [ ] 真实 ChatGPT 授权、真实厂商凭据下的推理、额度与套餐验证；需要账号所有者与部署责任人。
      （2026-10-28 取证：ChatGPT 连接的快照错误是 `stage=oauth_refresh` / `reason=token_expired` /
      `upstream_status=400`，即**登录态真的过期**，必须重新登录，与网络无关。）
- [ ] 放行网段后重新获取模型列表与一次真实模型调用是否成功；放行只解除本地拦截，不保证代理接管、认证或调用成功。
      （2026-10-28 取证：生产环境的 `address_blocked` 根因是代理 TUN 的 `dns-hijack: any:53`
      把本机所有 DNS 劫持成 fake-ip（`198.18.0.0/16`）；改成 `redir-host` 后容器内解析回到真实地址，
      应用自身出站代码路径已能连到厂商（返回 401 而非超时），SiliconFlow 目录刷新**成功拉到 98 个模型**，
      快照错误已清空。**真实模型调用仍未触发**，且 OpenAI / MiniMax 还需按节点可用性单独分流。）
- [ ] 迁移到生产后的真实数据搬迁与旧连接发现；旧连接是否仍然存在于生产数据中，未核实。
- [ ] Android 真机上的同一套恢复入口；本轮改动未涉及原生代码，也未做真机验证。

## 不作为验收依据的东西

- 宿主机连通性**不代表**容器连通性；目录拉取成功**不代表**推理成功。
- CI 使用合成凭据与模拟厂商，**不访问**真实厂商，也不产生任何付费推理。
- 未开启全局 `trust_env`，未默认放行 fake-ip 或私网，未关闭证书校验或重定向防线。

## P-003 验收状态（2026-10-28）

- [ ] 独立评审逐条核对 P-003 的权限、删除及 OAuth 失效场景、取消/断流与配置竞争。
- [ ] 两仓 PR 的 CI 全绿，并核对同一前端发布版本；本地模拟不代表 CI 或发布完成。
- [ ] 账号所有者在真实厂商套餐下验证 ASR、音色和 TTS 播放；人工浏览器及 Android 真机验收。
- [ ] 用户报告的生产删除故障：核对请求与数据库读回，未授权不得删除真实连接。本轮未复现。

自动回归仅使用模拟凭据和临时数据；真实账户、生产变更、人工验收分别记录。
