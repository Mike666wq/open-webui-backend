# GitHub 构建、串联发布与人工验收

当前共同进度见 [PROJECT_STATUS.md](PROJECT_STATUS.md)。本地资源有限时，只保留编辑器和 Git；完整构建与自动测试在 Actions 执行，不要求本地同时运行三个项目。

## 自动串联机制

1. 前端 main push 或 main 手动运行 `frontend`：安装锁定依赖、运行打包与上游单元测试、生产构建、移动 Chromium 检查，全部成功后发布 `frontend-<SHA前12位>` ZIP、manifest 和 SHA256SUMS。分支手动运行不发布正式前端。
2. 后端和客户端 main push、每小时第 7/22/37/52 分钟、手动 `Sync verified frontend and build`：检查前端当前 main 的 Release。未发布时等待；不能用旧前端冒充最新版。
3. 同步器核对仓库、完整 SHA、上游兼容基线与 manifest 校验，提交本仓库 `frontend.lock.json`，使用本仓库 GITHUB_TOKEN 显式 dispatch 后端或 APK 工作流。源码确认使用 Git 远端引用，避免刚推送后 commits API 短暂旧读的问题；无需跨仓库写入 PAT。
4. 后端和 APK 在同一个前端发布之后并行构建。后端通过不可变 digest 验收后才更新 latest；APK 通过签名和产物校验后才公开 Release。APK 不以“后端镜像先完成”为前置条件。
5. PR 用于检查和审查配置/代码变更；运行时的连接由 Actions、前端发布包和版本锁完成，不需要每次人工创建三个 PR。

GitHub 定时任务可能延迟或因公开仓库长期无活动被停用。前端成功后可手动运行两个下游的同步入口，无需等定时器。当前同步器没有普遍排除文档修改，不能承诺“文档 push 绝不构建/发版”。工作流使用公开前端资源；改私有仓库时需另行配置读取权限。

## 查看结果、去重与重试

- 先看前端 `frontend` 是否已发布，再看下游 `Sync verified frontend and build`，最后看 `Backend` / `Android APK release`。同步成功不等于构建成功。
- 同一提交已有 dispatch run 时，定时同步不反复构建；失败优先在原 run 选择 Re-run failed jobs。
- 同步锁提交后 dispatch 失败，重新运行同步任务或等定时器补偿。
- 手动同步的 `rebuild` 可有意重新派发。Android 版本准备提交带 run 标记时，应重跑该 APK 原 run，以复用版本和 versionCode。
- 同步使用 non-force push；遇到并发提交或分支保护拒绝时不会覆盖 main。检查新 head 后重试，不绕过保护。
- 自动锁更新和 APK 版本提交会推进 main；本地继续开发前先检查工作区，再同步远端，避免用旧锁覆盖新版本。不同工作区的未提交内容不能直接当作 main 发布事实。

## 推荐人工验收：独立机器运行已构建镜像

需要 Docker 和 Compose 已可用。进入后端仓库，在独立测试机器运行：

```bash
docker compose -f compose.test.yml pull
docker compose -f compose.test.yml up -d --wait
```

访问该机器的 127.0.0.1:8080，或通过受保护的远程连接访问。compose 默认只绑定回环，不自动公开测试服务。镜像已包含前端，不需要再启动前端仓库。首次创建独立测试账号；测试数据使用 `owui-dev_test-data` 卷。真实聊天需另外配置可达的模型 API，自动健康检查不证明模型调用成功。

复现指定版本时，用已验证的不可变 digest：

```bash
WEBUI_TEST_IMAGE=ghcr.io/mike666wq/open-webui-backend@sha256:<完整digest> docker compose -f compose.test.yml up -d --wait
```

停止测试容器用 `docker compose -f compose.test.yml down`，保留测试卷；仅在确认不要测试数据时加 `-v`。生产机器若承载测试，必须另有端口、数据库、文件目录和可用资源；当前项目不自动部署到生产。

## 可选源码调试

有能运行开发进程的机器时，前端仓库可连接测试后端后运行：

```bash
docker compose -f compose.test.yml up -d --wait
npm run dev -- --host 0.0.0.0 --port 5173
```

5173 是当前前端源码热更新，8080 是已构建镜像内的网站。前端 Vite 代理 API/WebSocket 到 8080。已有远程测试后端时也可设置 WEBUI_BACKEND_URL，不要求本地启动后端。

后端仓库可沿用镜像依赖，仅挂载源码：

```bash
docker compose -f compose.test.yml -f compose.source.yml up -d --wait
docker compose -f compose.test.yml -f compose.source.yml restart backend
```

新增 Python 依赖仍需更新运行时依赖层；源码挂载不会安装新依赖。APK 编译仍交给 Actions。

## GitHub 手动自动测试入口

后端 Actions → `Test published image` → Run workflow，选择匹配当前分支 lock 的 `latest`、`sha-<完整SHA>` 或 `sha256:<digest>`。它只拉取镜像运行隔离冒烟并上传报告，不发布、不部署生产。若 latest 正在切换而 lock 已更新，应等对应发布完成或选匹配版本。

## Codespaces：暂停使用，未验收可用

三个仓库保留 .devcontainer 配置，但实际创建曾因通用镜像占满磁盘而进入恢复模式；改轻量 Python 镜像后又遇到预装 Yarn apt 源签名错误，Docker 安装未完成。后续移除软件源的修复没有完成验证，不能把配置存在或 Actions 镜像测试通过称为 Codespaces 启动通过。

当前默认流程不需要 Codespaces，也不自动创建、重建、删除或修复已有云端环境。恢复这项工作时，需先验证 docker version、docker compose version、镜像启动和私有端口访问，才能重新标记可用。私有转发端口的 GitHub 浏览器登录不能直接被 APK WebView 复用。

## 额度与账单

个人 Free 包含每月 120 核心小时和 15 GB-month 存储用量，和机器实际磁盘容量不同。运行中的环境消耗计算用量；停止后仍累计保留存储；删除后停止继续累计，不退回已用量。关闭浏览器不等于停止。

本账号已在 [Billing Overview](https://github.com/settings/billing) → Current included usage 旁 More details 确认到 120 included Codespaces core hours、15 GB included Codespaces storage。该弹层显示额度和已应用抵扣，不是实时剩余小时；Usage 页尚未记录 Codespaces 时不能推断没有额度或没有消耗。

参考：[工作流触发](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[Codespaces 计量](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)、[停止环境](https://docs.github.com/en/codespaces/developing-in-a-codespace/stopping-and-starting-a-codespace)。
