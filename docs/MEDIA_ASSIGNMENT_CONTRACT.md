# 媒体出口指派契约（P-009）

状态：**已定稿**。前后端必须逐字遵守本文；两仓各存同一份。

## 1. 目标

四个媒体用途 —— `transcription` / `speech` / `image` / `realtime` —— 各自只有一个权威出口：
「某条 `provider_connection` 下的某个白名单模型」。旧配置字段（`audio.stt.*`、
`audio.tts.*`、`audio.realtime.*`、`image_generation.*`、`images.edit.*`）降级为
**迁移来源**，指派后标记 inactive，不再参与出站解析。

## 2. 核心规则

- **R-A 显式指派**：不存在推断式自动迁移。哪条连接、哪个模型，由管理员在界面上明确选择。
- **R-B 全实例用途必须用共享连接**：`purpose` 属于全实例功能时，`connection.visibility`
  必须是 `shared`；否则写入返回 `400`，`code = MEDIA_CONNECTION_PRIVATE`。
- **R-C 模型必须在该连接的白名单内**，且其 `uses[purpose]` 已声明。否则 `400`，
  `code = MEDIA_MODEL_NOT_ENABLED`（文案要指出「先在该连接下添加该用途的模型」）。
- **R-D 连接地址只用 base**：`config.base_url` 是 base；服务级覆盖走
  `config.services[purpose].endpoint`，`kind` 为 `base_url` 或 `endpoint_url`。
  旧配置里的完整 endpoint 在读取时归一化为 base，不再写入。
- **R-E 密钥不回传**：任何 `*/config` GET 只返回 `has_api_key: bool`，
  不回 `IMAGES_OPENAI_API_KEY` / `OPENAI_API_KEY` / `COMFYUI_API_KEY` 等明文。
  保存时字段**留空表示不变**，显式传 `""` 不删除已有 key（删除走单独字段，见 §6）。
- **R-F 幂等与回退**：同一 `(purpose, connection_id, model)` 重复提交返回同一结果；
  回退恢复旧字段并且 `ENGINE` 回到指派前的值。

## 3. 数据结构

### 3.1 用途描述（服务端返回）

```jsonc
{
  "purpose": "image",                       // transcription | speech | image | realtime
  "label": "生图",
  "mode": "provider",                       // provider | legacy | unconfigured
  "connection_id": "ebc64de9-…",            // mode=provider 时存在
  "connection_name": "MiniMax",
  "model": "provider:ebc64de9-…:MiniMax-H3-Image",
  "upstream_model": "MiniMax-H3-Image",
  "adapter": "minimax-image",
  "legacy": {                                // 旧配置快照，永远不含明文 key
    "engine": "openai",
    "base_url": "https://api.minimax.cn/v1/images/generations",
    "normalized_base_url": "https://api.minimax.cn/v1",
    "model": "MiniMax-H3-Image",
    "has_api_key": true,
    "configured": true
  },
  "ready": false,                            // 能否直接出站
  "reasons": [                               // ready=false 时的可操作原因
    {"code": "MEDIA_MODEL_NOT_ENABLED", "message": "该连接下还没有生图模型…"}
  ]
}
```

`mode` 判定：

| 值 | 条件 |
| --- | --- |
| `provider` | `engine == 'provider'` 且 `connection_id` 非空 |
| `legacy` | 旧字段有配置（`configured == true`） |
| `unconfigured` | 两者都没有 |

### 3.2 候选（供前端下拉）

```jsonc
{
  "connection_id": "ebc64de9-…",
  "name": "MiniMax",
  "provider_id": "minimax",
  "visibility": "shared",                   // private 的仍返回，但前端要标不可用于全实例用途
  "adapter": "minimax-image",
  "supported": true,
  "models": [
    {"id": "provider:ebc64de9-…:MiniMax-H3-Image",
     "upstream_id": "MiniMax-H3-Image",
     "enabled": true,
     "verification": "untested"}            // passed | failed | untested
  ]
}
```

## 4. 接口

### 4.1 `GET /api/v1/providers/media-assignment`（admin）

