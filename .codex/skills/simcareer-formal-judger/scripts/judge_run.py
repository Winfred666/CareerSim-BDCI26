#!/usr/bin/env python3
"""Run one formal CareerSim competition pass and archive immutable evidence.

The helper intentionally delegates game execution to the repository's Makefile
and treats the resulting transcript/events/score as evidence. It never reads a
repository .env file and excludes env files from the solution snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_REPO = Path("/home/openclaw-svc/Desktop/CareerSim/CareerSim-BDCI26")
DEFAULT_RESULTS = Path("/home/openclaw-svc/Desktop/CareerSim/results")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique_archive_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / _utc_stamp()
    suffix = 1
    while candidate.exists():
        candidate = root / f"{_utc_stamp()}-{suffix:02d}"
        suffix += 1
    candidate.mkdir()
    return candidate


def _safe_env() -> dict[str, str]:
    """Return the inherited environment without opening or parsing .env files."""
    # Keep the configured model/runtime environment available to make play
    # behave exactly as the repository command does. The helper itself never
    # reads .env; shell/environment loading remains JiuwenSwarm's concern.
    return dict(os.environ)


def _hash_path(path: Path) -> str | None:
    """Hash one file or directory without following nested symlinks."""
    if not path.exists():
        return None
    target = path.resolve() if path.is_symlink() else path
    digest = hashlib.sha256()
    if target.is_file():
        digest.update(target.read_bytes())
        return digest.hexdigest()
    if not target.is_dir():
        return None
    for child in sorted(target.rglob("*")):
        if child.is_symlink() or not child.is_file():
            continue
        digest.update(str(child.relative_to(target)).encode("utf-8"))
        digest.update(child.read_bytes())
    return digest.hexdigest()


def _integrity_snapshot(repo: Path) -> dict[str, Any]:
    """Capture hashes only; never read or hash any .env file."""
    dataset_candidates = sorted((repo / ".venv" / "lib").glob("python*/site-packages/career_emulator/data/dataset"))
    dataset = dataset_candidates[0] if dataset_candidates else None
    config = Path.home() / ".jiuwenswarm-instances" / "career_emu" / "config" / "config.yaml"
    return {
        "dataset": {"path": str(dataset.resolve()) if dataset and dataset.exists() else None, "sha256": _hash_path(dataset) if dataset else None},
        "jiuwenswarm_config": {"path": str(config), "sha256": _hash_path(config)},
    }


def _run_make(
    target: str,
    repo: Path,
    log_path: Path,
    stdin_data: str = "",
    make_vars: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run one Makefile target, stream output, and persist the complete log."""
    command = ["make"]
    command.extend(f"{key}={value}" for key, value in (make_vars or {}).items())
    command.append(target)
    started = datetime.now(timezone.utc).isoformat()
    lines = [f"$ {' '.join(command)}", f"started_at: {started}"]
    print(f"\n=== {' '.join(command)} ===", flush=True)
    proc = subprocess.Popen(
        command,
        cwd=repo,
        env=_safe_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    if proc.stdin is not None and stdin_data:
        # make replay uses an interactive live renderer. A newline buffer lets
        # it advance without hanging while keeping the requested target.
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except BrokenPipeError:
            # The target may exit early when no events log exists.
            pass
    elif proc.stdin is not None:
        proc.stdin.close()
    for line in proc.stdout:
        lines.append(line.rstrip("\n"))
        print(line, end="", flush=True)
    return_code = proc.wait()
    lines.append(f"exit_code: {return_code}")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return return_code, "\n".join(lines)


def _extract_path(text: str, label: str, repo: Path) -> Path | None:
    match = re.findall(rf"^{re.escape(label)}:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    raw = Path(match[-1].strip())
    return raw if raw.is_absolute() else (repo / raw).resolve()


def _newest(paths: Iterable[Path], not_before: float) -> Path | None:
    candidates = [path for path in paths if path.is_file() and path.stat().st_mtime >= not_before]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _find_current_artifacts(
    repo: Path,
    started_epoch: float,
    score_output: str,
    play_output: str,
    play_exit_code: int,
) -> dict[str, Path]:
    output_root = repo / ".career_sim_runner" / "career_emu" / "outputs"
    # A report path printed by the play command is authoritative even when the
    # run exits non-zero (for example, a partial transcript). Do not infer a
    # current report from `make score` after a failed play: that target can
    # rewrite an older run and would make stale evidence look fresh.
    report = _extract_path(play_output, "report_path", repo)
    if report is None and play_exit_code == 0:
        report = _extract_path(score_output, "report_path", repo)
    if report is not None and report.is_file():
        output_dir = report.parent
    else:
        score_candidates = list(output_root.glob("**/score_report.json"))
        score_file = _newest(score_candidates, started_epoch)
        if score_file is not None:
            output_dir = score_file.parent
        else:
            # A failed drive may still have a fresh transcript/events log but
            # no score_report.json. Select only a run directory with evidence
            # written after this invocation began.
            evidence_dirs = {
                path.parent
                for pattern in ("events-*.jsonl", "transcript-*.log")
                for path in output_root.glob(f"**/{pattern}")
                if path.is_file() and path.stat().st_mtime >= started_epoch
            }
            output_dir = max(evidence_dirs, key=lambda path: path.stat().st_mtime) if evidence_dirs else None
    if output_dir is None or not output_dir.is_dir():
        return {}
    found: dict[str, Path] = {}
    for name, pattern in (
        ("score_report", "score_report.json"),
        ("events", "events-*.jsonl"),
        ("transcript", "transcript-*.log"),
        ("replay", "replay-*.md"),
    ):
        candidate = _newest(output_dir.glob(pattern), started_epoch) or _newest(output_dir.glob(pattern), 0)
        if candidate is not None:
            found[name] = candidate
    found["output_dir"] = output_dir
    return found


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _score_summary(score: dict[str, Any]) -> tuple[str, str, str, str]:
    ending = score.get("ending_score") if isinstance(score.get("ending_score"), dict) else {}
    def display(name: str) -> str:
        value = ending.get(name)
        return "unavailable" if value is None or value == "" else str(value)

    outcome = display("outcome")
    grade = display("grade")
    competition = display("competition_partial_score")
    survival = display("survival_months")
    return outcome, grade, competition, survival


def _formal_completion(score: dict[str, Any]) -> bool:
    """Return whether the structured report proves a complete formal game."""
    ending = score.get("ending_score") if isinstance(score.get("ending_score"), dict) else {}
    if ending.get("completed") is not True:
        return False
    try:
        return float(ending.get("survival_months")) >= 48
    except (TypeError, ValueError):
        return False


def _event_summary(events_path: Path | None) -> dict[str, Any]:
    """Summarize only structured MCP events for the formal result."""
    summary: dict[str, Any] = {
        "observations": 0,
        "actions": 0,
        "action_failures": 0,
        "first_month": None,
        "last_month": None,
    }
    if events_path is None or not events_path.is_file():
        return summary

    seen_call_ids: set[str] = set()
    seen_result_ids: set[str] = set()
    try:
        records = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return summary

    for line in records:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        node = payload
        if isinstance(payload.get("delta"), dict):
            node = payload["delta"]
        kind = str(record.get("kind") or "")
        tool_call = node.get("tool_call") if isinstance(node.get("tool_call"), dict) else {}
        tool_name = str(
            tool_call.get("name")
            or node.get("tool_name")
            or payload.get("tool_name")
            or ""
        )
        tool_id = str(
            tool_call.get("tool_call_id")
            or node.get("tool_call_id")
            or payload.get("tool_call_id")
            or ""
        )
        seen_ids = seen_call_ids if kind == "tool_call" else seen_result_ids
        if tool_id and tool_id in seen_ids:
            continue
        if tool_id:
            seen_ids.add(tool_id)

        if kind == "tool_call" and tool_name.endswith("take_action"):
            summary["actions"] += 1
            continue
        if kind != "tool_result" or not tool_name.endswith("observe"):
            if kind == "tool_result" and tool_name.endswith("take_action"):
                raw = node.get("raw_output")
                if isinstance(raw, dict):
                    result = raw.get("result")
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except json.JSONDecodeError:
                            result = None
                    if isinstance(result, dict) and result.get("success") is False:
                        summary["action_failures"] += 1
            continue

        summary["observations"] += 1
        raw = node.get("raw_output")
        if not isinstance(raw, dict):
            continue
        result = raw.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = None
        if not isinstance(result, dict):
            continue
        state = result.get("current_state")
        time_info = state.get("time") if isinstance(state, dict) else None
        month = time_info.get("current_month") if isinstance(time_info, dict) else None
        if isinstance(month, int):
            if summary["first_month"] is None:
                summary["first_month"] = month
            summary["last_month"] = month
    return summary


def _analysis_summary(score: dict[str, Any], events_path: Path | None) -> list[str]:
    """Build a short, evidence-based analysis for result.md."""
    ending = score.get("ending_score") if isinstance(score.get("ending_score"), dict) else {}
    complete = _formal_completion(score)
    events = _event_summary(events_path)
    if complete:
        completion = "已完成正式完整局（结构化 completed=true 且生存月数达到 48）。"
    else:
        survival = ending.get("survival_months", "未提供")
        reason = score.get("termination_reason") or ending.get("outcome") or "未提供"
        completion = f"未完成正式完整局（生存月数：{survival}；终止原因/结局：{reason}）。"

    dimensions = [item for item in ending.get("dimensions", []) if isinstance(item, dict)]
    scored_dimensions: list[tuple[float, str]] = []
    for item in dimensions:
        try:
            value = float(item["weighted_score"])
        except (KeyError, TypeError, ValueError):
            continue
        scored_dimensions.append((value, str(item.get("label") or item.get("name") or "未命名维度")))
    if scored_dimensions:
        scored_dimensions.sort()
        low_value, low_label = scored_dimensions[0]
        high_value, high_label = scored_dimensions[-1]
        dimension_line = (
            f"得分结构：优势维度为 {high_label}（加权 {high_value:g}），短板维度为 "
            f"{low_label}（加权 {low_value:g}）。"
        )
    else:
        dimension_line = "得分结构：本次没有可用的结构化维度，不能可靠判断分项强弱。"

    process_line = (
        f"过程证据：结构化 observe {events['observations']} 次、take_action {events['actions']} 次"
        f"；动作失败 {events['action_failures']} 次。"
    )
    strategy_line = (
        "策略判断：本局满足正式存活约束；后续优化应围绕最低加权维度和可核查决策轨迹。"
        if complete
        else "策略判断：本局未满足正式存活约束；应先处理导致终止的可核查风险或运行问题，再讨论分数提升。"
    )
    return [completion, dimension_line, process_line, strategy_line]


def _write_result(
    path: Path,
    *,
    repo: Path,
    archive: Path,
    exit_codes: dict[str, int],
    artifacts: dict[str, Path],
    score: dict[str, Any],
    source: str,
) -> None:
    outcome, grade, competition, survival = _score_summary(score)
    termination = str(score.get("termination_reason") or "未提供")
    session_id = str(score.get("session_id") or "未提供")
    complete = _formal_completion(score)
    events_path = artifacts.get("events")
    lines = [
        "# CareerSim 判定结果",
        "",
        "## result",
        "",
        f"- 判定时间（UTC）: `{datetime.now(timezone.utc).isoformat()}`",
        f"- 仓库: `{repo}`",
        f"- 归档目录: `{archive}`",
        f"- 产物来源: `{source}`",
        f"- make play 退出码: `{exit_codes.get('play', '未执行')}`",
        f"- make score 退出码: `{exit_codes.get('score', '未执行')}`",
        f"- make replay 退出码: `{exit_codes.get('replay', '未执行')}`",
        f"- 游戏 session_id: `{session_id}`",
        f"- 终止原因: `{termination}`",
        f"- 游戏结局: `{outcome}`",
        f"- 等级: `{grade}`",
        f"- 竞赛折算分: `{competition}`",
        f"- 生存月数: `{survival}`",
        f"- 正式完整性判定: `{'已完成' if complete else '未完成'}`",
        "",
        "### 产物",
        "",
    ]
    if artifacts:
        lines.extend(f"- {name}: `{path}`" for name, path in sorted(artifacts.items()))
    else:
        lines.append("- 本次运行没有检测到可归档产物")
    lines.extend(
        [
            "",
            "### 分析总结",
            "",
            *_analysis_summary(score, events_path),
            "",
            "### 完整性说明",
            "",
            "本报告只记录仓库判定器的命令结果和结构化产物。Agent 的自然语言不能证明游戏完成；应以 score_report.json 与 events JSONL 为准。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _suggest_next_move(exit_codes: dict[str, int], score: dict[str, Any]) -> str:
    if exit_codes.get("play", 1) != 0:
        return "先修复 `make play` 阶段的失败原因，再进行策略调参。"
    if not _formal_completion(score):
        ending = score.get("ending_score") if isinstance(score.get("ending_score"), dict) else {}
        reason = str(score.get("termination_reason") or ending.get("outcome") or "未提供")
        return f"本局未完成正式 48 个月（终止原因：{reason}）；先定位并修复该终止路径，再讨论分数提升。"
    ending = score.get("ending_score") if isinstance(score.get("ending_score"), dict) else {}
    dimensions = ending.get("dimensions")
    if isinstance(dimensions, list) and dimensions:
        def weighted_value(item: dict[str, Any]) -> float:
            try:
                return float(item.get("weighted_score", float("inf")))
            except (TypeError, ValueError):
                return float("inf")

        lowest = min(
            (item for item in dimensions if isinstance(item, dict)),
            key=weighted_value,
            default=None,
        )
        if lowest is not None:
            label = str(lowest.get("label") or "the weakest dimension")
            return f"下一次同类决策优先补强 `{label}`，同时保留生存与晋升前置条件。"
    return "先用战报定位第一个可避免的损失，把它转成下一步决策规则；优化前先保证生存前置条件。"


def _write_review(path: Path, *, exit_codes: dict[str, int], score: dict[str, Any]) -> None:
    path.write_text(
        "\n".join(
            [
                "# CareerSim 判定复核",
                "",
                "## review",
                "",
                "### very concise and brief suggested strategy next move",
                "",
                _suggest_next_move(exit_codes, score),
                "",
                "该建议只基于本次归档的结构化结果；在调整策略前，应先检查同目录 replay、events 与 transcript。",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _copy_solution(source: Path, destination: Path) -> None:
    ignored_names = shutil.ignore_patterns(".env", ".env.*", ".git", "__pycache__", "*.pyc", ".career_sim_runner")
    shutil.copytree(source, destination, ignore=ignored_names, symlinks=False)


def _copy_existing_artifacts(source_dir: Path, archive: Path) -> dict[str, Path]:
    """Copy an already-produced runner directory into a new archive."""
    copied: dict[str, Path] = {}
    for key, pattern in (
        ("score_report", "score_report.json"),
        ("events", "events-*.jsonl"),
        ("transcript", "transcript-*.log"),
        ("replay", "replay-*.md"),
    ):
        matches = sorted(source_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
        if not matches:
            continue
        source = matches[-1]
        destination = archive / source.name
        shutil.copy2(source, destination)
        copied[key] = destination
    return copied


def _render_existing_replay(repo: Path, events_path: Path, archive: Path) -> tuple[int, Path | None, str]:
    """Render a non-interactive Markdown battle report from existing events."""
    destination = archive / "replay.md"
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "career_sim_runner",
        "replay",
        "--events",
        str(events_path),
        "--output",
        str(destination),
    ]
    result = subprocess.run(command, cwd=repo, env=_safe_env(), capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    return result.returncode, destination if destination.is_file() else None, output


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and archive one formal CareerSim competition pass")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--solution", type=Path, default=None, help="Submission directory; defaults to <repo>/solution")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument(
        "--existing-run",
        type=Path,
        default=None,
        help="Archive an existing runner output directory instead of starting make play",
    )
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    solution = (args.solution or repo / "solution").expanduser().resolve()
    if not repo.is_dir():
        parser.error(f"repository does not exist: {repo}")
    existing_run = args.existing_run.expanduser().resolve() if args.existing_run else None
    if existing_run is not None and not existing_run.is_dir():
        parser.error(f"existing runner output directory does not exist: {existing_run}")
    if not solution.is_dir() and existing_run is None:
        parser.error(f"solution directory does not exist: {solution}")
    if solution == repo or repo not in solution.parents:
        parser.error("solution must be inside the repository")

    archive = _unique_archive_dir(args.results_root.expanduser().resolve())
    integrity_before = _integrity_snapshot(repo)
    exit_codes: dict[str, int] = {}
    copied: dict[str, Path] = {}
    source_description = "make play → make score → make replay"

    if existing_run is not None:
        source_description = f"已有 runner 产物（--existing-run {existing_run}）"
        # Prefer the exact submission path recorded by the score report. If it
        # is unavailable, fall back to the caller-selected current solution.
        existing_score = _load_json(existing_run / "score_report.json")
        recorded_solution = Path(str(existing_score.get("submission_dir") or ""))
        source_solution = recorded_solution if recorded_solution.is_dir() else solution
        if not source_solution.is_dir():
            parser.error("existing run has no usable submission directory")
        _copy_solution(source_solution, archive / "solution")
        copied.update(_copy_existing_artifacts(existing_run, archive))
        if "events" in copied and "replay" not in copied:
            replay_code, replay_path, replay_output = _render_existing_replay(repo, copied["events"], archive)
            exit_codes["replay"] = replay_code
            (archive / "command-replay-existing.log").write_text(replay_output, encoding="utf-8")
            if replay_path is not None:
                copied["replay"] = replay_path
        else:
            exit_codes["replay"] = 0 if "replay" in copied else 1
        exit_codes["play"] = int(existing_score.get("play_exit_code", 1))
        exit_codes["score"] = 0 if "score_report" in copied else 1
        output_dir = existing_run
    else:
        _copy_solution(solution, archive / "solution")
        started_epoch = time.time()
        outputs: dict[str, str] = {}

        # Keep curation/development on the Makefile default (`dev`), but make
        # the formal competition pass deterministic against the held-out test
        # split. The play target then runs the exact same official chain.
        exit_codes["play"], outputs["play"] = _run_make(
            "play",
            repo,
            archive / "command-play.log",
            make_vars={"EMULATOR_SPLIT": "test"},
        )
        exit_codes["score"], outputs["score"] = _run_make("score", repo, archive / "command-score.log")
        # The Makefile's replay target is interactive (it advances on Enter).
        # Feed a generous newline buffer so the requested target is non-blocking.
        exit_codes["replay"], outputs["replay"] = _run_make("replay", repo, archive / "command-replay.log", "\n" * 4096)

        artifacts = _find_current_artifacts(
            repo,
            started_epoch,
            outputs["score"],
            outputs["play"],
            exit_codes["play"],
        )
        output_dir = artifacts.get("output_dir")
        if output_dir is not None:
            for key in ("score_report", "events", "transcript", "replay"):
                source = artifacts.get(key)
                if source is None or not source.is_file():
                    continue
                destination = archive / source.name
                shutil.copy2(source, destination)
                copied[key] = destination

    copied["output_dir"] = output_dir if output_dir is not None else archive

    score_path = copied.get("score_report")
    score = _load_json(score_path)
    integrity_after = _integrity_snapshot(repo)
    (archive / "integrity.json").write_text(
        json.dumps(
            {
                "before": integrity_before,
                "after": integrity_after,
                "changed": integrity_before != integrity_after,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_result(
        archive / "result.md",
        repo=repo,
        archive=archive,
        exit_codes=exit_codes,
        artifacts=copied,
        score=score,
        source=source_description,
    )
    _write_review(archive / "review.md", exit_codes=exit_codes, score=score)

    print(f"\narchive_dir: {archive}")
    print(f"make_exit_codes: {json.dumps(exit_codes, sort_keys=True)}")
    if score_path:
        print(f"score_report: {score_path}")
    commands_ok = all(code == 0 for code in exit_codes.values())
    formal_complete = _formal_completion(score)
    return 0 if commands_ok and formal_complete else 1


if __name__ == "__main__":
    sys.exit(main())
