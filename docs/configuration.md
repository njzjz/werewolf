# 配置与运行

推荐直接运行 `werewolf`，或用 `werewolf configure my-game.json` 指定路径。配置工作台可以创建或载入已有 JSON，并在一个界面中完成：

- 游戏语言、经典狼人杀、杀人游戏、两种捉鬼、内置电影牌组和自定义身份计数；
- 真人、LLM、本地机器人数量，以及逐席名称、persona、技能和固定身份；
- 多个 OpenAI-compatible Provider、模型、接口协议、推理强度、流式传输和 Prompt Caching；
- LLM 只读证据工具及每个动作允许的工具往返轮数；
- 全部房规、终端交互、严格模式、恢复点、公开日志和个人记忆导出；
- 保存前完整校验，以及“保存并开始游戏”。

真实终端支持方向键、`j`/`k`、Enter 和 Esc；非 TTY 环境自动使用编号输入。TUI 不要求输入真实 API 密钥，只保存环境变量名。`werewolf init` 仍可为脚本生成精简推荐配置，`werewolf init --full` 生成完整参考模板。密钥不要写入 JSON、日志或记忆文件。

## 配置工作台

```bash
# 默认创建或编辑 werewolf.json
werewolf configure

# 创建或编辑指定文件；config 和 setup 是等价别名
werewolf config tournament.json

# 不启用 ANSI 彩色样式
werewolf setup local.json --no-color
```

工作台写文件前会先执行与 `werewolf play` 相同的配置校验，再通过临时文件原子替换目标；新文件权限设为 `0600`。载入现有配置时不会丢弃 TUI 暂未展示的 `extra_headers` 等高级值。

## 推荐配置

