# 后端检查与镜像交付

此仓库独立维护后端源码和 Docker 镜像，不同步官方 Git 历史，也不运行官方前端构建、Android 构建或服务器部署。创建工作流文件不会自动发布；只有代码提交并推送到对应 GitHub 分支后才会运行。

## PR 与 main 检查

`Backend / checks` 使用 Python 3.11，仅运行仓库边界／源码语法检查和 `tests` 下的标准库单元测试，无需安装完整机器学习依赖。

```sh
python3 scripts/check_source.py
python3 -m unittest discover -s tests -v
```

PR 只有只读 `contents` 权限，不登录镜像仓库。PR 新提交会取消旧检查；main 的镜像运行排队，不取消已经开始的发布。

## main 镜像发布

main push 的检查通过后，工作流执行以下步骤：

1. 按 `frontend.lock.json` 下载并校验已发布的 DIY 前端，不重新编译前端。
2. 使用 Dockerfile 固定的官方基线镜像，覆盖本仓库后端源码和锁定前端资源，构建 `linux/amd64`、`linux/arm64`；构建层使用 GitHub Actions 缓存。
3. 将镜像推送为 `ghcr.io/mike666wq/open-webui-backend:sha-<完整源码 SHA>`，随后通过不可变的 registry digest 拉取同一镜像的 amd64 版本。
4. 在隔离容器中验收后端健康、基线版本、全部前端文件摘要、首页、聊天深链接、JS/CSS 和缺失资源的 404。成功后才把同一个多架构 digest 推广为 `latest`。

已有的 `sha-<SHA>` 标签不会覆盖。重试运行复用其 digest 并重新验收；如果镜像代码需要修复，提交新源码以生成新 SHA。构建或冒烟检查失败时保留源码标签用于诊断，`latest` 保持原值。`sha-<SHA>` 在冒烟成功前只代表候选镜像，不代表生产验收通过。

源码 SHA 写入 `BUILD_HASH` 和 OCI revision label；前端 SHA 写入 `FRONTEND_COMMIT`。镜像内的 `/app/frontend.lock.json` 和 `/app/build/frontend-manifest.json` 保留完整资源身份。CI 只在镜像任务申请 `packages: write`，使用仓库内置 `GITHUB_TOKEN`，无需生产服务器密钥。

首次发布后，仓库管理员按需要将 GHCR package visibility 设为 public。推送权限和包可见性是 GitHub/GHCR 配置，不由应用代码更改。

## 隔离镜像冒烟

以下命令要求 Docker 已启动，镜像已拉取或本地构建：

```sh
python3 scripts/image_smoke.py --image ghcr.io/mike666wq/open-webui-backend@sha256:<实际摘要>
```

检查使用随机容器名、独立命名数据卷和禁止外网访问的 internal bridge，仅向 `127.0.0.1` 发布随机端口。不挂载现有数据库，不连接生产服务，不注册账号，也不发送业务消息。停止容器后删除其临时数据卷和网络。

报告、后端日志和容器状态保存在 `artifacts/image-smoke/`，不记录容器环境中的临时密钥。GitHub 保存这些证据 14 天；失败不会覆盖 `latest`。当前只做 amd64 运行验收，arm64 经过镜像构建检查，不声称已经完成 arm64 设备运行或生产数据迁移验收。

## 发布边界

镜像推送不等于服务器切换。生产配置、数据库迁移、完整备份、恢复演练与切换仍需独立实施和确认。此脚手架不包含资源回收站业务，也不会清理任何现有用户数据。
