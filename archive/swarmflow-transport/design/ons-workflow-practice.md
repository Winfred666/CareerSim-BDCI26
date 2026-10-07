# 静默 O/N/S workflow 历史实测

本文保留三路版本的原始记录。当前五路、八角色版本见 [2026-09-29接入记录](questionnaire-integration-20260929.md)。

日期：2026-09-27；模型：`deepseek-flash`。在隔离实例 `career_ons_workflow` 中，用公开游戏 API 完成手册初始化后，交给真实 Jiuwen Leader 执行一个剧情事件。未运行完整48个月比赛，未重新评估翻译准确率。

## 精简后的运行验证（run-8，当前流程）

继续使用 `deepseek-flash`，真实完成一个剧情事件：6个 worker 全部完成，1次行动，最终 reviewed，Leader 回到 idle。文字/推理帧0、send_message 0、错误/警告日志0。O/N/S 并行启动，decide 在最慢的 O 结束后启动。

运行层直接阻塞 `swarmflow`：四个阶段从启动到所有 worker 完成的区间内，Leader 模型调用均为0。框架会吞普通 rail 异常，因此越界同时设置 force_finish；真实回调链测试确认模型和工具主体不会执行。模型不再调用 async_task_output；异步宿主的重复完成通知被抑制。UI 保留启动、工具和状态控制帧；模型收到的是校验后的阶段结果。

| 角色 | 模型调用次数 | 首次输入 token | 整轮输入 token（含缓存） | 整轮输出 token | 可见工具数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Leader | 7 | 2,662 | 26,651 | 1,064 | 初始化8，阶段内1 |
| observe | 4 | 2,116 | 9,913 | 429 | 4 |
| analyse_O | 13 | 1,932 | 71,332 | 3,559 | 2 |
| analyse_N | 8 | 1,932 | 25,585 | 1,426 | 2 |
| analyse_S | 19 | 1,932 | 77,586 | 2,483 | 2 |
| decide | 4 | 2,479 | 13,771 | 1,527 | 3 |
| review | 4 | 2,166 | 10,289 | 550 | 4 |

对照上一版 run-6：Leader 首次输入45,186→2,662（减少94.1%），整轮输入533,906→26,651（减少95.0%），模型调用11→7。observe 首次输入27,519→2,116。Leader 输入中缓存命中486,272→18,432；不能把总输入降幅当作费用降幅。

本轮总输入+输出246,165 token；旧样本1,996,922。两轮事件和问答路径不同，这个总量差异不作为严格配对收益。本轮从首次请求到收尾约59秒，旧样本约60秒；问卷推理仍占主要耗时，不宣称所有事件都会更快，也未证明绝对最少 token 或翻译精度不变。

精简范围：角色白名单工具、固定命令入口、短静态角色规则；移除不适用的通用派单、汇报、任务清单和进化提示；保留安全、来源及文件边界。问卷与 decide policy 语义未更改。首次手册若与公开规则基线不同则停止，不允许 Leader 临时改写角色。后续问卷/policy 更新继续沿用原接口；新增工具或脚本入口时须同步更新角色白名单。

证据：

- [完整验收](/tmp/career-ons-workflow-practice/run-8/verification.json)、[逐角色用量](/tmp/career-ons-workflow-practice/run-8/token-summary.json)。
- [真实等待区间核验](/tmp/career-ons-workflow-practice/run-8/wait-verification.json)、[逐次调用遥测](/tmp/career-ons-workflow-practice/run-8/notebooks/runtime-usage.jsonl)。
- [通信帧](/tmp/career-ons-workflow-practice/run-8/frames.jsonl)、[执行日志](/tmp/career-ons-workflow-practice/run-8/runtime-execution.log)。
- [完整回归](/tmp/ons-lean-full-suite.txt)：627 passed；最后补充的初始化边界切换、真实回调链强制终止已通过[专项19项](/tmp/ons-lean-boundary-tests.txt)。

