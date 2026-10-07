# Manifest、Jiuwen 与 Coach 改动报告

本文记录当前工作区相对 `HEAD` 的 Jiuwen/Coach 相关源码改动，并说明 runner 支持的 SwarmFlow 开关、实际并发语义，以及仍属于 Jiuwen 实例配置而尚未开放给参赛 manifest 的能力。`solution/` 内的策略改动不是本次 runner 提交的一部分。

## 结论

- `enable_swarmflow` 不是第三种运行 `mode`，而是 `mode: "team"` 下只给 Leader 挂载 SwarmFlow 编排工具的能力开关。
- 当前 `observe-decide-review` 已使用 `mode: "agent"`，由同一 agent 串行执行各阶段，共享上下文和工具；翻译保留在上下文，不再使用 SwarmFlow 或翻译文件交接。下文的 SwarmFlow 说明仍适用于 runner 支持的其他 team 提交。
- 普通 `reload` 现在保留相同 Jiuwen `agent_session_id`、Leader、团队状态和原始会话历史；只有显式指定替代 session 时才建立新会话并注入重建的历史文件。
- `withdraw` 现在拆成可恢复的阶段：取消在途工作、回退 host 历史、持久化阶段标记、删除独立的 team runtime、回退游戏快照，再以同一 Player 标识重建干净团队。删除失败后重试不会二次 rewind。
- 为支持这套语义，runner 对固定版本的 OpenJiuwen/JiuwenSwarm 应用窄补丁：增加 worker 的共享 skill CWD、按 run 隔离 resume journal，并让 rewind 后的 `session.delete` 能从持久 binding 找回 team ownership。

## 当前 manifest 契约

runner 当前真正读取的提交字段如下：

| 字段 | 当前行为 | 校验状态 |
|---|---|---|
| `team` | 团队名，也是提交显示名的第一优先级 | 必填语义；拒绝空值和占位名 |
| `name` | `team` 缺失时作为提交显示名 | 无额外类型校验 |
| `mode` | 写入 WebSocket chat envelope；仅支持 `agent`、`team` | 枚举校验 |
| `instruction` | 安装时写入 Jiuwen workspace 的 `IDENTITY.md`，作为持续系统指令 | 归一化为字符串 |
| `enable_swarmflow` | 安装记录保留该值；实例配置时把布尔值写到 `modes.team.*.enable_swarmflow` | 当前只判断键是否存在，尚未做严格布尔类型校验 |

runner 还会在安装记录中生成 `submission_mode` 和 `participant_skill_names`；它们不是参赛者应手写的 manifest 字段。其他未知字段目前会被原样保存在 active install record 中，但没有执行语义。

`enable_swarmflow` 的数据流是：

```text
solution/manifest.json
  -> active_install.json
  -> ensure_instance_configured()
  -> Jiuwen config.yaml: modes.team.<template>.enable_swarmflow
  -> Team Leader 获得 swarmflow 工具
```

目前实现会更新 `modes.team` 下所有模板，而不是只更新被选中的模板。`mode: "agent"` 即使携带该字段也不会得到 team-only 的 SwarmFlow 工具。manifest 应使用 JSON 布尔值 `true` / `false`；字符串 `"false"` 在当前桥接代码中会被 Python 视为真值，这是后续应补的校验点。

## SwarmFlow 是什么

SwarmFlow 是 team Leader 可选的一层确定性 Python 工作流 DSL，不是持久 teammate 机制，也不是新的 chat mode。

一次 `swarmflow(...)` 调用会先异步返回 `task_id`，后台执行脚本，完成后把最终结果注入 Leader；Leader 也可以用 `async_task_output(task_id=...)` 阻塞取回结果。脚本里的每次 `agent()` 都构造一个用完即弃的 worker：worker 继承 teammate 的模型、工具、skill 与 workspace 能力，但不带建队、派任务、成员消息等团队协作工具。

当前 `solution/skills/observe-decide-review/scripts/stage.py` 的做法是：

1. 每次 workflow 只执行一个 `agent()`；
2. Leader 取回该 worker 的最终文本后才启动下一阶段；
3. worker 通过 `options={"cwd": "team_skills"}` 直接在团队共享 skill 根目录工作；
4. `META.resume_scope = "run"` 让每次新的工具调用使用独立 journal，避免相同脚本在同一 session 内被旧结果短路；同一次 run 的暂停/恢复仍复用自己的 journal。

所以这里的收益主要是隔离、明确的输入输出和自动回灌，而不是墙钟并行。Translator、Decision Maker、Leader action、Reviewer 对同一游戏状态有因果依赖，串行是正确语义；若将这些阶段直接并行，会产生读旧状态、重复动作或并发写 notebook 的风险。

## Jiuwen/OpenJiuwen 兼容补丁

`career_sim_runner/jiuwen_patch.py` 在实例配置前幂等检查并应用两个随包分发的 patch；首次应用只接受精确版本：

