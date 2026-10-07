# 【华为 openJiuwen】职场长程生存与晋升挑战（CCF BDCI 2026）

[![Documentation](https://img.shields.io/badge/赛题文档-blue?style=for-the-badge&logo=readthedocs&link=https%3A%2F%2Fcareer-emulator.readthedocs.io%2Fen%2Flatest%2Findex.html)](https://career-emulator.readthedocs.io) [![GitCode Dev](https://img.shields.io/badge/%E5%8F%82%E8%B5%9B%E7%94%A8GitCode%E4%BB%93-brown?style=for-the-badge&logo=GitCode)](https://gitcode.com/SushiNinja/CareerSim-BDCI26) [![GitHub Dev](https://img.shields.io/badge/%E5%8F%82%E8%B5%9B%E7%94%A8GitHub%E4%BB%93-black?style=for-the-badge&logo=GitHub)](https://github.com/openJiuwen-ai/CareerSim-BDCI26)

本赛题是一个面向 Agent 的职场模拟游戏。你要扮演一名刚入职业务研发组的员工，在 48 个月里处理剧情事件、分配季度体力、选择主行动，并努力在健康、尊严、技能、人脉、产出与财富之间维系脆弱的平衡。

赛题官方页面：<https://www.xir.cn/competition/1165>

赛题文档网址：<https://career-emulator.readthedocs.io>

## 前置工具

本项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 依赖和虚拟环境，使用 `make` 简化常用操作。

<details>
<summary>安装 <code>make</code></summary>

#### Windows

使用包管理器 [Chocolatey](https://docs.chocolatey.org/en-us/choco/setup/#more-install-options)，执行 `choco install make` 安装 `make`。

#### macOS

若已安装 Xcode，可运行 `xcode-select --install` 安装 Xcode 命令行工具，其中包含 `make`。或使用包管理器 [Homebrew](https://brew.sh)，执行 `brew install make` 安装。

#### Linux

运行 `sudo apt update` 更新包列表，然后运行 `sudo apt install make` 安装 `make`；或安装 `sudo apt install build-essential`，它会安装包括 `make` 在内的常用开发工具。

</details>

## 快速开始

### 1. 安装依赖

```bash
make sync
```

该命令会安装 [career-emulator](https://pypi.org/project/career-emulator/)、`uv`，并同步本项目的全部依赖。

当前固定使用 [WorkSwarm 0.2.8b1（JiuwenSwarm 新包名）](https://github.com/openJiuwen-ai/jiuwenswarm/releases/tag/v0.2.8.beta1)
及该版本指定的 openJiuwen `8f9f112698d5580b749158b21b1a89a54e16a8f9`（版本号 `0.1.19`），支持 Python 3.11–3.13。
`jiuwenswarm-*` 命令和 Python 导入名仍保持不变。`uv.lock` 固定完整依赖及官方 wheel 的 SHA256；
使用 `uv sync --locked` 复现安装，openJiuwen 源码只需首次构建，后续复用 uv 缓存。
保留并更新了 coach 的兼容补丁；每次 setup 会幂等应用。会话 metadata 原子写入及按 run 隔离 journal 已使用上游实现。
新环境测试前先执行 `uv run career-emulator update --source distribution --split dev` 初始化开发数据集。

> **建议**：[jiuwenswarm](https://openjiuwen.com/jiuwenswarm) 和 [career-emulator](https://pypi.org/project/career-emulator/) 都通过当前仓库目录的 [uv](https://docs.astral.sh/uv/) 虚拟环境管理，所有命令统一使用 `uv run` 前缀调用。请避免将它们安装到全局 Python 环境或其他虚拟环境中，以免冲突。

### 2. 配置环境

先把 `.env.example` 复制为 `.env`，填入 API 配置：

```bash
cp .env.example .env
```

`.env` 示例包含以下变量：

```bash
API_BASE=""
API_KEY=""
MODEL_NAME="deepseek-flash"
```

Jiuwen Player 在 coach、play、benchmark 和其他测试中统一使用 `deepseek-flash`。Runner 通过 `JIUWEN_PLAYER_MODEL` 固定模型，`.env` 中的 `MODEL_NAME` 仅保持示例一致，不覆盖它；reload 时也会同步服务进程中的模型配置。

然后运行 setup，它会自动初始化 JiuwenSwarm 命名实例 `career_emu`，并将上述配置写入实例目录：

```bash
make setup
```

### 3. 启动 JiuwenSwarm

在一个独立终端中启动 `career_emu` 实例：

```bash
make start-jiuwen
```

启动后可以随时查看各实例的运行状态：

```bash
uv run jiuwenswarm-start --list
```

输出示例：

```text
INSTANCE     STATUS     PID     WORKSPACE                                          PORTS
--------------------------------------------------------------------------------
default      stopped  -       /Users/you/.jiuwenswarm                              18092/19000/19001/5173
career_emu   running  58987   /Users/you/.jiuwenswarm-instances/career_emu         19092/20000/20001/6173
```

其中 `PORTS` 列的最后一个端口是前端 UI 地址。例如上面 `career_emu` 实例的端口为 `6173`，在浏览器访问 `http://localhost:6173` 即可打开 JiuwenSwarm 网页端界面。

#### 推荐：安装为用户级自动恢复服务

`career_emu` 是承载 AgentServer、Gateway、WebChannel 和 MCP 子进程的命名 JiuwenSwarm 实例。推荐交给 `systemd --user` 管理，避免终端关闭或单个子进程退出导致比赛失败：

```bash
make service-install
make service-status
```

服务会在启动前刷新 MCP 配置，把项目 `.venv/bin` 加入 PATH，并在异常退出后自动重启。查看实时日志：

```bash
make service-logs
```

手动重启使用 `make service-restart`；安装服务后不要再同时运行 `make start-jiuwen`，避免两个实例争用端口。

`make play` 会先停止专用服务、删除旧 `.agent_teams` view，再启动服务并创建新团队；因此大幅修改 solution 后也不会沿用旧团队成员、任务或消息。若尚未安装用户服务，先运行 `make service-install`。

### 4. 开始一局新比赛

```bash
make play
```

`make play` 每次都会通过 `new_game` 创建新的比赛 `session_id`，并在安全停止服务后重建专用的 `.agent_teams/team.db`。只做第 1 月协作验收时运行：

```bash
make play PLAY_ARGS='--stop-after-month 1 --timeout-s 900'
```

### 5. 查看分数

```bash
make score
```

### 6. 生成可读战报

从最近一次运行的 `events-*.jsonl` 生成 Markdown 复盘：

```bash
make replay
```

### Coach 单步控制

若需要在比赛途中逐事件检查 JiuwenSwarm 的表现，可用不暴露给 Player 的旁路
`career_sim_runner.coach` 控制器。新开的 coach 比赛默认只推进一个事件，之后每次
显式执行一步；这些命令不会注册为 Career Emulator MCP 工具：

```bash
make coach-start                              # 输出新的 jiuwen_player_session_id
make coach-step JIUWEN_PLAYER_SESSION_ID=<id> # 只推进一个事件
make coach-inspect JIUWEN_PLAYER_SESSION_ID=<id>            # 查看当前题目、近期决策和状态迁移
make coach-inspect JIUWEN_PLAYER_SESSION_ID=<id> INSPECT_ARGS="-i 2 -j 4"  # 查看第 2 到第 4 轮
make coach-withdraw JIUWEN_PLAYER_SESSION_ID=<id>           # 撤回最近一次 player step
make coach-withdraw JIUWEN_PLAYER_SESSION_ID=<id> WITHDRAW_ARGS="--checkpoint-round 12" # 回到指定轮次
```

`make play` 的原有连续比赛流程保持不变。Coach 控制器的详细参数见
[`career_sim_runner/coach/README.md`](career_sim_runner/coach/README.md)。

Observation Translator 可用固定顺序的官方事件节点单独评测，不执行任何游戏动作：

```bash
make translation-benchmark TRANSLATION_BENCHMARK_ARGS="--limit 20"
```

输出写入 `.career_sim_runner/translation_benchmark/`；详细格式见
[`career_sim_runner/translation_benchmark/README.md`](career_sim_runner/translation_benchmark/README.md)。

## 赛题玩法

每个月，系统会推进时间、生成剧情事件，并让你基于当前状态做选择。每逢季度末，你还会拿到 3 点 `Energy` 处理体力行动，再额外选择 1 个季度主行动。每 6 个月进行一次绩效评估并结算绩效奖金。

- **剧情选择** 决定短期状态变化，也会改变后续事件走向。
- **季度行动** 是你主动调节职业轨迹的主要抓手。
- **长线规划** 很多风险不会当月就爆，往往是攒着以后一起算总账。

整局游戏总长 48 个月。活满 48 个月后，系统会综合职级、财富、身心状态、技能、风险控制和同事关系给出结局评分（从 D 到 S）。

## 交付件

参赛团队需要使用 [JiuwenSwarm](https://openjiuwen.com/jiuwenswarm) 构建 Agent，并开发一个或多个 [Skill](https://agentskills.io) 来辅助 Agent 进行游戏。最终把 Skill 文件夹统一打包成单个 `zip` 作为交付件。

提交目录结构如下：

```text
solution/
  manifest.json
  README.md
  design/
    skills_design.md
    decision_report.md
  skills/
    environment-perception/
      SKILL.md
    risk-analysis/
      SKILL.md
    ...
```

其中 `manifest.json` 记录团队名和提交件元信息：

```json
{
  "team": "your-team-name",
  "name": "your-submission-name",
  "mode": "agent",
  "instruction": ""
}
```

`mode` 字段决定 [JiuwenSwarm](https://openjiuwen.com/jiuwenswarm) 以何种模式运行你的 Agent，支持以下几种：

| mode | 说明 |
|---|---|
| `agent` | 单 Agent 模式。支持通用工具、技能与 MCP 的调用，并挂载任务规划、子代理编排与技能演进等能力；记忆为被动模式，按需读写。|
| `team` | 团队协作模式。启动多 Agent 协作，Leader 统筹任务拆解与调度，Teammate 按角色分工并行执行。团队成员继承工具和 MCP 能力，适合需要多智能体协同的场景。|

## 一些方便的 Makefile 命令

| 说明 | Makefile 快捷命令 |
|---|---|
| 通过 `uv` 安装项目依赖 | `make sync` |
| 初始化环境、配置 JiuwenSwarm 实例 | `make setup` |
| 启动 JiuwenSwarm 服务（`career_emu` 实例） | `make start-jiuwen` |
| 挂载当前 `solution` 文件夹内的技能、并连接 JiuwenSwarm 进行一局游戏 | `make play` |
| 重新读取上次运行的结局分数 | `make score` |

也可直接使用 `career_sim_runner` 的 CLI 接口（以验证 `solution` 内容的 validate 命令为例）：

```bash
uv run python -m career_sim_runner validate --submission solution
```

## 运行产物

每次运行产生的输出均会出现在 `.career_sim_runner/career_emu/` 目录下：

```text
.career_sim_runner/career_emu/
  active_install.json        # 当前挂载的提交件
  career_emulator.sqlite3    # 共享状态数据库
  emulator_logs/             # setup/兼容模式下的默认日志目录
  outputs/
    <solution_name>/<timestamp>/ # 每次运行的 transcript、events、score 与模拟器日志
      transcript-*.log       # 对话记录
      events-*.jsonl         # 结构化事件流
      <session_id>.log       # Career Emulator 模拟器日志
      score_report.json      # 结局评分报告
```

## 比赛交互方式

比赛通过 [career-emulator](https://pypi.org/project/career-emulator/) 提供的 [MCP](https://modelcontextprotocol.io) 服务与 Agent 交互。常用能力包括：

- `new_game`：开一局新游戏。
- `observe(session_id)`：读取当前状态、当前事件和可选项。
- `take_action(session_id, choice, notes)`：执行选择，并把备注写进日志。
- `show_employee_handbook()`：获取公开手册。
- `check_latest_logs(session_id)`：读取最近日志。
