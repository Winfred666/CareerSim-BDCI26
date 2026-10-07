# 五组指标问卷工作流

更新：2026-09-29。沿用静默 Leader calling；主流程八个 worker，分析阶段五路并行。

## 版本与角色

| 角色 | 选定 policy | 形态 |
|---|---|---|
| analyse_O | contextual100/selected/questionnaire.md | 逐题分支，保留原引擎 |
| analyse_N | flat-n40-20260928/flat-polish.md | 全部选项一次分档 |
| analyse_S | frozen-final120/flat-polish.md | 全部选项一次分档 |
| analyse_HW | dictionary-v2/candidate-d27.md | H/W 联合分档 |
| analyse_R | career-r-flat-20260929-1caz2cpn/flat-polish.md | 全部选项一次分档，保留原引擎 |

五份 policy 文本原样复制；绝对来源、源脚本与 SHA256 见
`../skills/observe-decide-review/scripts/questionnaires/sources.json`。
N/S/HW 的同目录文件是 benchmark driver，提取其答案验证与持久化接口为共享 flat.py；不复制它们的 GT、评测数据或外部 /tmp 依赖。

## 调用和状态

Leader：observe → analyse → decide → review。分析阶段 label 路由身份，唤醒正文仅为
`请调用脚本回答新问题`；运行层将对应 analyse_X.md 放入 minimal 模式实际渲染的 identity。
worker 无需读取 skill、其他角色规则、源码或同伴结果。

```bash
python3 "<skill_dir>/scripts/analyse.py" O ready
python3 "<skill_dir>/scripts/analyse.py" O '1'
python3 "<skill_dir>/scripts/analyse.py" HW '{"1":{"H":0,"W":1},"2":{"H":-1,"W":0}}'
```

ready 返回完整公开题面、选项与当前题；R 的模板已直接嵌入这些内容，避免重复。
返回 `complete=true` 后仅 structured_output({}) 结束。所有事件号、题目游标由脚本维护。
N/R 答案为选项号→整数；S 为选项号→{S:整数}；HW 为选项号→{H:整数,W:整数}。
O 每次提交当前问题要求的一个编号。

| 文件 | 用途 |
|---|---|
| notebooks/translation-context.json | 当前会话、事件、选项、观察摘要、版本摘要 |
| translations/会话摘要/event_id/public-event.json | 公开题面；同一事件已执行的公开前情 |
| translations/…/组/.event.json、.policy.json | 该组输入快照 |
| translations/…/组/.answers.json、cursor.json | 已答题与脚本拥有的游标 |
| translations/…/组/complete.json | 覆盖全部选项的版本绑定完成凭据 |
| translations/…/result.json | 五组完成后自动写入的六指标数值汇总 |
| notebooks/workflow-state.json | 阶段屏障与当前事件 |

每组独立文件锁；完成凭据与共享结果原子替换。脚本仅在全部结果可用时让 decide 读取，
且 workflow 必须等全部 worker 真正结束。公开菜单有权威增量时无需模型翻译。
D 不翻译，R+ 是新增隐患，R- 是消除隐患。

## 静默与重复答案

manifest 开启 silent_team、lean_team，swarmflow_agents_per_run=5。
Leader 的 swarmflow 在 Python 中等待完成，不产生等待期间模型调用；失败立即阻塞。
角色工具白名单禁止消息、跨指标、源码读取、shell 串联；越界强制结束，避免宿主吞掉普通异常后继续执行。

连续几题可能都答 `0`。运行层从 cursor.json 读取游标，自动标记工具 bookkeeping 的 call_goal，
脚本命令仍只有组名与答案，模型不填写 ID。这样宿主不会把不同问题误判为重复调用死循环。

旧三路版本的历史实测保留于 [ons-workflow-practice.md](ons-workflow-practice.md)；
当前八角色实测与 benchmark 见 [2026-09-29记录](questionnaire-integration-20260929.md)。