- `openjiuwen==0.1.16.post2`
  - 给 SwarmFlow worker 增加白名单选项 `cwd="team_skills"`；它只能指向当前 team 的共享 skills 目录，且不能与 worktree isolation 同时使用。
  - 给 workflow `META` 增加 `resume_scope: "session" | "run"`；默认仍是原有的 session 级内容寻址，`run` 级 journal 追加稳定 `run_id`。
  - 复用 SwarmFlow 已有的真实 token budget ledger，按 `run_id` 隔离并发 workflow 的消耗；每个后台执行段结束时把 worker 总 token 作为独立 usage 事件写回外层 WebSocket。该事件只提供可靠的 `total_tokens`，不伪造无法恢复的 input/output 拆分，也不改变 workflow 返回值。
- `jiuwenswarm==0.2.4b3`
  - `session.delete` 无法从已 rewind 的空 host metadata 取得 team name 时，回退查询持久的 session-to-team binding；只接受唯一匹配。

补丁的默认行为保持不变，只有显式使用新选项或 withdraw 删除路径时生效。安装包现在把 `patches/*.patch` 纳入 package data。运行环境必须存在 `patch` 命令；版本不匹配、只应用了一半或 marker 校验失败都会 fail fast，避免在未知上游源码上模糊套补丁。

WebSocket 层还增加了两个明确控制面操作：

- `cancel_agent_session()` -> `chat.interrupt(intent="cancel", mode="team")`，用于终止即将丢弃的在途 team 工作；
- `delete_agent_session()` -> `session.delete`，用于删除 host session 目录，并清除对应的成员、任务和消息状态。

所有 Jiuwen Player 入口的 chat envelope 显式携带 `gpt-5.6-luna`，统一取自 `JIUWEN_PLAYER_MODEL`。`agent.reload_config` 同时更新服务进程的 `MODEL_NAME`，避免未注册的模型名静默回退到旧模型；仅修改磁盘 `.env` 不足以更新已运行服务。

## Coach 的 step-wise withdraw

Team 会话实际有三套需要协调的状态：

```text
Jiuwen host conversation/history
          +
OpenJiuwen team runtime (members/tasks/messages)
          +
CareerSim game snapshot/logs/checkpoints
```

仅调用 `session.rewind` 只能处理第一层。旧 team runtime 仍可能记得被撤回动作已经完成，因此新的 withdraw 顺序是：

1. 对活跃 team 执行 cancel，并撤销旧 execution gate/worker；
2. 校验实时游戏仍等于最新 checkpoint 的 post-state；
3. 将 Jiuwen host history rewind 到所选决策 turn；
4. 立即把 `pending_withdraw_*` 写入 coach registry，形成跨进程的阶段提交点；
5. 调用 `session.delete` 删除该 Player 的 host session，并清除其独立持久化的 team runtime；
6. 删除所选 checkpoint 及其后记录，把 CareerSim payload、模拟器日志和 coach 日志恢复到 action 前；
7. 标记 `jiuwen_rewound` / `jiuwen_team_reset`，随后可自动 reload。

若第 5 步失败，游戏快照和 checkpoint 尚未改动；再次执行相同 game/round 的 withdraw 会识别 `pending_withdraw_*`，跳过已经成功的 host rewind，只重试 team delete，成功后再进入游戏回退。这样外部 RPC 的半成功不会导致第二次 rewind 空历史。

## Reload session 语义

普通 reload 被重新定义为“热更新配置/skill”，而不是“换一条对话”：

- 默认沿用原 `agent_session_id`；普通 team reload 先 pause，让旧 stream waiter 停在检查点，再 retire 旧 gate，安装 skill 后通过同一 session 恢复。
- 同一 session 已拥有权威对话历史，不再给 prompt 注入 `previous-context-*.json`，避免重复回放；只有显式指定不同 replacement session 才用该文件 bootstrap。
- solution reinstall 会覆盖 runtime skill 文件，因此 reload 后只扫描 `*/notebooks/session.json`，并且只修改显式含 `session_id` 键的文件，把当前 game session 重新绑定进去；其他 notebook 和源码不动。
- withdraw 后仍复用相同 Player 标识，但此前 team runtime 已删除。reload prompt 明确要求按当前 `observe` 重新开始当前阶段，并以当前游戏状态为唯一事实。
- reload 完成后清除 rewind/team-reset 标记，保留 checkpoint 到同一 agent session 的绑定。

显式 replacement session 仍然可用作灾难恢复，但不再是普通 reload 的默认路径。

## 值得关注的并行/团队概念

下表中的项目是已安装 Jiuwen/OpenJiuwen 具备的能力；除 `enable_swarmflow` 外，当前 runner **没有**把它们从 `solution/manifest.json` 转发到实例 team template。