```json
{
  "providers": {
    "default": {
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY",
      "model": "your-model-id",
      "wire_api": "responses",
      "reasoning_effort": "high"
    }
  },
  "players": [
    { "name": "真人玩家", "controller": "human" },
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

| 行为       | 推荐值                                                      |
| ---------- | ----------------------------------------------------------- |
| 语言与牌组 | 中文、`classic`                                             |
| 运行安全   | 安全进度开启、严格控制器、失败重试 2 次                     |
| 恢复与日志 | `game_runs/private.checkpoint.json`、`game_runs/public.log` |
| 终端体验   | 清屏、关键选择确认、LLM 投票并发                            |
| 记忆       | 导出到 `game_memories/`                                     |

推理强度没有跨 Provider 通用的“自动最高”值。省略 `reasoning_effort` 表示使用服务默认值，而不是最高档。质量优先建议从 `high` 开始；`xhigh`、`max` 等名称与支持范围由具体模型决定。程序会在 Chat Completions 请求中发送顶层 `reasoning_effort`，在 Responses 请求中发送 `reasoning.effort`。

不要把玩家命名为 `你`、`我` 等代词。模型会在座位表和历史中反复看到这些名称，容易误判指代；推荐使用 `真人玩家`、`主持人` 或其他明确专名。

若不需要某项文件输出，可以显式设置 `"checkpoint_path": null`、`"public_transcript_path": null` 或 `"memory_directory": null`。全部字段及当前值可通过 `werewolf init --full` 查看。

## 身份牌组

省略牌组设置时使用经典牌组。所有内置模式都通过 `role_preset` 选择：

```json
{
  "role_preset": "ghost_blank"
}
```

常用值如下：

| `role_preset`   | 默认牌组与流程 |
| --------------- | -------------- |
| `killer`        | 6–16 人杀人游戏；杀手与警察随人数扩展，其余为平民 |
| `ghost_similar` | 6–16 人捉鬼近义词版；幽灵随人数扩展，其余为水民 |
| `ghost_blank`   | 6–16 人捉鬼无词版；互认的鬼随人数扩展，其余身份为人 |

模式和玩家人数可以独立设置。默认牌组在 6–11 人配置 2 名敌对角色，12–15 人配置 3 名，16 人配置 4 名；杀人游戏同时配置同等数量的警察。三个 8 人真人加本地 bot 示例位于 `examples/killer.json`、`examples/ghost_similar.json` 和 `examples/ghost_blank.json`。捉鬼模式由 preset 自动跳过夜晚。无词版必须配置至少一个 LLM Provider，开局由它随机生成词牌并公开字数和类型；全部鬼出局后先进行鬼队私密讨论，再使用 1 次基础猜词机会，并为每名出局的人增加一次，鬼出局不增加次数。

无词版优先使用配置顺序中第一名 LLM 玩家的 Provider 出词；若没有 LLM 玩家，则使用名为 `default` 的 Provider，或配置中唯一的 Provider。多个 Provider 并存、又没有 `default` 或 LLM 玩家引用时属于歧义配置，启动会报错；可将出词 Provider 命名为 `default`，或让首名 LLM 玩家引用它。生成出的答案只进入持词玩家的私密记忆和恢复点，公开频道只看到字数与类型。

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

支持的身份名为 `villager`、`werewolf`、`police`、`seer`、`witch`、`hunter`、`medium`、`bodyguard`、`madman`、`fox`、`cupid` 和 `shared`。内置新模式会把内部的 `werewolf`/`villager` 显示为杀手/平民、幽灵/水民或无词版的鬼/人；`police` 用于杀人游戏。共有者必须为 0 或 2 张；妖狐与丘比特不能同时启用；预言家、女巫等单例身份不能重复。

整副牌默认洗牌。若主持人要指定少数玩家的身份，可只给这些玩家填写 `fixed_role`，未指定玩家继续从剩余牌堆随机抽取：

```json
{
  "name": "主持人",
  "controller": "human",
  "fixed_role": "seer"
}
```

固定身份会暴露给读取配置的人，适合主持人测试或有意设计的对局，不建议用于需要主持人也完全未知身份的普通游戏。

`roles` 只覆盖身份组成；`role_preset` 仍决定昼夜流程、身份显示、胜负条件、猜词规则、电影存活奖金与牌组专项策略。自定义普通桌游通常保持默认 `classic`；不要只为了换一副牌而套用 `killer` 或 `ghost_*`，否则会同时启用对应模式规则。

## 高级顶层选项

这些字段都可以省略，只在需要覆盖推荐行为时填写：

| 字段                       | 用途                                                          |
| -------------------------- | ------------------------------------------------------------- |
| `language`                 | `zh-CN` 或 `en`；控制法官文本和 LLM 语言要求                  |
| `seed`                     | 控制座位、身份洗牌、平票和本地 bot；不保证真实 LLM 输出可复现 |
| `clear_screen`             | 多真人共用终端时，在私密回合之间清屏                          |
| `context_char_limit`       | 单个玩家可见历史进入 LLM 提示词的字符上限                     |
| `memory_directory`         | 终局后导出每名玩家的独立记忆                                  |
| `spectator_progress`       | 显示不泄密的行动进度和单行推理耗时                            |
| `strict_controllers`       | LLM 重试耗尽后终止并保留恢复点                                |
| `controller_retries`       | 严格终止前，对同一个 LLM 动作的重试次数                       |
| `public_transcript_path`   | 实时写入可公开分享的 UTF-8 观战日志                           |
| `checkpoint_path`          | 保存含私密状态和响应日志的恢复点                              |
| `human_strategy_notes`     | 真人行动后是否询问可选的私密策略笔记                          |
| `confirm_critical_actions` | 投票、用药、开枪、查验等真人选择是否二次确认                  |
| `parallel_llm_votes`       | 并行请求互不可见的 LLM 公开投票                               |
| `max_parallel_llm_requests` | 并行投票同时发出的模型请求上限；默认 2，降低 429 限流风险     |
| `enable_tools`             | 是否允许 LLM 调用当前玩家范围内的只读证据工具；默认开启       |
| `max_tool_rounds`          | 单个动作最多工具往返轮数；默认 2，可配置 1–8                  |

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

| 字段               | 说明                                                         |
| ------------------ | ------------------------------------------------------------ |
| `base_url`         | API 根地址，客户端自动补 `/responses` 或 `/chat/completions` |
| `api_key_env`      | 推荐的密钥来源环境变量                                       |
| `api_key`          | 仅适合本地占位密钥，不建议保存真实凭据                       |
| `model`            | 服务实际接受的模型 ID                                        |
| `wire_api`         | `responses` 或 `chat`                                        |
| `reasoning_effort` | 推理强度；Chat 与 Responses 均会传递，具体档位由模型决定      |
| `max_tokens`       | 单动作输出预算；默认 4000，包含最终 JSON 及 Provider 可能计入的推理输出 |
| `use_json_mode`    | 服务不支持 JSON mode 时设为 `false`；提示词仍要求 JSON       |
| `stream`           | 默认开启，使用 SSE 接收增量，降低长推理经过代理时的超时风险  |
| `force_ipv4`       | IPv6 不可达时强制 IPv4，同时保留 TLS 主机名验证              |
| `extra_headers`    | 兼容服务要求的额外 HTTP 请求头                               |

LLM 的增量内容不会直接打印到公开频道。客户端在本地组装完整 JSON，完成解析和合法性校验后，法官才会发布允许公开的文本。

若使用 `api_key_env`，程序会在分配身份、创建新恢复点或发出请求之前检查该环境变量。请确保它是在运行 `werewolf play` 的同一个终端中导出的；只在另一个 shell 中设置不会生效。

质量优先示例：

```json
{
  "wire_api": "responses",
  "reasoning_effort": "high",
  "max_tokens": 4000
}
```

兼容服务若主要提供 Chat Completions，也可以使用同一个字段：

```json
{
  "wire_api": "chat",
  "reasoning_effort": "max"
}
```

第二个例子中的 `max` 不是 OpenAI 通用枚举，只适用于明确声明支持该值的 Provider。配置无法替代模型选择：速度型或小型模型即使开启高推理，策略质量仍可能明显弱于更强的推理模型。

Chat 流式请求会附带 `stream_options.include_usage`，否则该接口不返回 token 统计；个别兼容服务拒绝该字段时会自动改用不带它的请求重发一次。只返回 `reasoning_content` 的推理网关也会被正确解析。`finish_reason=length` 或 Responses 的 `status=incomplete` 会被视为截断而不是合法发言，控制器重试时会要求模型缩短内容并完整结束 JSON；纯 `...`/`…` 占位发言、以续写标点结束的内容，以及没有完整句末标点的长发言同样会被拒绝。公开文本另有 8000 字符的终端安全上限，正常发言通常不会触及。

有历史的发言和频道交流默认执行“检索证据 → 审核草案 → 最终回答”。并行投票仍会收到完整的玩家私密视图和全部只读工具，但工具调用保持可选，避免每名投票者都固定产生三次 Provider 请求而触发限流。观战提示会显示实际并发上限；限流较严的服务可将 `max_parallel_llm_requests` 进一步设为 `1`。

## AI 只读证据工具

LLM 默认通过 Chat Completions 或 Responses 的原生 function calling 使用六个工具：

- `get_evidence_ledger`：整理当前玩家可见的角色提及、公开票型、最新发言、私密事件和策略笔记；
- `search_visible_history`：在当前玩家的完整可见历史中搜索文本，可按公开、单人私密或队伍频道过滤；
- `get_player_dossier`：按公开玩家 ID 汇总某人的发言、他人提及、票型和当前玩家可见的私密线索。
- `get_vote_analysis`：把公开投票整理为逐轮票型、目标联盟、个人投票历史和改票次数，不自动推断阵营；
- `get_claim_matrix`：按玩家和事件序号整理角色提及、自称与否认，所有项目仍明确标记为未确认声明；
- `review_action_draft`：在最终回答前检查合法选项、必填发言、可见证据序号、反方解释和后续计划。

工具处理器只接收引擎已经裁剪过的 `PlayerView`，不能读取法官身份真相、其他玩家记忆、文件、网络或 Shell。工具结果只存在于该玩家当前动作的内存对话中，不写入公开事件、公开日志，也不会发给其他玩家。公开发言即使由工具检索出来仍是不可信证据，角色自称不会被自动升级成确认事实。

默认设置为：

```json
{
  "enable_tools": true,
  "max_tool_rounds": 2
}
```

工具循环有严格轮数上限。默认 2 轮在有历史的发言和频道交流中依次用于“证据检索”和“草案审核”：第一轮只能选择证据工具，第二轮只能提交 `review_action_draft`，随后必须输出最终 JSON。没有历史的开局动作不会被迫做无意义查询，并行投票等选择类动作也不会被强制增加请求。若将 `max_tool_rounds` 降为 1，则只保留证据检索；高于 2 的额外轮次可由模型按需使用。兼容 Provider 若明确拒绝 `tools` 或 `tool_choice` 字段，客户端会记住该能力缺失，并自动退回普通结构化回答。工具调用需要完整保留 function-call continuation，因此发生工具调用的动作使用非流式完整响应；普通无工具请求仍沿用配置的 SSE 流式路径。终局 token 摘要会同时显示工具调用数和失败数。

证据工具主要提升长局中的回溯与一致性，不替代强模型和足够的 `reasoning_effort`。它们也会增加请求轮数、输入 token 与延迟；若 Provider 的工具实现较差，可在 TUI 的“终端体验与安全”中关闭，或设置 `"enable_tools": false`。

## 模型思考与长期记忆

Provider 隐藏的内部推理或 `reasoning_content` 不会写入游戏，也不会在下一轮要求模型复述。程序只保存模型主动放入最终 JSON 的简短 `thought`、`note` 和 `memory`：

- `thought` 与 `note` 合并为带昼夜阶段的私密策略笔记；
- `memory.beliefs` 按玩家 ID 保存 0–100 的怀疑度、置信度、可见证据序号和简短理由；
- `memory.open_questions` 保存待核验问题；
- `memory.plan` 保存下一步计划；
- `memory.counter_case` 保存当前主判断最强的反方解释。

这些内容只进入该玩家下一轮的私密提示词和证据账本，并写入私密 checkpoint 与该玩家自己的终局记忆文件。无效玩家 ID、不可见证据序号和过长文本会被丢弃或裁剪；旧 checkpoint 没有结构化状态时按空状态恢复。它们是玩家自己的策略结论，不是法官事实，也不会发送给其他玩家。

## Prompt Caching

提示词固定采用“公共规则 → 公共事件历史 → 公共状态 → 玩家私密身份 → 私密频道历史 → 私密策略笔记 → 当前动态请求”的顺序。所有玩家可共享尽可能长的公共前缀，私密身份与历史只出现在共享前缀之后；公共发言被明确标记为不可信转录，不能借此覆盖法官规则或索取私密信息。

三条历史通道分别保持追加式增长，并共同使用 `context_char_limit`，不会因为拆分而扩大总上下文预算。超过上限时各通道按稳定区块裁剪，避免新增公共发言时重排私密策略笔记、或新增私密事件时破坏公共缓存前缀。

Responses provider 可选：

```json
{
  "prompt_cache": true,
  "prompt_cache_retention": "24h"
}
```

开启后，客户端把公共规则与每名玩家稳定的私密上下文散列成不含明文身份信息的独立 `prompt_cache_key`。动态公共/私密历史和当前动作不会进入该路由键。`prompt_cache_retention` 可选 `in-memory` 或 `24h`，实际支持范围由模型和兼容服务决定。

部分代理会拒绝这些新字段，因此默认关闭。即使 `prompt_cache` 为 `false`，上游若支持自动前缀缓存，稳定前缀设计仍然有效。工具定义对所有玩家保持稳定，工具结果只追加在公共与私密提示前缀之后，因此不会破坏已经命中的前缀；但每次工具往返都会新增后缀 token，所以“缓存 token 数”可能上升而总体缓存率仍受动态内容占比影响。游戏结束时，如果 provider 返回 `usage`，终端会汇总输入、缓存命中、输出 token 和工具调用；token 统计只覆盖当前进程，游戏时长和控制器可靠性统计会随恢复点延续。

缓存命中的读取兼容三种常见写法：`prompt_tokens_details.cached_tokens` 或 `input_tokens_details.cached_tokens`（OpenAI、vLLM、DashScope）、`prompt_cache_hit_tokens`（DeepSeek 官方 API）、`cache_read_input_tokens`（Anthropic 兼容网关）。如果 provider 的 `usage` 里一个都没有，终端会显示“缓存命中 未知”，而不是 0%——这类网关只是没有上报缓存字段，未必真的没有命中缓存。

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

新开局不会覆盖已有检查点、公开日志或非空记忆目录。应优先使用 `--resume` 继续旧局；只有确认要丢弃旧输出时才使用 `--force-new`。检查点、公开日志和记忆目录解析后也必须是三个不同路径。

法官在应用回答前会先做归一和校验：

- `choice` 允许写成座位号、`3号`、公开标签或玩家姓名，只要唯一指向一个合法选项就按该选项执行；
- 可以弃权的动作接受显式的 `null`、`弃权`、`不使用解药` 等表述；省略 `choice` 字段属于无效回答并会触发重试，必须选择的能力则不接受弃权；
- 公开发言和遗言必须给出非空 `text`，模型返回空正文按无效回答处理，本人或本地机器人的沉默不受影响；
- 判定无效时，重试请求会附带法官给出的原因，模型据此修正，而不是原样重复上一次回答。

选择类动作使用独立的最小输出契约，只允许返回 `{"choice": ...}`，不会同时要求模型生成发言、心路历程或结构化记忆。兼容服务支持严格 JSON Schema 时，合法选项会直接作为枚举交给 provider，并在服务端强制 `choice` 必填；不支持该能力的 OpenAI-compatible 网关会自动降级到普通 JSON mode。若降级后的回答漏掉 `choice`，只有在 `text`、`thought` 或 `note` 中出现唯一且明确的行动目标时才会保守恢复，嫌疑列表或多目标表述仍会进入正常重试。终局可靠性统计会按“动作类型/安全错误类别”给出失败明细。

严格模式中，私密夜间动作失败时终端错误不会显示玩家姓名或具体身份能力，避免恢复后污染信息边界。CLI 会保留恢复点并直接打印可复制的 `--resume` 命令。

启动时还会检查常见实时体验风险：关闭进度、未配置恢复点、允许后备、`xhigh`/`max` 推理或超过 5000 token 的单动作输出预算都会在身份分配前给出公开提示。模型等待状态使用灰色单行原地刷新，动作完成后擦除；重试提示也不会持续刷入滚动区和公开日志。

公开投票仍保持互不可见，但默认最多同时发送 4 个模型请求。HTTP 429 会遵循 Provider 的数字型 `Retry-After`，否则使用有上限的指数退避，避免十个席位同时立即重试造成限流风暴。

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

查验这类由自己动作产生的私密结果会在同一个回合内当场显示：法官先给出结果并等待确认，然后才提示交接终端，因此真人预言家不需要等到下一次行动才知道验人结果。从恢复点重放已完成的动作时只重建个人记忆，不会把结果再打印到别人面前。

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
