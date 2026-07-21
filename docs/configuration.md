# 配置与运行

`werewolf init` 默认生成一份精简推荐配置；确认 provider、模型和玩家后即可运行 `werewolf play`。需要查看或修改全部高级字段时使用 `werewolf init --full`。密钥应通过环境变量读取，不要写入 JSON、日志或记忆文件。

## 推荐配置

```json
{
  "providers": {
    "default": {
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY",
      "model": "your-model-id"
    }
  },
  "players": [
    {"name": "你", "controller": "human"},
    "智能体1",
    "智能体2",
    "智能体3",
    "智能体4",
    "智能体5",
    "智能体6",
    "智能体7"
  ]
}
```

字符串形式的玩家默认使用 `llm` 控制器。只有一个 provider 时，它会自动分配给所有没有显式填写 `provider` 的 LLM 玩家。

省略字段时会采用以下推荐值：

| 行为 | 推荐值 |
| --- | --- |
| 语言与牌组 | 中文、`classic` |
| 运行安全 | 安全进度开启、严格控制器、失败重试 2 次 |
| 恢复与日志 | `game_runs/private.checkpoint.json`、`game_runs/public.log` |
| 终端体验 | 清屏、关键选择确认、LLM 投票并发 |
| 记忆 | 导出到 `game_memories/` |

若不需要某项文件输出，可以显式设置 `"checkpoint_path": null`、`"public_transcript_path": null` 或 `"memory_directory": null`。全部字段及当前值可通过 `werewolf init --full` 查看。

## 身份牌组

省略牌组设置时使用经典牌组。内置电影牌组仍可通过 `role_preset` 选择：

```json
{
  "role_preset": "movie_lovers"
}
```

需要自由组合身份牌时，使用 `roles` 计数表；计数总和必须等于玩家人数：

```json
{
  "roles": {
    "werewolf": 2,
    "villager": 3,
    "seer": 1,
    "witch": 1,
    "hunter": 1
  }
}
```

支持的身份名为 `villager`、`werewolf`、`seer`、`witch`、`hunter`、`medium`、`bodyguard`、`madman`、`fox`、`cupid` 和 `shared`。共有者必须为 0 或 2 张；妖狐与丘比特不能同时启用；预言家、女巫等单例身份不能重复。

整副牌默认洗牌。若主持人要指定少数玩家的身份，可只给这些玩家填写 `fixed_role`，未指定玩家继续从剩余牌堆随机抽取：

```json
{
  "name": "主持人",
  "controller": "human",
  "fixed_role": "seer"
}
```

固定身份会暴露给读取配置的人，适合主持人测试或有意设计的对局，不建议用于需要主持人也完全未知身份的普通游戏。

`roles` 只覆盖身份组成；`role_preset` 仍决定经典或电影模式的存活奖金与牌组专项策略。自定义普通桌游通常保持默认 `classic`，需要电影生存结算时可同时指定相应电影 preset。

## 高级顶层选项

这些字段都可以省略，只在需要覆盖推荐行为时填写：

| 字段 | 用途 |
| --- | --- |
| `language` | `zh-CN` 或 `en`；控制法官文本和 LLM 语言要求 |
| `seed` | 控制座位、身份洗牌、平票和本地 bot；不保证真实 LLM 输出可复现 |
| `clear_screen` | 多真人共用终端时，在私密回合之间清屏 |
| `context_char_limit` | 单个玩家可见历史进入 LLM 提示词的字符上限 |
| `memory_directory` | 终局后导出每名玩家的独立记忆 |
| `spectator_progress` | 显示不泄密的行动进度和单行推理耗时 |
| `strict_controllers` | LLM 重试耗尽后终止并保留恢复点 |
| `controller_retries` | 严格终止前，对同一个 LLM 动作的重试次数 |
| `public_transcript_path` | 实时写入可公开分享的 UTF-8 观战日志 |
| `checkpoint_path` | 保存含私密状态和响应日志的恢复点 |
| `human_strategy_notes` | 真人行动后是否询问可选的私密策略笔记 |
| `confirm_critical_actions` | 投票、用药、开枪、查验等真人选择是否二次确认 |
| `parallel_llm_votes` | 并行请求互不可见的 LLM 公开投票 |

## 玩家控制器

