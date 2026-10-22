> 两仓库更新：Android 已归入 frontend；后端同步解析 main 历史中的最新已发布 frontend-* 产品，忽略 APK Release 和文档提交。文档变化不会重复构建镜像。

# 后端检查、镜像与串联交付

共同进度见 [PROJECT_STATUS.md](PROJECT_STATUS.md)，日常操作见 [CLOUD_DEVELOPMENT.md](CLOUD_DEVELOPMENT.md)。

## 实际入口

- PR：`Backend` 运行只读源码/基础测试、SQLite 生命周期和 PostgreSQL 迁移检查，不推镜像。
- main push、定时或手动：`Sync verified frontend and build` 检查前端当前 main 的 Release，校验并提交 frontend.lock.json，再显式派发 `backend.yml`。
- `Backend` 正式镜像任务只在 main 的 workflow_dispatch 运行；不是“main push 直接编译镜像”。直接手动派发时也会检查前端锁是否过期。
- `Test published image` 是额外的手动隔离冒烟入口，不发布镜像。

当前工作流分别为 .github/workflows/backend.yml、frontend-sync.yml、test-image.yml。全部使用本仓库 GITHUB_TOKEN，不需要生产 SSH 密钥或跨仓库写入 PAT。

## 自动检查与镜像提升

1. Python 3.11 运行源码边界/语法与标准库基础测试。
2. 固定官方运行时容器运行 SQLite 资源生命周期和 PostgreSQL 迁移检查；这些检查独立于开发者本地依赖。
3. 下载并逐文件校验锁定的前端，基于 Dockerfile 中不可变的官方依赖镜像构建 linux/amd64 和 linux/arm64。
4. 推送 `ghcr.io/mike666wq/open-webui-backend:sha-<完整源码SHA>`。同一源码已有标签时复用其 digest，不覆盖它。
5. 按不可变 digest 拉取 amd64，检查健康、基线版本、前端全部摘要、首页/聊天深链、JS/CSS 与缺失资源 404。
6. 使用 compose.test.yml 的独立测试数据运行健康及首次测试注册登录；临时账号与卷不属于生产。
7. 仅前述检查全部成功才把同一多架构 digest 推广为 latest。arm64 构建成功不等于真实 arm64 机器运行已验收。

镜像 SHA、BUILD_HASH、OCI revision 和前端身份可追溯；镜像内 /app/frontend.lock.json 与 /app/build/frontend-manifest.json 保留来源。候选 sha 标签存在不代表 latest 或生产已通过。

## 并发、失败与重试

PR 新提交可取消旧检查；main 发布串行，不取消已经开始的发布。镜像/冒烟失败时不会提升 latest。优先重跑失败 job；源码修复应生成新的提交和不可变标签。同步 non-force push 遇到并发/保护规则时不会覆盖 main。

基础检查也可在具备 Python 的机器运行：

```sh
python3 scripts/check_source.py
python3 -m unittest discover -s tests -v
```

本地资源有限时不必执行完整依赖安装、镜像编译或数据库集成测试；Actions 会执行相应检查。

## 手动镜像测试

Actions → Test published image，选匹配所选分支 frontend.lock.json 的 latest、sha-<完整SHA> 或 sha256:<digest>。脚本校验输入后解析为不可变引用，再运行隔离冒烟。

单独运行需要 Docker 已启动且镜像已拉取：

```sh
python3 scripts/image_smoke.py --image ghcr.io/mike666wq/open-webui-backend@sha256:<实际摘要>
```

image_smoke 使用随机容器/卷/网络、回环随机端口、阻断出网的测试网络，不挂载生产数据、不注册业务账号；独立 compose 环境检查则另行创建一次性合成测试账号。两种测试的目的和证据分别保留，不混称为真实用户联调。

报告位于 artifacts/image-smoke/ 和 artifacts/compose-test/；正式构建保留14天，手动镜像测试保留7天。不上传实际用户数据或容器环境密钥。

## 当前验收与发布边界

[run 39722585622](https://github.com/Mike666wq/open-webui-backend/actions/runs/39722585622) 已通过以上正式镜像门禁。资源生命周期代码已经合入，不能再称为“仅空脚手架”；真实多人/多设备和生产副本演练仍待完成，见 [RESOURCE_MANAGEMENT.md](RESOURCE_MANAGEMENT.md)。

GHCR 包的可见性由仓库管理员配置；镜像发布不等于服务器已切换。生产备份、迁移、恢复演练和部署另行安排。本流程不会自动清理任何现有生产数据。Codespaces 仍未通过实际启动验收，当前暂停使用。
