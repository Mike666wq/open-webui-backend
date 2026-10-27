# 当前项目状态

更新：2026-10-24（Asia/Shanghai）。运行事实以 Actions、Release 和锁文件为准。

目前收敛为两个开发仓库：

| 仓库 | 职责 |
|---|---|
| [frontend](https://github.com/Mike666wq/open-webui-frontend) | 网页源码、`apps/client/` Tauri/Rust/Android 源码、前端产物和签名 APK |
| [backend](https://github.com/Mike666wq/open-webui-backend) | 后端、接口测试、消费已发布前端、构建和验证多架构镜像 |

旧 client 仓库保留历史与 v0.2.32 及更早的 Releases；自动前端同步已退役，迁移后不再向那里提交新客户端功能。
导入客户端源码基线：`621b666b674da5118ae2b1e28e19efce0eef6ba4`（v0.2.32）。网页官方兼容基线仍为 Open WebUI 0.12.2。

网页变化：检查和生产构建 → 发布不可变前端 ZIP → 同一流水线打包 Android；后端同步后并行构建镜像。
客户端原生变化：仅客户端检查和 APK；后端变化：后端检查和镜像；文档/开发容器变化不发布产品。
后端选择 main 历史中最新已成功发布的前端产品，不把当前 main 文档提交当作尚未发布的网页版本，也不把 APK Release 当作前端 ZIP。
签名必须使用原密钥，禁止为了迁移重新生成。新 APK versionCode 使用 `200000 + frontend workflow run_number`，高于旧版 100026。

迁移代码存在不等于新流水线验收通过；请检查 frontend Actions 的 Android 签名、原生测试和 APK 完整性门禁及对应 Release。
真机覆盖安装、HyperOS、SAF provider、多设备和真实模型交互仍需人工验收。Codespaces 暂停使用，不是自动构建的依赖。
生产环境不会随这次代码迁移自动部署。

实时入口：

- [前端和 APK 流水线](https://github.com/Mike666wq/open-webui-frontend/actions/workflows/frontend.yml)
- [前端和 APK Releases](https://github.com/Mike666wq/open-webui-frontend/releases)
- [客户端前端锁](https://github.com/Mike666wq/open-webui-frontend/blob/main/apps/client/frontend.lock.json)
- [后端流水线](https://github.com/Mike666wq/open-webui-backend/actions/workflows/backend.yml)
- [后端前端锁](https://github.com/Mike666wq/open-webui-backend/blob/main/frontend.lock.json)

## 本次迁移验收记录

- 原签名迁移：[run 39759453405，第 3 次](https://github.com/Mike666wq/open-webui-client/actions/runs/39759453405/attempts/3) 成功；一次性入口已删除。
- frontend 独立原生路径：[run 39760517628](https://github.com/Mike666wq/open-webui-frontend/actions/runs/39760517628) 成功，发布 v0.2.33/versionCode 200031。
- 网页构建后复用同次产物打包：[run 39761599593](https://github.com/Mike666wq/open-webui-frontend/actions/runs/39761599593)，实时结果以该入口为准。
- 后端消费同一新版网页：[run 39762613694](https://github.com/Mike666wq/open-webui-backend/actions/runs/39762613694)，实时结果以该入口为准。
- 对齐的网页验收来源为 b1b7f63debb2032bd10cf86cbf9abf5db1ae0d6d；这是本次迁移的记录，不是永久的“最新版本”常量。
- 临时迁移 Secret 可删除；专用 Token 可撤销，原签名配置继续保留。生产未自动部署，真机覆盖安装仍需人工验收。

## 录音文档与布局修复（2026-10-24）

笔记页录音文档功能与后端持久化任务已实现；复用服务端 ASR 配置，模型由管理员后续选择。原文、整理稿及知识库快照分开保存，原音频保持私有。前端修复消息与输入框中心不一致。详见 [功能说明](recording-documents.md)。

自动化使用模拟 ASR，不能视为真实模型准确率验收；发布结果以当前 Actions、Release 和前端锁为准。

## 供应商与模型管理（2026-10-26）

外部连接改为供应商列表加详情布局，详情分为连接设置、模型、调用记录；内置硅基流动、OpenAI、DeepSeek、百炼、New API 与 ChatGPT 入口。服务端保存目录并按 15 分钟有效期合并刷新，模型启停同时作用于管理页与聊天选择器，旧版 OpenAI 连接通过预览与确认迁移，调用记录按用途汇总真实用量。详见 [功能说明](provider-management.md)。

本轮补齐「标题、标签、后续建议、搜索查询分别选择辅助模型」：默认模型对全部辅助任务生效，单独指定只覆盖该用途，留空沿用默认；默认与单独指定都为空时该用途不自动生成，也不回退到聊天模型。配置键为 `task.model.<purpose>.default/external`，`<purpose>` 取 `title`、`tags`、`followup`、`query`。

自动化覆盖目录失败保留、过期目录、并发刷新、权限撤销、禁用模型、人工配置保留、迁移回滚与幂等、旧聊天引用，以及辅助模型的按用途选择与不回退。CI 使用模拟供应商，不访问真实付费模型；真实厂商调用仍需用户在模型详情显式触发，不能视为已验收。

## 供应商体系改版（2026-10-27）

按厂商接入协议重做「供应商与模型管理」。厂商列表降级为候选池，用户勾选的模型才进入白名单，只有白名单里的模型出现在聊天选择器；厂商级启用开关取代逐模型关闭；旧 OpenAI / Ollama 服务端卡片下线并全部并入新供应商页，浏览器直连保留为独立高级来源、不迁移。新增 Anthropic、Gemini、Azure OpenAI、Bedrock 与本地推理模板但标记为未实装，添加时明确拒绝，不按 OpenAI 协议误调。

本轮同时把出站请求的错误归因收敛为五类（连接、厂商网络、鉴权、限流、上游），后端给出可展示的中文提示，前端按分类展示。契约见 [接口契约](PROVIDER_CONTRACT.md)，功能说明见 [功能说明](provider-management.md)。

合并顺序：前端 PR #30 先合，后端 PR #18 再合（`GET /providers` 返回结构由裸数组变为 `{object,data,groups}`，前端已兼容两种形状）。

自动化覆盖模板目录、白名单增删改查、厂商级开关、迁移幂等与双读兜底、五类错误分类，以及旧连接搬迁。CI 使用合成凭据与模拟厂商，不访问真实付费模型。浏览器端真实交互（添加供应商 → 拉取模型 → 勾选 → 保存 → 聊天选择器出现）与真实厂商凭据下的连通性检查**尚未人工验收**；迁移到生产环境后的真实数据搬迁**尚未验收**。