- `human`：从当前终端读取发言和选择；策略笔记可通过配置或 `--strategy-notes` 开启。
- `llm`：调用指定 provider；每次只发送该玩家已经获权的个人视图。
- `bot`：不访问网络的简单本地机器人，用于演示和测试，不代表 LLM 水平。

对象形式的玩家可以设置 `persona`、`skills`、`provider` 和 `fixed_role`。`persona` 进入该玩家的稳定系统提示。`skills` 可选择：

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

部分代理会拒绝这些新字段，因此默认关闭。即使 `prompt_cache` 为 `false`，上游若支持自动前缀缓存，稳定前缀设计仍然有效。游戏结束时，如果 provider 返回 `usage`，终端会汇总输入、缓存命中和输出 token；token 统计只覆盖当前进程，游戏时长和控制器可靠性统计会随恢复点延续。

参考：[OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)。

## 观战、严格模式与恢复

精简配置已经默认开启安全进度、严格模式、两次重试、公开日志和恢复点，因此正式对局通常直接运行：

```bash
werewolf play movie.json
tail -f game_runs/public.log
```

只有临时覆盖配置时才需要附加参数，例如为这一局改用独立文件：

```bash
werewolf play movie.json \
  --transcript game_runs/movie_public.log \
  --checkpoint game_runs/movie_private.checkpoint.json
```

另开终端观战：

```bash
tail -f game_runs/movie_public.log
```

恢复中止对局：

```bash
werewolf play movie.json \
  --resume game_runs/movie_private.checkpoint.json
```

每次控制器成功返回后都会写入动作日志。恢复时程序回到安全阶段边界，重放已经完成的响应，只重新请求第一个未完成动作，避免重复投票或重复使用技能。

严格模式中，私密夜间动作失败时终端错误不会显示玩家姓名或具体身份能力，避免恢复后污染信息边界。CLI 会保留恢复点并直接打印可复制的 `--resume` 命令。

启动时还会检查常见实时体验风险：关闭进度、未配置恢复点、允许后备、`xhigh` 推理或超过 5000 token 的单动作输出预算都会在身份分配前给出公开提示。

### 显式安全后备

`--allow-fallback` 或 `strict_controllers: false` 适合不要求完整 LLM 可信度的休闲对局。它与旧式随机本地机器人后备不同：

- 公开发言、遗言和票型会标记“系统安全后备”；
- 投票、女巫用药、猎人开枪等可放弃动作默认弃权；
- 查验、守护、丘比特连人等必须选择的能力使用第一个合法座位；
- 私密后备在过程中只显示不泄密的技术提示，终局再披露玩家、动作和错误；
- 终局统计会明确说明本局不满足完整 LLM 对局标准。

公开日志只包含法官公告、公开发言、公开投票、合法遗言和安全进度。恢复点包含身份、恋人关系、私密记忆、心路历程和响应记录，权限设为 `0600`，不能公开分享。

## 真人终端

多名真人共用终端时应保持 `clear_screen: true`，并避免查看终端回滚缓冲。正式线下局更适合每名真人使用独立进程或设备。只有一名真人时，程序不再在每次行动后要求“交接终端”；多真人时仍保留清屏和交接流程。

真人私密回合会显示稳定座位号、当前存活/死亡名单、最近关键事件，以及最近一次已完成胜负检查产生的公开狼人数量上限。相同的座位图和机械约束也会进入 LLM 当前请求，减少与即时胜负条件冲突的身份叙事。

精简面板不会删除任何授权信息；在发言或选择提示中输入 `/history` 可以随时按昼夜分组查看完整个人可见历史，然后继续当前动作。

关键选择默认需要回车确认；可用 `--no-confirm` 关闭。机器调用方可以使用 `--json-result` 在本地化结算后追加一行结构化结果。

Unix/Linux/macOS 会自动启用 `readline`/`libedit`，支持中文按字符退格和左右移动光标；缺少 readline 时回退到 Python 基础输入行为。

## 记忆导出

默认在 `game_memories/` 下为每名玩家生成单独 JSON，内容包括：

- 最终身份和实际加载的技能；
- 该玩家获权看到的公开、私密、狼人或恋人事件；
- 自己每次行动后的 `thought` 与 `note`；
- 恋人信息仅写入相关玩家自己的文件。

记忆文件包含敏感个人视角。分享前应按玩家分别检查；可用 `werewolf play --no-memory` 或 `memory_directory: null` 关闭导出。
