# 云端开发、串联构建与人工验收

日常只需本地编辑代码并推送对应仓库，也可直接在 GitHub Codespaces 浏览器编辑。三个仓库保持独立；生产服务器不接入这套测试配置。

## 自动流水线

1. 前端 main push（或在 main 手动运行 `frontend`）先完成单元测试、生产构建、Chromium 布局检查，才发布 `frontend-<SHA前12位>` 不可变 Release。分支手动构建仅生成测试 artifact，不发布、不改变下游版本。
2. 后端、客户端 main push 自动运行 `Sync verified frontend and build`；另外每15分钟检查一次前端 main。只有**前端当前 main 的 Release 已成功发布**，才继续。前端构建失败或尚未完成时等待，不能使用旧前端冒充最新版。
3. 同步器检查来源仓库、完整提交 SHA、上游兼容版本、manifest 校验值，更新并提交本仓库 `frontend.lock.json`，然后通过本仓库 `GITHUB_TOKEN` 显式 dispatch `backend.yml` / `android.yml`。下游仍对下载 ZIP、内部文件逐项校验。无需跨仓库 PAT，也不用把签名密钥给前端仓库。
4. 后端和客户端消费同一个已发布前端，彼此并行。后端先发布不可变 `sha-<后端SHA>` 多架构镜像，通过镜像启动及静态资源冒烟后才更新 `latest`。客户端完成签名及包校验后发布 APK。APK 不等待后端镜像，因为它连接的是可配置的服务，二者共享的是前端发布前置条件。
5. 最后人工验收：按需启动 Codespaces 测试服务、浏览器检查，下载 Release APK 做手机验收。测试通过后再自行更新生产服务；此流程不自动替换生产容器和数据库。

GitHub schedule 可能延迟，公开仓库长期无活动也可能停用定时任务。前端成功后可以直接在两个下游仓库 Actions 手动运行 `Sync verified frontend and build`，不用等定时器。公开仓库读取公开前端 Release；如果以后改私有仓库，需要另行配置有读取权限的 GitHub App。

## 查看进度与重试

- 前端：Actions → `frontend`；前端发布失败时先修复或重跑，后端及 APK 会等待。
- 后端/客户端：Actions → `Sync verified frontend and build`；summary 显示源 SHA 及已有下游 run 链接。同步后再查看 `Backend` / `Android APK release`。
- 构建失败：优先在失败 run 点 **Re-run failed jobs**。定时器不会反复重试同一失败构建。
- 同步已提交但 dispatch 失败：重新运行同步工作流，或等定时器自动发现没有下游 run 的提交。
- 有意重新构建同一来源：手动运行同步工作流并选 `rebuild`。Android 已有版本准备提交时必须重跑原 APK run，以保留版本和 versionCode，不另起版本循环。
- 同步 non-force push 因并发提交/分支保护被拒绝时不会覆盖 main；重跑同步。如果 main 禁止机器人直接提交，应调整为允许该工作流提交，或改用受审查 PR 模式，不能绕过保护。
- main 推送后若立即本地继续提交，先 `git pull --rebase`，避免与自动前端锁定和 APK 版本提交冲突。

## GitHub 云端人工调试（Codespaces）

在对应仓库 **Code → Codespaces → Create codespace on main**，使用仓库提供的 `.devcontainer/devcontainer.json`。默认不会启动服务、不会部署生产，也不会自动创建 Codespace。端口保持 GitHub 默认私有。

只验收已发布版本，在终端执行：

```bash
docker compose -f compose.test.yml pull
docker compose -f compose.test.yml up -d --wait
```

打开 **Ports → 8080 → Open in Browser**，注册独立测试管理员。数据保存在当前 Codespace 的 `owui-dev_test-data` 测试卷，和生产完全分离。默认没有模型服务；需要实际聊天时，在测试管理页配置可访问的模型 API，或在 Codespaces secrets 中配置测试凭据。自动测试不代表已经验证了真实模型调用。

复现指定镜像（建议用发布日志中的 digest）时：

```bash
WEBUI_TEST_IMAGE=ghcr.io/mike666wq/open-webui-backend@sha256:<完整digest> docker compose -f compose.test.yml up -d --wait
```

前端仓库内边改边看：

```bash
docker compose -f compose.test.yml up -d --wait
npm run dev -- --host 0.0.0.0 --port 5173
```

打开 **5173** 端口，Vite 将 API / WebSocket 代理给 8080 后端。同一 Codespace 重复运行时无需重复创建数据。修改前端后提交推送，正式流水线才会产出新的前端包、镜像和 APK。

后端仓库内编辑 Python 源码（依赖沿用预构建镜像，不在云端重新安装整套依赖）：

```bash
docker compose -f compose.test.yml -f compose.source.yml up -d --wait
# 修改源码后重启以加载变更
docker compose -f compose.test.yml -f compose.source.yml restart backend
```

新增 Python 依赖时需更新运行时依赖层；源码挂载不会安装新依赖。APK 编译继续交给 Actions。客户端仓库 Codespaces 中也有预构建后端测试入口，可用于核对服务行为。

停止服务：`docker compose -f compose.test.yml down`。保留测试数据；只有明确不需要数据时才加 `-v` 删除测试卷。结束调试后在 GitHub 停止 Codespace；不再使用时删除环境，先提交未保存代码。

私有转发端口需要 GitHub 浏览器登录，APK WebView 不能直接复用该登录。手机端云端服务联调需要另行安排受保护的可达测试地址；本配置不会自动公开端口。可先通过浏览器完成云端验收，再让 APK 连接已有测试服务。不要把生产数据库/上传目录挂到测试环境。

## 无交互自动测试与本地回退

后端 Actions → `Test published image` → Run workflow，输入 `latest`、`sha-<完整SHA>` 或 `sha256:<digest>`；会拉取指定镜像运行隔离冒烟，上传报告。这不发布新镜像、不连接生产，验证镜像前端必须与选定分支的 lock 一致。默认 main + latest 若刚好正在发布发生版本不匹配，应等待发布完成或选匹配提交/镜像再运行。

本地有 Docker 时使用相同 `compose.test.yml` 即可。仅拉取镜像，无需本地编译。默认绑定 `127.0.0.1:8080`；若机器已有同端口服务，修改这份测试 compose 的宿主端口。没有 Docker 的电脑直接用 Codespaces 浏览器即可。

## 额度与首次启用

公开仓库的标准 GitHub-hosted Actions 计算免费，artifact 和其他服务仍有各自限制。Codespaces 是独立额度：个人 Free 每月120核心小时、15 GB-month，Pro 为180核心小时、20 GB-month；2核机器120核心小时约为60运行小时。停止后计算用量停止，存储持续到删除。设置空闲自动停止，并检查 Billing 中的预算和达到预算停止用量设置。

首次合入需三个仓库的 Actions 可运行、后端能写 GHCR、客户端已有两项固定签名 secrets。工作流使用最小任务权限申请 `contents: write` 和 `actions: write`；没有跨仓库写权限。先合入前端，再合入后端和客户端，前端 release 成功后手动运行两次下游同步即可启动首次串联。以后仅推送代码。

参考：[工作流触发](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[Codespaces 费用](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)、[端口访问](https://docs.github.com/en/codespaces/reference/security-in-github-codespaces)。
