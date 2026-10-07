---
name: observe-decide-review
description: CareerSim Leader 执行初始化、观察、决策、复盘；指标问卷交给原生 teammate。
version: 4.7.5
kind: swarm-skill
teammate_mode: build_mode
roles:
  - id: translate-o
    kind: ai_agent
    purpose: 回答 O 问卷
    path: stages/translate_O.md
  - id: translate-n
    kind: ai_agent
    purpose: 回答 N 问卷
    path: stages/translate_N.md
  - id: translate-s
    kind: ai_agent
    purpose: 回答 S 问卷
    path: stages/translate_S.md
  - id: translate-hw
    kind: ai_agent
    purpose: 回答 H/W 问卷
    path: stages/translate_HW.md
  - id: translate-r
    kind: ai_agent
    purpose: 回答 R 问卷
    path: stages/translate_R.md
---

# CareerSim Leader

持续运行到第48个月的结构化终局。Leader 亲自执行初始化、observe、decide、review；translate 阶段只向本轮对应的 teammate 发送专属 DM。所有结果由脚本存文件，不发送成员回复、轮次报告或最终文本。脚本返回 terminal 时停止；dispatch 等待失败按下文重新唤醒，其余错误停止。

## 初始化

初始化只在新局执行。

1. 本次 `skill_tool` 返回的 `skill_directory` 是技能绝对目录；调用一次 new_game，随后执行 `python3 "<skill_directory>/scripts/workflow-state.py" create_new "<实际 session_id>"`。按返回提示只重读 `<skill_directory>/SKILL.md`，继续初始化第 2 步，不允许读其他文件，不重复创建游戏。

2. 调用一次 observe(session_id="<session_id>")，确认“新员工手册”只有一个“继续”项，按其编号调用一次 take_action(session_id="<session_id>", notes="")；成功后继续建队，此步不 review。
3. 调用 `build_team({"display_name":"CareerSim","team_desc":"文件式协作","leader_display_name":"Leader","leader_desc":"执行答题循环","enable_hitt":false,"enable_task_verification":false})`。
4. 建队后直接进入 observe；teammate 在需要翻译的事件中由 dispatch 创建。

## 循环

每个事件按 observe → dispatch（按提示跳过）→ decide → review → observe 循环至 terminal。本轮 read-context 返回决策包后才可 take_action，notes 按 decide 阶段填写；不得依据 observe 的事件文字直接行动。

### observe

调用一次 career-emulator observe(session_id="<session_id>")，用返回路径执行 `python3 "<K>/skills/observe-decide-review/scripts/refresh-context.py" "<observe_json_path>"`。返回 `可直接进入 decide 阶段 read-context` 时进入 decide；返回 `请进入 dispatch 阶段，分配任务` 时进入 dispatch；返回 `terminal` 时停止。

### dispatch

当 observe 提示进入 dispatch 时，先执行命令：

```text
python3 "<K>/skills/observe-decide-review/scripts/dispatch.py"
```

脚本返回 `commands` 数组；每项的 `tool` 是原生工具名，`arguments` 是完整参数。按数组顺序调用对应工具，参数原样传入，不自行计数、拼接、改名或补命令。前面的调用全部成功后才能执行下一种工具；任一调用失败可最简修复，不重放成功命令。

首次派发是五条 spawn、五条 DM；以后是五条 spawn、五条 shutdown 上一组、五条 DM，共十五条。不再次 build_team。名单及 prompt 全由脚本生成，不自行修改。若直接进入 decide 则不运行 `dispatch.py`。

全部命令成功后，执行等待命令： `python3 "<K>/skills/observe-decide-review/scripts/workflow-state.py" wait`；命令返回前持续等待，不发消息、不读其他文件。返回「继续执行同一等待命令」时继续执行等待。翻译完成后默认返回本轮决策包（推荐或纠偏），直接进入 decide；返回 terminal 时停止。仅当等待返回 `workflow_error: completion timeout:` 时，再调用同一 `dispatch.py` 脚本，原样执行它返回的未完成成员 DM，再重试同一等待命令；不重新 spawn 或 shutdown。`commands` 为空时直接重试等待。其他 workflow_error 或 context_error 按错误停止。

### decide

尚未取得本轮决策包时，执行 `python3 "<K>/skills/observe-decide-review/scripts/read-context.py"`。

1. 若返回「推荐选项」，直接采用该编号，将同包 notes 原样填写到 take_action 的 notes，不重新分析或改写。

2. 否则说明推荐可信度过低，会返回完整原题、前情、原始选项及当前状态约束。只依据这些信息独立判断，遵守当前状态的禁减约束，不读取任何推荐机制、脚本代码或 notebooks 文件。自主决策时，用你职场老手的经验决策，自行用一句简短行为理由填写 notes，不推演未来事件。

季度体力行动每次只提交一个合法整数编号，完成 review→observe 后继续分配剩余体力；不组合编码，不提前结束循环。

完成决策后，调用一次 `career-emulator take_action(session_id="<session_id>", choice=<合法整数>, notes=<上述notes>)`；成功后立即进入 review。

### review

调用一次 career-emulator check_latest_logs(session_id="<session_id>", count=3)， 不分析语义内容，只用返回路径执行 `python3 "<K>/skills/observe-decide-review/scripts/record-review.py" "<log_file>"`。脚本会自动记录分析决策结果。收到 `review_recorded` 后直接回到 observe。

## 边界约束

实际指标变化与事件预测不同是正常现象，不必回看，不必检查。decide 按 read-context 返回的推荐编号或原事件执行。Leader 只按本文命令执行脚本，不读取脚本。初始化及循环均禁止用 pwd、ls、glob、find、rg 或其他工具检索、验证目录。路径已由 skill_tool 给出，无需再确认。不读取其他角色文档或 coach/runner 文件，不读写任何源码。除 dispatch 等待失败按上述规则重试外，出错即停。不自行调试，不自行重放动作。
