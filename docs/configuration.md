# 配置与运行

`werewolf init werewolf.json` 会生成一份可直接修改的完整配置。密钥应通过环境变量读取，不要写入 JSON、日志或记忆文件。

## 最小示例

```json
{
  "language": "zh-CN",
  "role_preset": "classic",
  "seed": null,
  "clear_screen": true,
  "memory_directory": "game_memories",
  "context_char_limit": 24000,
  "spectator_progress": true,
  "strict_controllers": true,
  "controller_retries": 2,
  "public_transcript_path": "game_runs/public.log",
  "checkpoint_path": "game_runs/private.checkpoint.json",
  "providers": {
    "default": {
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY",
      "model": "your-model-id",
      "wire_api": "responses",
      "reasoning_effort": "low",
      "use_json_mode": false,
      "stream": true,
      "timeout": 300,
      "max_tokens": 500,
      "prompt_cache": false
    }
  },
  "players": [
    {
      "name": "真人玩家",
      "controller": "human",
      "persona": "谨慎的证据派",
      "skills": ["logic", "social", "memory"]
    },
    {
      "name": "智能体02",
      "controller": "llm",
      "provider": "default",
      "persona": "发言简洁，重视票型",
      "skills": ["logic", "social", "deception", "memory"]
    }
  ]
}
```

## 顶层字段

| 字段 | 用途 |
| --- | --- |
| `language` | `zh-CN` 或 `en`；控制法官文本和 LLM 语言要求 |
| `role_preset` | `classic` 或电影牌组名称 |
| `seed` | 控制座位、身份洗牌、平票和本地 bot；不保证真实 LLM 输出可复现 |
| `clear_screen` | 多真人共用终端时，在私密回合之间清屏 |
| `context_char_limit` | 单个玩家可见历史进入 LLM 提示词的字符上限 |
| `memory_directory` | 终局后导出每名玩家的独立记忆；设为 `null` 可关闭 |
| `spectator_progress` | 显示不泄密的行动进度和单行推理耗时 |
| `strict_controllers` | LLM 失败时直接终止，不回退到本地 bot |
| `controller_retries` | 严格终止前，对同一个 LLM 动作的重试次数 |
| `public_transcript_path` | 实时写入可公开分享的 UTF-8 观战日志 |
| `checkpoint_path` | 保存含私密状态和响应日志的恢复点 |

## 玩家控制器

- `human`：从当前终端读取发言、选择和私密策略笔记。
- `llm`：调用指定 provider；每次只发送该玩家已经获权的个人视图。
- `bot`：不访问网络的简单本地机器人，用于演示和测试，不代表 LLM 水平。

玩家配置中的 `persona` 进入该玩家的稳定系统提示。`skills` 可选择：

- `logic`：追踪事实、声明、票型和矛盾。
- `social`：观察站边、关系变化和表达方式。
- `deception`：在可见信息边界内进行身份伪装。
- `memory`：回顾历史并维护简短策略笔记。

法官还会自动注入全局技能、真实身份技能、电影生存目标、恋人子身份技能以及适用的牌组专项技能。身份技能只进入对应玩家的私密上下文。

## Provider 与流式传输

同一局可以配置多个 provider，混用 OpenAI、兼容代理或本地服务。

| 字段 | 说明 |
| --- | --- |
| `base_url` | API 根地址，客户端自动补 `/responses` 或 `/chat/completions` |
| `api_key_env` | 推荐的密钥来源环境变量 |
| `api_key` | 仅适合本地占位密钥，不建议保存真实凭据 |
| `model` | 服务实际接受的模型 ID |
| `wire_api` | `responses` 或 `chat` |
| `reasoning_effort` | 兼容 Responses 推理强度，例如 `low`、`high`、`xhigh` |
| `use_json_mode` | 服务不支持 JSON mode 时设为 `false`；提示词仍要求 JSON |
| `stream` | 使用 SSE 接收增量，降低长推理经过代理时的超时风险 |
| `force_ipv4` | IPv6 不可达时强制 IPv4，同时保留 TLS 主机名验证 |
| `extra_headers` | 兼容服务要求的额外 HTTP 请求头 |

LLM 的增量内容不会直接打印到公开频道。客户端在本地组装完整 JSON，完成解析和合法性校验后，法官才会发布允许公开的文本。

## Prompt Caching

提示词固定采用“稳定规则与身份 → 个人可见历史 → 当前动态请求”的顺序。历史超过上限时按稳定区块裁剪，避免缓存前缀每轮滑动。

Responses provider 可选：

```json
{
  "prompt_cache": true,
  "prompt_cache_retention": "24h"
}
```

开启后，客户端把每名玩家稳定的私密系统前缀散列成不含明文身份信息的独立 `prompt_cache_key`。`prompt_cache_retention` 可选 `in-memory` 或 `24h`，实际支持范围由模型和兼容服务决定。

部分代理会拒绝这些新字段，因此默认关闭。即使 `prompt_cache` 为 `false`，上游若支持自动前缀缓存，稳定前缀设计仍然有效。游戏结束时，如果 provider 返回 `usage`，终端会汇总输入、缓存命中和输出 token；恢复后的统计只覆盖当前进程。

参考：[OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)。

## 观战、严格模式与恢复

正式的纯 LLM 对局建议：

```bash
werewolf play --config movie.json --spectator --strict-controllers \
  --controller-retries 2 \
  --transcript game_runs/movie_public.log \
  --checkpoint game_runs/movie_private.checkpoint.json
```

另开终端观战：

```bash
tail -f game_runs/movie_public.log
```

恢复中止对局：

```bash
werewolf play --config movie.json \
  --resume game_runs/movie_private.checkpoint.json
```

每次控制器成功返回后都会写入动作日志。恢复时程序回到安全阶段边界，重放已经完成的响应，只重新请求第一个未完成动作，避免重复投票或重复使用技能。

公开日志只包含法官公告、公开发言、公开投票、合法遗言和安全进度。恢复点包含身份、恋人关系、私密记忆、心路历程和响应记录，权限设为 `0600`，不能公开分享。

## 真人终端

多名真人共用终端时应保持 `clear_screen: true`，并避免查看终端回滚缓冲。正式线下局更适合每名真人使用独立进程或设备。

Unix/Linux/macOS 会自动启用 `readline`/`libedit`，支持中文按字符退格和左右移动光标；缺少 readline 时回退到 Python 基础输入行为。

## 记忆导出

默认在 `game_memories/` 下为每名玩家生成单独 JSON，内容包括：

- 最终身份和实际加载的技能；
- 该玩家获权看到的公开、私密、狼人或恋人事件；
- 自己每次行动后的 `thought` 与 `note`；
- 恋人信息仅写入相关玩家自己的文件。

记忆文件包含敏感个人视角。分享前应按玩家分别检查；可用 `werewolf play --no-memory` 或 `memory_directory: null` 关闭导出。