以下保留旧 run-6 作为优化前证据；它使用显式 async_task_output 等待，已被当前阻塞机制替代。

## 优化前结果（run-6）

- Leader 按 observe → analyse → decide → review 调用磁盘 workflow；6个 worker 全部完成，event_id=`00001`，最终 phase=`reviewed`。
- 正式事件只执行一次 take_action，选择1，notes为空；随后权威日志复核完成。
- 所有发出的帧均为工具、控制或遥测事件；自然语言/推理文字帧0，send_message调用0。运行日志确认 Leader 最后回到 idle。
- Leader 全程以 async_task_output(block=true) 等待当前 task_id；没有重复启动分支。上一轮也验证了 running 后继续等待同一个 task_id。

| worker | 提交时间 UTC | 完成时间 UTC |
| --- | --- | --- |
| observe | 17:20:50.617 | 17:20:56.242 |
| analyse_O | 17:20:57.963 | 17:21:16.300 |
| analyse_N | 17:20:57.973 | 17:21:12.584 |
| analyse_S | 17:20:57.975 | 17:21:24.656 |
| decide | 17:21:26.104 | 17:21:33.787 |
| review | 17:21:35.714 | 17:21:41.576 |

manifest 已显式设置 `swarmflow_agents_per_run: 3`。运行日志确认 O/N/S 三个 worker 均在17:20:58 UTC实际开始执行；decide 在最慢分支 S 完成之后启动。前一轮容量2的[实测](/tmp/career-ons-workflow-practice/run-5/verification.json)也验证了排队时等待不提前放行；集成测试另覆盖容量1与3。


该事件冻结的决策数值（均为 estimated）：

| choice | O | N | S |
| --- | ---: | ---: | ---: |
| 1 | 0 | 1 | 0 |
| 2 | 0 | -2 | 0 |
| 3 | 0 | 0 | 0 |

read-context 的接口只包含选项编号、数值、来源、可选择状态和提示；事件/选项文字泄漏由专项测试检查。H/W/R尚未接入，D不翻译。

## 实测中修正的问题

1. swarmflow 的 script_path 必须为绝对路径；workflow 直接载入角色 markdown 并替换绝对脚本路径。
2. “空回复”可能被模型写成“（空）”；改成 structured_output({}) 控制结束，框架立即结束 worker。
3. S连续多题回答0会触发框架相同工具参数死循环检测；新增 question_id，既区分正常步骤，也拒绝重放旧题答案。
4. Leader 即使收到静默指令仍可能输出阶段说明；manifest 的 silent_team 显式启用运行层 rail，在发送前抑制文字，保留工具和控制信号。移植至其他宿主需要安装同等 rail。
5. 禁用框架自动重试，单个 worker 最长600秒。失败、超时或缺失凭据阻塞后续阶段，不重复执行游戏动作。

## 证据与回归

- [结构化验收结果](/tmp/career-ons-workflow-practice/run-6/verification.json)
- [原始通信帧](/tmp/career-ons-workflow-practice/run-6/frames.jsonl)
- [工具执行与任务结束日志](/tmp/career-ons-workflow-practice/run-6/runtime-execution.log)
- [最终状态](/tmp/career-ons-workflow-practice/run-6/notebooks/workflow-state.json)
- [完整测试输出](/tmp/ons-final-suite.txt)：622 passed，5项依赖库弃用警告。

同目录归档了本轮 notebooks 与 workflow journals。游戏session为 `3bf4be4849ce496080cdd6a7c1ff7fb9`；Jiuwen会话为 `ons-workflow-practice-6`。仓库初始 session.json 保持 null。

H/W/R 后续建议拆成两个 teammate：analyse_HW 连续完成 H、W 两套独立问卷，analyse_R 专门处理隐患；各指标仍保留独立结果与凭据。该建议尚未做分组精度对照实验。

最后还校验了用户固定比例表的展示舍入：L7 的 S/N=210/80 显示2.63，内部保留精确分数；封顶与反馈不改变比例。
