# 本地开发与 GitHub 构建

日常只维护 frontend 和 backend 两个仓库，当前架构见 [PROJECT_STATUS.md](PROJECT_STATUS.md)。

1. 本地编辑、提交并推送。网页与 Android 在 frontend，客户端位于 `apps/client/`。
2. 网页变更运行前端测试、生产构建和真实 Chromium 检查；通过后发布 ZIP/manifest/SHA256SUMS。
3. frontend 内 Android 工作流复用本次相同产物，运行客户端/原生测试及签名、包元数据和完整性检查后发布 APK。
4. backend 定时或手动 `Sync verified frontend and build` 消费最新已发布前端产品，更新锁后测试、构建并验证镜像，再提升 latest。
5. 原生修改不重新发布网页；后端文档变化与网页文档变化都不生成新产品。手动运行允许明确重建。

客户端目录使用自己的 package.json、package-lock.json 和 Rust 工具链；不要在网页根目录运行客户端的 npm 命令。
旧 client 只保留历史，不再承担自动同步。原签名必须迁移到 frontend 的仓库 Secrets；迁移完成后撤销临时凭据。

本地可选测试：在有 Docker 的环境下载 backend 的 compose.test.yml，运行 `docker compose -f compose.test.yml up -d`，
访问 http://127.0.0.1:8080。使用独立测试卷，不能连接生产数据库。
GitHub 可手动运行 backend 的 `Test published image`，验证已发布镜像并保存测试证据。
真实页面和 Android 操作仍需人工验收；自动测试不等于所有设备和新需求都验证完成。

Codespaces 暂停使用。此前开发容器初始化失败，未完成修复验收，不属于必需开发环节。
