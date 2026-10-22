# 当前项目状态

更新：2026-10-22（Asia/Shanghai）。运行事实以 Actions、Release 和锁文件为准。

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
