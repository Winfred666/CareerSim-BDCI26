---
name: simcareer-formal-judger
description: CareerSim 正式、运行时间较长的完整比赛运行与复盘 skill：执行官方连续比赛链路，归档可核查证据，并将结果与简洁分析总结写入 result.md。
---

# SimCareer 正式比赛判定器

仅在需要正式比赛成绩、完整长程运行或赛后复盘时使用。本技能的 compete 语义是官方连续比赛入口：必须让一局按正常 Agent 流程推进到真实终局，再评分、生成战报和复盘；smoke run、coach 单步、只跑部分月份或只读取旧分数都不算正式比赛。

## 正式运行流程

1. 默认仓库为 `/home/openclaw-svc/Desktop/CareerSim/CareerSim-BDCI26`；只有用户明确指定时才切换。运行前确认提交件可用，但保持当前 `solution/` 不变。
2. 严禁读取、打印、复制或修改任何 `.env`。运行器只可继承已配置的环境；不得把环境内容写入日志或报告。
3. 从仓库根目录通过 `scripts/judge_run.py` 严格顺序执行：

   ```bash
   python3 /home/openclaw-svc/Desktop/CareerSim/.codex/skills/simcareer-formal-judger/scripts/judge_run.py
   ```

   脚本内部执行官方 `make play`、`make score`、`make replay`；其中正式 `make play` 必须以 `EMULATOR_SPLIT=test` 运行，使 `career-emulator update --source distribution --split test` 使用裁判数据集。普通开发运行保留 Makefile 的 `dev` 默认值。这是正式长时间运行，不能为了省时改成 coach、smoke、`--continue`，也不能静默追加未被官方入口支持的 `--competition` 参数。运行期间等待真实终局；不要中途停止或用旧产物补齐本局。
4. `make play` 失败、超时、达到 continuation 上限或未进入真实终局时，仍归档本次新产生的 transcript/events/log；结果必须明确标记未完成，不能把退出码 0 或旧分数解释为 48 个月通关。
5. 运行结束后核对本次归档的 `score_report.json`、events、transcript、replay 和命令日志。完整比赛的最低验收条件是结构化 `ending_score.completed == true` 且 `survival_months >= 48`；否则 `result.md` 必须写明实际生存月数、终止原因和未完成状态。
6. `result.md` 必须含精确标题 `## result`，记录三段命令退出码、session、结局、等级、竞赛折算分、生存月数、完整性判定和结构化证据路径；随后用本次 score/events/transcript 可核查内容写一个简短的 `### 分析总结`，至少说明完成性、主要得分维度强弱或缺失信息，以及对策略结果的直接判断。不得从自然语言臆造分数、月份或隐藏规则。
7. `review.md` 只保留一个可执行的下一步建议，并保留精确标题 `### very concise and brief suggested strategy next move`。运行链路失败时先建议修复链路；正式局未完成时不得把策略优化写成既成结论。

## 已有产物归档

用户只要求整理已有官方运行产物时，使用 `--existing-run`，不要再次启动游戏：

```bash
python3 /home/openclaw-svc/Desktop/CareerSim/.codex/skills/simcareer-formal-judger/scripts/judge_run.py \
  --existing-run /path/to/.career_sim_runner/career_emu/outputs/<submission>/<timestamp>
```

此模式必须在 `result.md` 标注“已有运行产物”，保留原始完成性和退出信息；它不是一次新的正式比赛，也不能把失败或 smoke run 改写为正式完成结果。

## 游戏边界与完整性

- Career Emulator MCP 是唯一允许推进或读取游戏状态的边界。不得用 shell、Python、SQLite、文件写入、数据集修改、环境变量修改或直接 import 模拟器改变状态。
- 评测期间不得批准 Agent 修改提交件、`career_emulator.sqlite3`、模拟器数据集、JiuwenSwarm 配置或任何 `.env`。优先使用 `permissions.enabled: true`、`permission_mode: strict`，拒绝 shell/文件写工具，仅允许 MCP 游戏工具和只读技能加载。
- 高可信运行应记录运行前后数据集与 JiuwenSwarm 配置哈希；归档只保存哈希，不保存 `.env` 内容。
- 只以 `score_report.json` 的 `ending_score` 和 events 为准。`observe` 返回的 choice 编号必须原样传给 `take_action`；不得翻译、重排或猜测选项。

## 归档布局

每次运行写入 `/home/openclaw-svc/Desktop/CareerSim/results/<YYYYMMDDTHHMMSSZ>/`，至少包含：

- `solution/`：排除 `.env*`、缓存和生成文件的提交快照；
- `result.md`：固定 `## result`、完整性判定和简洁分析总结；
- `review.md`：固定 `## review` 及一条下一步建议；
- `score_report.json`、replay Markdown、events JSONL、transcript 与 `command-*.log`（存在则归档）；
- `integrity.json`：运行前后数据集及 JiuwenSwarm YAML 配置哈希。

以脚本输出的 `archive_dir` 作为交付路径。时间戳冲突时追加后缀，不覆盖已有归档。