| 概念 | 作用 | 当前建议 |
|---|---|---|
| `swarmflow_concurrency.max_workflows` | 单个 Leader 同时运行的 workflow 数上限（L1，默认 16） | 最适合作为下一批受控 manifest 字段之一 |
| `swarmflow_concurrency.agents_per_run` | 单 workflow 同时占用的 worker 上限（L2；空值使用引擎默认） | 可用于限制单脚本 fan-out |
| `swarmflow_concurrency.max_agents_total` | 该 Leader 所有 workflow 合计的 worker 上限（L3，默认 64） | 应由平台设硬上限，再允许提交件向下收紧 |
| `swarmflow_budget` | 同一 Leader 跨所有 workflow 共用的 token 硬上限 | 适合由比赛/运行方控制；可允许 manifest 只向下声明预算 |
| `parallel([...])` | fork-join barrier；分支并发，全部完成后返回 | 适合独立多视角搜集；有跨 item 汇总依赖时使用 |
| `pipeline(items, ...)` | 每个 item 独立流过多阶段，无全局 barrier | 多阶段批处理通常比逐阶段 `parallel` 延迟更低 |
| `map_parallel` / `pmap` | 安全绑定 item 的并行 map | 避免 lambda 闭包捕获最后一个变量 |
| `agent_session()` | 有状态的多轮临时 worker session | 适合一个专家需要连续追问；不等于持久 team member |
| `team_mode` | `default` 动态 roster、`predefined` 固定 roster、`hybrid` 预定义加动态扩容 | 会改变 Leader 工具面和建队协议，不宜未经约束直接开放 |
| `predefined_members` | 预先声明专家 roster | 可减少运行时建队随机性，但扩大 manifest schema 和模型/工具审计面 |
| `dispatch_mode` | `autonomous` 共享任务板抢占，或 `scheduled` 由 Leader 定向派发并自动 handoff/review | 是另一套持久团队并行模型，与当前一次性 worker 模型应分开比较 |
| `enable_task_verification` 与 review 阈值/轮数 | scheduled dispatch 的多 reviewer 投票与返工循环 | 适合需要审议的任务，不适合 CareerSim 每事件低延迟链路默认启用 |
| `lifecycle` | `temporary` / `persistent` 团队生命周期 | 直接影响 reload、memory 和清理语义，应继续由平台控制 |
| `teammate_mode` | `build_mode` / `plan_mode`，控制成员是否先提交 task plan | 属于持久 teammate 协议，不是 SwarmFlow worker 开关 |
| `model_pool` / routing strategy | 把并发成员分散到多个模型 endpoint | 决定真实吞吐和公平性，优先由部署方配置 |
| worktree isolation | 并行写代码时让 worker 使用独立 git worktree | 有 200–500ms 级创建和磁盘成本；当前共享 notebook 流程不应使用 |

SwarmFlow 引擎还限制单次 `parallel` / `pipeline` 最多 4096 项、单 workflow 最多 1000 个 agent，并只允许一层嵌套 workflow。默认 per-run 并发解析为 `min(16, CPU 核数 - 2)` 一类的引擎 cap；最终仍同时受 L2/L3 governor 限制。

### 建议的 manifest 演进顺序

如果要继续把并行能力开放到提交件，建议采用显式 allow-list，而不是把任意 team YAML 透传：

1. 先严格校验 `enable_swarmflow` 必须为 JSON boolean，并只修改实际选中的 team template；
2. 再开放一个受平台上限夹持的 `swarmflow_concurrency`，允许参赛方案向下收紧 `max_workflows`、`agents_per_run`、`max_agents_total`；
3. 可选开放只允许向下声明的 `swarmflow_budget`；
4. `team_mode`、`dispatch_mode`、`predefined_members`、model routing 等拓扑/资源字段继续留在平台配置，直到比赛公平性、清理语义和 validation contract 都明确。

当前 CareerSim 的动作有严格顺序和唯一写者要求，因此“更多并发”只适用于同一只读阶段内的独立研究/投票；Translator -> Decision Maker -> action -> Reviewer 主链仍应串行。

## 测试覆盖与边界

本次非 `solution/` 改动配有以下回归测试：

- patch 完整性、严格版本与幂等应用；
- manifest 的 `enable_swarmflow` 到 team config 桥接；
- chat envelope 显式模型、cancel/delete RPC 参数；
- Leader 外层用量与 SwarmFlow worker 总 token 合并计入 benchmark，并按稳定 `usage_id` 防止重放重复计费；
- reload 保留同一 agent session、普通 reload pause、withdraw reload 重建干净 team；
- reinstall 后只重绑声明过的 `session.json`；
- team delete 首次失败后，第二次 withdraw 不重复 host rewind；
- 既有 Coach checkpoint、日志、执行 gate 和 WebSocket 行为的回归。

这些测试验证 runner 的控制面和合成状态机，不等于已经用真实模型跑过所有 SwarmFlow fan-out、服务异常或进程崩溃组合。尤其需要继续关注：严格 manifest 类型校验、只更新选中 team template，以及运行时 patch 与上游版本升级的维护成本。