返回：

```jsonc
{
  "object": "media_assignment",
  "purposes": [ /* §3.1 × 4，顺序固定 transcription, speech, image, realtime */ ],
  "candidates": { "image": [ /* §3.2 */ ], "speech": [], "...": [] }
}
```

`candidates[purpose]` 只包含**该用途 `supported == true`** 的连接。

### 4.2 `PUT /api/v1/providers/media-assignment/{purpose}`（admin）

请求体：

```jsonc
{ "connection_id": "ebc64de9-…", "model_id": "provider:ebc64de9-…:MiniMax-H3-Image" }
```

- 校验顺序：purpose 合法 → 连接存在且可管理 → `visibility == 'shared'`（R-B）→ 服务
  `supported` → 模型在白名单且声明了该用途（R-C）→ 写入。
- 成功后写入配置：`{prefix}.engine = 'provider'`、
  `{prefix}.provider_connection_id = connection_id`、`{prefix}.model = model_id`；
  并把本次写入前三个旧值存到 `{prefix}._previous`（JSON），供回退。同时写
  `{prefix}._assignment_fingerprint = sha256(purpose|connection_id|model_id)`。
- 幂等：若指纹与当前一致，直接返回当前状态，不改 `_previous`。
- 返回：§3.1 的单个 purpose 对象（`mode = provider`）。

`purpose` 前缀映射：

| purpose | prefix | model key |
| --- | --- | --- |
| `transcription` | `audio.stt` | `model` |
| `speech` | `audio.tts` | `model` |
| `image` | `image_generation` | `model` |
| `realtime` | `audio.realtime` | `MODEL` |

> 注意 `audio.realtime` 的 model 键是 `MODEL`（大写），沿用该模块既有键名。

### 4.3 `DELETE /api/v1/providers/media-assignment/{purpose}`（admin）

把 `{prefix}._previous` 恢复回三个键（engine / provider_connection_id / model），
清空 `_assignment_fingerprint` 与 `_previous`。若没有 `_previous`，返回 `409`
`code = MEDIA_NO_PREVIOUS_ASSIGNMENT`。返回 §3.1 的对象。

## 5. 出站解析（后端内部，不对外）

新增单一入口，`images.py` 与 `audio/__init__.py` **不再各自实现引擎分支**：

```python
async def resolve_media_target(purpose: str, user) -> MediaTarget
# MediaTarget: adapter, base_url, endpoint, model, upstream_model, connection_id
```

规则：

1. `mode == 'provider'` → 用 `resolve_configured_media` 拿连接与模型，再
   `resolve_service(connection, purpose, adapter)` 拿最终地址。**地址只追加一次登记路径。**
2. `mode == 'legacy'` → 保持现有 `openai` / `automatic1111` / `comfyui` / `gemini` 分支不变。
   仅在这一分支里，`base_url` 若形如完整 endpoint，先剥离登记路径再使用（R-D）。
3. `mode == 'unconfigured'` → 抛 `400`，`code = MEDIA_NOT_CONFIGURED`。

## 6. 密钥脱敏细节

- GET `*/config` 返回体里，所有 `*_API_KEY` 与 `API_KEY` 字段值统一替换为 `''`，
  并额外返回 `has_api_key: bool`（按该用途聚合，放在同一对象里）。
- 保存（POST `*/config/update`）：请求里 key 字段为空字符串 → **保留原值**；
  传 `"__clear__"` → 删除。这样前端不需要「是否已设置」的额外分支。
- 前端所有密码框 placeholder 从「留空不改」改为按 `has_api_key` 显示。

## 7. 验收（与 P-009 的 K1–K12 对应）

契约层必须能验证：K1 四用途同页可见、K2 指派后配置正确、K3 私有连接 400、
K4 MiniMax 走 `minimax-image` 且路径不叠加、K5 endpoint 归一化、K6 指派不产生 private、
K7 幂等与回退、K8 无明文 key、K9 realtime 可指派、K10 白名单为空有引导、
K11 普通用户不再 403、K12 测试在移除修复时会失败。
