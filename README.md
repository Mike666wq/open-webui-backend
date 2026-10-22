> 云端开发和前端 → 后端镜像 / APK 串联构建，见 [云端开发说明](docs/CLOUD_DEVELOPMENT.md)。

# Open WebUI Backend

这是 `Mike666wq` 自行维护的 Open WebUI 后端开发仓库。官方源码先以固定版本快照导入本地，在这里开发、验证后再提交；没有上游 GitHub 自动同步或官方发布工作流。

当前基线是 Open WebUI **0.12.2**、提交 `6a18585ff4c2e5f6805f682222cfbc46dc669ef8`。来源、归档摘要和镜像摘要记录在 [backend.upstream.json](backend.upstream.json)。官方许可、品牌资产和许可历史完整保留。

## 三个项目的职责

- `open-webui-frontend`：Svelte/Vite DIY 网页及已校验的静态资源发布。
- `open-webui-backend`：Python API、资源管理后续开发、数据库迁移、后端镜像。
- `open-webui-client`：Android 原生外壳、APK 签名与发布。

本仓库不重复维护 Svelte 组件。根 `package.json` 和 `CHANGELOG.md` 是官方后端读取版本所需的元数据，不用它们运行 `npm install`。`pyproject.toml`、`uv.lock`、`hatch_build.py` 保留用于依赖和来源审计；本仓库不使用官方 wheel 构建流程。

## 本地开发

使用 **Python 3.11 或 3.12**，建议 3.11。首次准备依赖：

```sh
cd /Users/bbben/Desktop/open-webui-client/open-webui-backend
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
```

日常开发分别启动 API 与现有前端，不必反复构建镜像：

```sh
# 后端；仅监听本机 8081，自动重载 backend/open_webui
.venv/bin/python scripts/dev_backend.py

# 另一个终端：真实 DIY 前端，连接这次开发的后端
cd /Users/bbben/Desktop/open-webui-client/open-webui-frontend
WEBUI_BACKEND_URL=http://127.0.0.1:8081 npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

开发启动器固定使用 `.local/dev/` 下的新数据库、文件和密钥；不会继承生产数据库、Redis、S3 或模型服务配置。首次测试账号只在这个隔离环境创建。先用以下命令查看非敏感配置：

```sh
python3 scripts/dev_backend.py --print-config
```

## 检查与镜像

```sh
python3 scripts/check_source.py
python3 -m unittest discover -s tests -v
python3 scripts/prepare_frontend.py
docker build -t open-webui-backend:dev .
```

前端必须从本仓库 `frontend.lock.json` 对应的发布包准备，核对 ZIP、manifest、完整提交和逐文件摘要。输出在 `.build/frontend/`；已有其他提交的本地 `dist` 不能直接替代。需要离线使用时传入 `--archive /path/to/open-webui-frontend.zip`。

镜像基于固定摘要的同版官方标准 CPU 镜像，继承其依赖后完整替换 Python 源码和 DIY 前端。若修改官方 requirements，需要相应调整镜像安装层；来源检查会阻止只改依赖声明而复用旧运行环境。生产切换还需核对实际使用的 CPU/CUDA/Ollama/slim 变体。

仓库只配置自己的后端检查与 GHCR 镜像发布，不构建 APK，不重复构建 Svelte，也不自动部署生产。流程见 [CI.md](docs/CI.md)。

## 当前阶段与数据边界

当前提交准备的是后端开发与镜像基础，不代表七天回收站、资源统计或自动清理已经完成。后续按 [RESOURCE_MANAGEMENT.md](docs/RESOURCE_MANAGEMENT.md) 分阶段开发，先本地展示，再 CI 验证和发布。

正式切换镜像前必须确认数据挂载、数据库、向量／对象存储和原有 secret，做完整一致性备份并在独立副本演练。原服务地址和数据可继续使用；启动本地测试或创建镜像不会迁移或清理生产数据。

不要把 `.env`、密钥、数据库、上传、备份和真实聊天提交到此公共仓库。已有 `LICENSE`、`LICENSE_NOTICE` 和 `LICENSE_HISTORY` 说明适用的上游许可；官方说明备份在 [UPSTREAM_README.md](docs/UPSTREAM_README.md)。

