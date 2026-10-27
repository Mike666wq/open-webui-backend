# 当前验收清单

核对日期：2026-10-27（对应规划件 P-001 r2）。共同进度见 [PROJECT_STATUS.md](PROJECT_STATUS.md)。
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

## 尚未完成的验收

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
