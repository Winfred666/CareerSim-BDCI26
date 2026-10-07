# 五组优选问卷接入与20题实测

日期：2026-09-29。N/S/O/HW/R policy 均采用用户指定文件，文本 hash 不变。
O/R 复制原状态引擎；N/S/HW 从同目录评测 driver 移植验证逻辑为独立状态脚本。
没有将 hidden GT、调参结果或基准答案写入运行 policy。

## 已完成的接入

- analyse_O、analyse_N、analyse_S、analyse_HW、analyse_R 五路独立；H/W 合并，R 独立，D 不翻译。
- 唤醒仅 `请调用脚本回答新问题`；role markdown 由运行层放入实际渲染的 identity。
- `analyse.py 组 ready/答案` 不接收模型填写的 event_id/question_id；脚本维护当前事件、游标、结果及五份完成凭据。
- ready 输出题干、选项、当前问题与提交格式；同一事件的已执行公开前情由脚本保存。
- 五个 worker 全部结束且完成凭据完整后才放行 decide；read-context 仅返回六指标数值及提示。
- dictionary 从 solution 移除。查表、压缩、归因及旧 benchmark driver/skill 已归档；旧账本保持原样。

## 20题比较

运行：`20260929T082800Z-questionnaires`；模型 deepseek-flash；seed `career-sim-translation-v1`；20事件、46选项。
每事件并行启动五个直接 API 上下文；不加载 Jiuwen 通用工作区，不混入其他指标答案。
O 连续问答；N/S/HW/R 每事件一次全选项分档。D 不参与以下准确率和 FP/FN。

| 版本 | 加权 cosine | 六指标准确率 | 选项全对 | FP | FN | 遗漏 | 反向 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 新版问卷 · deepseek-flash | 59.98% | 58.70% | 4.35% | 71 | 54 | 26 | 11 |
| 旧 dictionary 历史最佳 · 账本 gpt-6-luna | 74.46% | 67.75% | 21.74% | 55 | 40 | 13 | 6 |
| 旧 dictionary · deepseek-flash | 69.45% | 71.38% | 17.39% | 48 | 37 | 19 | 5 |

cosine 权重：R×5、D×0、其他×1。六指标准确率含零值，容许幅度差≤0.5；选项全对必须六指标全部正确。
遗漏/反向只计 O/S/N/H/R；反向与遗漏互斥，不计纯幅度误差。

历史最佳来自 `20260923T182215Z-career-sim-translation-v1`：先按完整412题成绩选定（68.51%），再提取同20题。
同模型基线来自 `20260927T061823Z-career-sim-translation-v1`：完整池55.72%，同20题69.45%。
所有旧答案均复用并验证 GT 相同，没有重译、挑选同题最高成绩或修改答案。

**本次新版未胜出，也未达到历史65%综合分门槛。** 旧最佳与本次模型不同；即使看同模型历史结果，
前情、thinking、批次与提示仍有差异，不能把差值全归因于 policy。该 dev 池参与过迭代，本次也是样本内评估。
一处多路径合流题保留原抽样位置，向五个角色明确提供“前情无法唯一确定”，未虚构分支。

### Token 与失败调用

| 角色 | API调用 | 输入 token | 输出 token |
|---|---:|---:|---:|
| O | 236 | 335,637 | 7,316 |
| N | 20 | 17,848 | 856 |
| S | 22 | 20,672 | 1,070 |
| HW | 20 | 22,180 | 1,270 |
| R | 22 | 19,461 | 930 |
| 总计 | 320 | 415,798 | 11,442 |

总 token **427,240**，其中输入缓存325,248；reasoning字段320次均未提供，不能按0推理token解释。
O 占输入80.72%，平均每事件11.8次作答调用，是主要成本来源。

首事件 S 返回缺项，R 返回非JSON，各一次自动重试仍不可用。保留四次失败原始响应和token；
检查后把提交工具改为按全部选项生成类型结构，只定向重试未完成的 S/R。O/N/HW 成功答案完全复用。
总重试4次（自动2、格式修复后定向2），无语义复核/纠错调用，无重复有效翻译。

旧最佳同题结果行归属 token：输入328,107、输出4,166；旧 deepseek：输入397,320、输出213,835。
这些旧行可能归属批次、额外上下文或历史复核，原始调用数/推理token无法据此核定，因此不能据此宣称严格的同题成本降幅。

### 诊断

| 指标 | 正确/46 | 遗漏 | 反向 | GT零值误报 |
|---|---:|---:|---:|---:|
| O | 25 | 6 | 4 | 7 |
| N | 19 | 10 | 2 | 10 |
| S | 29 | 5 | 1 | 5 |
| H | 29 | 4 | 0 | 11 |
| W | 42 | 2 | 0 | 2 |
| R | 18 | 1 | 4 | 6 |

N 的遗漏与零值误报各10；H 有11次零值误报；O/R 各4次反向。R 权重高，其方向与幅度错误对综合分影响较大。
这是需要后续单独验证的改进方向，本轮没有用这20题回填或修改指定问卷。

## 八角色真实工作流

实际 deepseek-flash 运行 `ons-workflow-practice-10`，公开游戏会话 `24892e36e34a47d7891897400c2b0526`：

- observe → 五路 analyse → decide → review，8个worker完成，1次游戏行动，最终 reviewed，Leader idle。
- 文字/推理帧0，send_message调用0。
- 四段真实 worker 执行区间中，Leader 模型调用均为0；五份结果完成后才进入 decide。
- 检查26份分析模型请求，首条用户消息均为固定唤醒短句；21条分析脚本命令均无ID参数。
- 从 observe 调用到 review 完成约40.18秒；单事件实测，不代表所有事件延迟。
- Leader 7次模型调用、输入28,124/输出734 token；N/S/HW/R各3次（ready、提交、结束），O14次。

此前尝试 `practice-9` 在observe处被边界阻塞：minimal模式未渲染自定义career_role区，worker尝试读skill。
已修为identity注入、移除worker冗余skill发现提示，并以真实minimal builder测试及practice-10验证。
重复数字答案的工具标记由脚本游标生成；agent不填写ID，框架不误判不同问题为重复调用。

验证：629项pytest通过，5个依赖弃用warning；skill格式校验通过。实际练习实例已停止。

## 可复查证据

- [完整20题报告](../../.career_sim_runner/translation_benchmark/20260929T082800Z-questionnaires/benchmark.md)
- [精确模型请求/响应和脚本快照](../../.career_sim_runner/translation_benchmark/20260929T082800Z-questionnaires/)
- [八角色完成验证](/tmp/career-ons-workflow-practice/run-10/verification.json)
- [固定唤醒、无ID与等待区间验证](/tmp/career-ons-workflow-practice/run-10/boundary-verification.json)
- [benchmark skill](../../.codex/skills/simcareer-translation-benchmark/SKILL.md)

benchmark保留当时的脚本快照；随后仅加强live ready的全选项JSON格式提示、minimal角色装载与运行边界，
未重新翻译已完成题目。后续评测从新run冻结当前版本；resume继续旧run的冻结版本。
