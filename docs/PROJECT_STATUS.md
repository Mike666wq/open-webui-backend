# 三仓库当前进度

核对日期：2026-10-22（Asia/Shanghai）。本文件在三个仓库保持同一份说明；它记录项目状态，不代替实时 Actions、Release 和版本锁。

## 开发与交付方式

日常在本地编辑和推送代码；依赖安装、生产构建、自动测试、镜像和 APK 打包交给 GitHub Actions。人工验收使用独立测试服务和手机。Codespaces 当前暂停使用，未通过实际环境验收，也不是发布前置条件。

| 仓库 | 职责 | 已有能力 | 尚不能据此宣称完成 |
|---|---|---|---|
| [open-webui-frontend](https://github.com/Mike666wq/open-webui-frontend) | Svelte 网页、静态资源 ZIP、来源 manifest | DIY 外观与壁纸、目录、资源管理页面；单元测试、生产构建、移动 Chromium 检查和不可变 Release | 全站重构全部完成、所有页面人工验收、真实模型与多设备回归 |
| [open-webui-backend](https://github.com/Mike666wq/open-webui-backend) | Python API、资源生命周期、数据库迁移、GHCR 镜像 | 资源 API/模型/迁移及隔离测试已合入；SQLite 生命周期、PostgreSQL 迁移、镜像资源校验及独立注册登录检查 | 生产数据库已迁移、真实多人并发已全面验收、已释放真实生产磁盘空间 |
| [open-webui-client](https://github.com/Mike666wq/open-webui-client) | Tauri Android 外壳、内置前端、原生连接和保存、签名 APK | 固定前端来源、原生单元测试、ARM64 编译、签名/元数据/产物完整性校验和 Release | HyperOS 真机问题已解决、所有 SAF provider 已通过、桌面当前版本已重新构建 |

官方兼容基线仍为 Open WebUI 0.12.2，上游提交为 `6a18585ff4c2e5f6805f682222cfbc46dc669ef8`。这不等于三个仓库必须拥有相同 Git SHA 或版本号。

## 什么才算三仓库对齐

1. 前端已成功发布与选定源码 SHA 对应的 `frontend-<SHA前12位>` Release。
2. 后端和客户端的 `frontend.lock.json` 使用相同的完整前端 SHA、ZIP URL、ZIP 和 manifest 校验值。
3. 对应后端镜像和 APK 的构建检查成功；仅看到绿色同步任务不代表镜像或 APK 已完成。
4. 如果前端 main 又有新提交，先等新前端 Release，再同步下游；运行中的版本允许继续完成，不把“排队中”写成“已发布”。
5. 文档更新也会改变源码 SHA；需要核对更新之后的版本锁和构建结果。

实时核对入口：

| 项目 | 入口 |
|---|---|
| 前端已发布包 | [Releases](https://github.com/Mike666wq/open-webui-frontend/releases) / [Actions](https://github.com/Mike666wq/open-webui-frontend/actions/workflows/frontend.yml) |
| 后端前端版本锁 | [frontend.lock.json](https://github.com/Mike666wq/open-webui-backend/blob/main/frontend.lock.json) |
| 客户端前端版本锁 | [frontend.lock.json](https://github.com/Mike666wq/open-webui-client/blob/main/frontend.lock.json) |
| 后端镜像结果 | [Backend workflow](https://github.com/Mike666wq/open-webui-backend/actions/workflows/backend.yml) |
| APK 结果与下载 | [Android workflow](https://github.com/Mike666wq/open-webui-client/actions/workflows/android.yml) / [最新 Release](https://github.com/Mike666wq/open-webui-client/releases/latest) |

## 已验证的交付证据

以下是已完成的历史证据，不是对未来每个提交的保证：

- [前端 run 39721953479](https://github.com/Mike666wq/open-webui-frontend/actions/runs/39721953479)：前端测试、构建、浏览器检查和 Release。
- [后端 run 39722585622](https://github.com/Mike666wq/open-webui-backend/actions/runs/39722585622)：源码/基础测试、SQLite 生命周期、PostgreSQL 迁移、多架构镜像、资源与独立测试环境检查，通过后更新 latest。
- [客户端 run 39722809500](https://github.com/Mike666wq/open-webui-client/actions/runs/39722809500)：Android 编译、原生测试、签名与包校验，并发布 [v0.2.31](https://github.com/Mike666wq/open-webui-client/releases/tag/v0.2.31)。

## 当前明确未完成的事项

- Codespaces：旧通用镜像解压超出机器磁盘，轻量镜像重建又受 Yarn apt 源签名错误影响；实际环境仍处于恢复模式。保留的配置不是“已经可用”的证明，当前不继续把它作为默认开发入口。
- 人工验收：完整网页体验、真实模型调用、长会话、多人/多设备、手机安装覆盖与 SAF provider、HyperOS 启动稳定性仍需独立记录。
- 生产：本轮不连接、迁移、清理或替换生产数据库、文件卷与服务；推送镜像不等于生产已升级。
- 桌面：历史 Windows/macOS 产物不等于当前源码的桌面验收；正式桌面签名、公证、自动更新和商店分发未完成。

## 文档如何阅读

[云端交付与人工验收](CLOUD_DEVELOPMENT.md) 是三仓库共享操作说明。各仓库 README 解释本仓库职责；CI 文档解释实际工作流；设计方案描述目标，验收清单记录证据，二者不能互相替代。上游 README、LICENSE 和 CHANGELOG 保留来源语义，不改写成自有项目的进度报告。
