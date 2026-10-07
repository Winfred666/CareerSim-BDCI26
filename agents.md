# CareerSim-BDCI26 Agent Instructions

## Jiuwen Player model

Jiuwen Player 在 coach、普通 play、benchmark 及其他测试中一律使用 `deepseek-flash`。统一配置在 `career_sim_runner/constants.py` 的 `JIUWEN_PLAYER_MODEL`；不得通过环境变量或复用会话切换成其他模型。这项约束只针对 Jiuwen Player。

## Role based swarm skills

role based swarm skills 的重点是用不同角色拆分注意力。每个角色都是自己负责指标的专家，只监控并解释自己拥有的指标、规则和证据。

专家必须给出确定答案，尤其要明确指出应选的选项、应保留的状态或应报告的结论。禁止把整段输出写成“完全不确定”“待核验”或一组没有结论的可能性；那会把本应由专家完成的判断转嫁给掌握信息更少的 Leader。

`?` 只能作为简短、肯定答复中的限定符，用来表示方向或幅度仍需核验，例如 `选 2，O+?`。它不能单独构成答复，不能替代选项、结论或优先级。专家的首句应直接给出结论，必要时再补一个很短的依据。

各角色的输出保持简短有力，避免沉迷用冒号堆叠字段、长表格或重复上下文。Leader 会自行综合和判断；角色只返回自己的明确结论、必要的指标符号和最短依据。

## Metric direction

H/D/S/N/O/W 增为好、减为坏。按用户最新约定，`R` 表示 risk（隐患），与 `HiddenRisk` 同向：`R+` 为新增隐患（坏），`R-` 为消除隐患（好）。尽量保持 R≤1，恢复至0最好；不得再解释为 risk tolerance 或容灾余量。
