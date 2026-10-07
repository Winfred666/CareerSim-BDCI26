"""Run a fixed ordered translation stream through one fresh Jiuwen session."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import runpy
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import websockets
import yaml

from career_sim_runner.constants import REPO_ROOT
from career_sim_runner.models import TokenUsage
from career_sim_runner.transcript import StreamCollector
from career_sim_runner.translation_benchmark.cases import TranslationCase, load_cases
from career_sim_runner.translation_benchmark.dictionary import (
    activation_counts, activation_tsv, dictionary_rows, numbered_dictionary, parse_activations,
)
from career_sim_runner.translation_benchmark.report import render_events, render_report
from career_sim_runner.translation_benchmark.scoring import (
    CaseScore, directional_error_summary, normalize_metric, parse_json_object, score_case,
)
from career_sim_runner.translation_benchmark.storage import Ledger
from career_sim_runner.ws_client import build_chat_envelope

OUTPUT_ROOT = REPO_ROOT / ".career_sim_runner" / "translation_benchmark"
SCORING_MODE = "deterministic_structural"
DEV_BATCH_SIZE = 4
DEV_BATCH_WORKERS = 4
DELIVERY_OPPORTUNITY_RULE = (
    "只有题面明确本职已排期工作或交付受到挤占、延期、落空，才推断 O 负向机会成本；"
    "不能仅凭行动耗时估 O-?。"
)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return cleaned[:32] or "seed"


def allocate_output(root: Path, seed: str) -> tuple[str, Path]:
    """Allocate a collision-resistant run directory."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / f"{stamp}-{_slug(seed)}"
    output = base
    suffix = 2
    while output.exists():
        output = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    output.mkdir(parents=True)
    return output.name, output


def resolve_dictionary(solution: Path, skill_id: str) -> Path:
    """Return the selected solution's event translation dictionary."""
    path = solution.resolve() / "skills" / skill_id / "notebooks" / "event-translator-dictionary.tsv"
    if not path.is_file():
        raise FileNotFoundError(f"Translation dictionary not found: {path}")
    return path


def resolve_translation_rules(solution: Path, skill_id: str) -> tuple[Path, Path, str]:
    """Extract translation rules from linked instructions or a registered role."""
    skill_root = solution.resolve() / "skills" / skill_id
    skill_path = skill_root / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(f"Solution skill instructions not found: {skill_path}")
    skill_document = skill_path.read_text(encoding="utf-8")
    frontmatter_match = re.match(r"(?s)^---\s*\n(.*?)\n---(?:\s*\n|$)", skill_document)
    if frontmatter_match is None:
        raise ValueError(f"Cannot parse YAML frontmatter from: {skill_path}")
    frontmatter = yaml.safe_load(frontmatter_match.group(1))
    roles = []
    if isinstance(frontmatter, dict):
        metadata = frontmatter.get("metadata") or {}
        roles = frontmatter.get("roles", metadata.get("roles", []) if isinstance(metadata, dict) else [])
    if any(isinstance(role, dict) and str(role.get("id", "")).startswith("analyse_") for role in roles):
        raise ValueError("Questionnaire workflow requires questionnaire replay; dictionary benchmark is incompatible")
    translator = next(
        (role for role in roles if isinstance(role, dict) and role.get("id") == "observation-translator"),
        None,
    )
    if translator is not None and isinstance(translator.get("path"), str):
        references = [translator["path"]]
    else:
        links = re.findall(r"\[([^\]\n]*)\]\(([^)\s]+\.md)(?:#[^)\s]*)?\)", skill_document)
        references = [
            ref for title, ref in links
            if re.search(r"翻译|translat", title, re.IGNORECASE) and ":" not in ref
        ]
    matches = []
    paths = dict.fromkeys((skill_root / ref).resolve() for ref in references)
    for path in paths:
        try:
            path.relative_to(skill_root)
        except ValueError as exc:
            raise ValueError(f"Instruction path escapes the skill directory: {path}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"Skill instructions not found: {path}")
        document = path.read_text(encoding="utf-8")
        steps = re.finditer(
            r"(?ms)^(?:\#{1,6}[ \t]+)?\d+(?:\.\d+)*\.?[ \t]+(.*?)"
            r"(?=^(?:\#{1,6}[ \t]+)?\d+(?:\.\d+)*\.?[ \t]+|^##[ \t]+|\Z)",
            document,
        )
        for step in steps:
            paragraphs = step.group(1).strip().split("\n\n")
            for index, paragraph in enumerate(paragraphs):
                if "event-translator-dictionary.tsv" in paragraph:
                    matches.append((path, "\n\n".join(paragraphs[index:])))
                    break
    if len(matches) != 1:
        raise ValueError(f"Expected one dictionary-based translation step in: {skill_path}; found {len(matches)}")
    path, rules = matches[0]
    # The observe step also carries promotion and cap-notebook maintenance.
    # These paragraphs do not govern metric translation and distract the
    # offline translator from the current event.
    non_translation_prefixes = (
        "晋升比例模型由", "封顶依据本局", "每次事件后，", "这次复核向",
    )
    rules = "\n\n".join(
        paragraph for paragraph in rules.split("\n\n")
        if not paragraph.startswith(non_translation_prefixes + ("固定菜单直接使用", "翻译完全部选项后"))
    )
    rules = re.sub(
        r"`?<notebooks_directory>/event-translator-dictionary\.tsv`?",
        "下方 `event-translator-dictionary.tsv` 内容",
        rules,
    )
    return skill_path, path, rules


def _prompt_template(name: str) -> str:
    return (Path(__file__).resolve().parent / name).read_text(encoding="utf-8").strip()


def _public_candidate_rows(
    dictionary_path: Path, cases: list[TranslationCase]
) -> tuple[dict[str, list[str]], dict[str, dict[int, list[str]]]]:
    """Reuse the live observe refresher's whole-case and per-option hints."""
    script = dictionary_path.parent.parent / "scripts" / "refresh-redline.py"
    if not script.exists():
        return {}, {}
    matchers = runpy.run_path(str(script), run_name="translation_benchmark")
    matcher = matchers.get("dictionary_matches")
    option_matcher = matchers.get("dictionary_matches_by_choice")
    if not callable(matcher) or not callable(option_matcher):
        return {}, {}
    source_lines = dictionary_path.read_text(encoding="utf-8").splitlines()
    line_by_row = {row: index for index, row in enumerate(source_lines, start=1)}

    def annotate(matches: list[str]) -> list[str]:
        return [f"L{line_by_row[row]} {row}" for row in matches if row in line_by_row]

    return (
        {case.case_id: annotate(matcher(dictionary_path.parent, case.observation)) for case in cases},
        {case.case_id: {choice: annotate(rows) for choice, rows in
                        option_matcher(dictionary_path.parent, case.observation).items()}
         for case in cases},
    )


def render_prompt(case: TranslationCase, dictionary: str, translation_rules: str, *, first: bool,
                  dev_full: bool = False, candidate_rows: list[str] | None = None) -> str:
    """Render the initial or continuation prompt for one ordered case."""
    template = _prompt_template("prompt.md" if first else "case.md")
    attribution = (
        "Dev 归因模式：词典映射前的 L 数字是原文件行号。每个选项先逐指标填 "
        "`dictionary_lines` 对象，完整列出 H/D/S/N/O/W/R 七个键和该指标实际采用的行号数组；"
        "缺词条但有机制估计时填 []，并在 metrics 中给带?的估计。行号必须包含对应指标，且该分量确实适用；合并行可出现在多个指标组。"
        "同一机制的一般/具体条只取最贴切一条。填完七组行号后，再合计 metrics。"
        "格式：`{\"choice\":1,\"dictionary_lines\":{\"H\":[],\"D\":[],\"S\":[26],\"N\":[],\"O\":[41],\"W\":[],\"R\":[]},\"metrics\":{...}}`。"
        if dev_full else ""
    )
    return (
        template.replace("{{DICTIONARY}}", dictionary)
        .replace("{{ACTIVATION_INSTRUCTIONS}}", attribution)
        .replace("{{TRANSLATION_RULES}}", translation_rules)
        .replace("{{CANDIDATES}}", "\n".join(candidate_rows or ["（无词面候选；仍逐指标查完整词典）"]))
        .replace("{{SHORTTITLE}}", case.shorttitle)
        .replace("{{OBSERVATION}}", json.dumps(case.observation, ensure_ascii=False, separators=(",", ":")))
    )


def render_batch_prompt(cases: list[TranslationCase], dictionary: str, translation_rules: str,
                        candidates: dict[str, list[str]] | None = None,
                        option_candidates: dict[str, dict[int, list[str]]] | None = None) -> str:
    """Translate a bounded dev batch with one fresh context and one dictionary."""
    inputs = [
        {"case_id": case.case_id, "shorttitle": case.shorttitle,
         "dictionary_matches": [int(re.match(r"L(\d+)", row)[1]) for row in (candidates or {}).get(case.case_id, [])],
         "dictionary_matches_by_choice": {choice: [int(re.match(r"L(\d+)", row)[1]) for row in rows]
                                          for choice, rows in (option_candidates or {}).get(case.case_id, {}).items()},
         "observation": case.observation}
        for case in cases
    ]
    return (
        "按以下 solution 规则和词典翻译题面选项。只输出 JSON，不解释、不复核、不调用工具。\n"
        f"规则：\n{translation_rules}\n\n"
        "输出格式：{\"cases\":[{\"case_id\":\"输入原值\",\"shorttitle\":\"输入原值\","
        "\"options\":[{\"choice\":1,\"metrics\":{\"H\":\"unknown\",\"D\":\"unknown\","
        "\"S\":\"+\",\"N\":\"unknown\",\"O\":\"unknown\",\"W\":\"unknown\",\"R\":\"unknown\"},"
        "\"dictionary_lines\":{\"H\":[],\"D\":[],\"S\":[18],\"N\":[],\"O\":[],\"W\":[],\"R\":[]}}]}]}。\n"
        "覆盖所有输入选项；metrics 保留七个键，无变化用 unknown；"
        "dictionary_lines 只填实际使用且对应指标的行号，无命中填 []。\n"
        f"词典：\n{dictionary}\n"
        f"题面：\n{json.dumps(inputs, ensure_ascii=False, separators=(',', ':'))}"
    )



def parse_batch_response(text: str) -> dict[str, dict[str, Any]]:
    """Read the final complete batch envelope from a streamed response."""
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    offset = 0
    while (start := text.find("{", offset)) >= 0:
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if isinstance(value, dict) and isinstance(value.get("cases"), list):
            last = value
        offset = start + consumed
    if last is None:
        raise ValueError("response does not contain a batch cases object")
    return {
        item["case_id"]: item for item in last["cases"]
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    }


def effect_review_choices(document: dict[str, Any], choice_count: int) -> list[int]:
    """Find options with at most one non-D effect for a per-metric coverage check."""
    options = document.get("options")
    if not isinstance(options, list) or len(options) != choice_count or not options:
        return []
    return [
        option["choice"]
        for option in options
        if isinstance(option, dict)
        and type(option.get("choice")) is int
        and isinstance(option.get("metrics"), dict)
        and sum(
            normalize_metric(metric, option["metrics"].get(metric)) != "unknown"
            for metric in "HSNOWR"
        ) <= 1
    ]


def _merge_usage(total: TokenUsage, addition: TokenUsage) -> None:
    total.input_tokens += addition.input_tokens
    total.output_tokens += addition.output_tokens
    total.total_tokens += addition.total_tokens
    total.total_cost += addition.total_cost
    for model, usage in addition.by_model.items():
        bucket = total.by_model.setdefault(
            model,
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "total_cost": 0.0},
        )
        for key, value in usage.items():
            bucket[key] = bucket.get(key, 0) + value


async def _chat(
    websocket: Any,
    session_id: str,
    prompt: str,
    timeout_s: float,
    output: Path,
) -> tuple[str, TokenUsage]:
    with tempfile.TemporaryDirectory(prefix="translation-benchmark-stream-") as temporary:
        stream_dir = Path(temporary)
        collector = StreamCollector(
            log_dir=output,
            events_path=output / "events.jsonl",
            transcript_path=stream_dir / "transcript.log",
        )
        envelope = build_chat_envelope(prompt, session_id, "agent")
        try:
            await websocket.send(json.dumps(envelope, ensure_ascii=False))
            deadline = asyncio.get_running_loop().time() + timeout_s
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=min(60.0, remaining))
                except asyncio.TimeoutError:
                    continue
                frame = raw if isinstance(raw, dict) else json.loads(raw)
                frame_request_id = frame.get("request_id")
                if frame_request_id and frame_request_id != envelope["request_id"]:
                    continue
                collector.feed_frame(frame)
                kind = str(frame.get("response_kind") or "")
                if kind == "e2a.error" or frame.get("status") == "failed":
                    raise RuntimeError(f"Jiuwen translation request failed: {frame}")
                if frame.get("is_final") and kind == "e2a.complete":
                    break
            else:
                raise TimeoutError(f"Jiuwen translation request exceeded {timeout_s:g}s")
        finally:
            collector.finalize()
        return collector.transcript.strip(), collector.totals


async def _chat_with_retries(
    websocket: Any, base_session: str, prompt: str, timeout_s: float,
    output: Path, validator: Callable[[str], object], attempts: int,
) -> tuple[str, TokenUsage, str]:
    """Retry failed or malformed Jiuwen turns with a fresh session each time."""
    total_usage = TokenUsage()
    last_text = ""
    # Resumes and socket retries must not append prompts to failed contexts.
    invocation_session = f"{base_session}-r{uuid.uuid4().hex[:8]}"
    active_session = invocation_session
    for attempt in range(1, attempts + 1):
        active_session = f"{invocation_session}-a{attempt}"
        try:
            response_text, usage = await _chat(websocket, active_session, prompt, timeout_s, output)
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            continue
        _merge_usage(total_usage, usage)
        last_text = response_text
        try:
            validator(response_text)
        except ValueError:
            continue
        return response_text, total_usage, active_session
    if not last_text.strip():
        raise TimeoutError(f"Jiuwen returned no translation text after {attempts} attempts")
    return last_text, total_usage, active_session


def _result_payload(
    case: TranslationCase,
    input_prompt: str,
    response_text: str,
    usage: TokenUsage,
    score: CaseScore,
    dictionary_activations: dict[int, list[int]] | None = None,
) -> dict[str, Any]:
    choice_texts = {
        int(choice["choice"]): str(choice["action"])
        for choice in case.observation.get("choices", [])
        if isinstance(choice, dict) and "choice" in choice and "action" in choice
    }
    return {
        "case_id": case.case_id,
        "shorttitle": case.shorttitle,
        "choice_texts": choice_texts,
        "observation": case.observation,
        "input_prompt": input_prompt,
        "expected": case.expected,
        "actual": score.actual,
        **directional_error_summary(case.expected, score.predicted),
        "predicted": score.predicted,
        "wrong_options": score.wrong_options,
        "mismatches": score.mismatches,
        "false_positives": score.false_positives,
        "false_positive_metrics": score.false_positive_metrics,
        "false_negative_metrics": score.false_negative_metrics,
        "false_positive_count": score.false_positive_count,
        "false_negative_count": score.false_negative_count,
        "errors": score.errors,
        "response_text": response_text,
        "usage": usage.to_dict(),
        "metric_correct": score.metric_correct,
        "metric_total": score.metric_total,
        "option_correct": score.option_correct,
        "option_total": score.option_total,
        "direction_scores": score.direction_scores,
        "direction_score": score.direction_score,
        "dictionary_activations": dictionary_activations or {},
    }


def _write_outputs(output: Path, metadata: dict[str, Any], results: list[dict[str, Any]],
                   dictionary: str = "") -> None:
    """Regenerate the score report and readable input/output context."""
    report_metadata = {
        **metadata,
        "omission_count": sum(result["omission_count"] for result in results),
        "reversal_count": sum(result["reversal_count"] for result in results),
        "effect_review_count": sum("-review-" in result.get("jiuwen_session_id", "") for result in results),
    }
    if metadata.get("mode") in ("dev", "dev-batch", "dev-full"):
        rows = dictionary_rows(dictionary)
        counts = activation_counts(results, rows)
        report_metadata = {**report_metadata, "dictionary_activation_counts": counts}
        (output / "dictionary-activation.tsv").write_text(
            activation_tsv(dictionary, counts), encoding="utf-8"
        )
    (output / "benchmark.md").write_text(render_report(report_metadata, results), encoding="utf-8")
    (output / "events.md").write_text(render_events(report_metadata, results), encoding="utf-8")


def _saved_fence(block: str, title: str) -> str:
    """Recover an exact fenced evidence block from an interrupted run."""
    marker = f"### {title}\n\n"
    start = block.find(marker)
    if start < 0:
        raise ValueError(f"resume evidence missing {title}")
    body = block[start + len(marker):]
    first, separator, rest = body.partition("\n")
    match = re.fullmatch(r"(`{3,})(?:[a-z]+)?", first)
    if not separator or match is None:
        raise ValueError(f"resume evidence has an invalid {title} fence")
    closing = f"\n{match.group(1)}\n"
    end = rest.find(closing)
    if end < 0:
        raise ValueError(f"resume evidence has an unterminated {title} fence")
    return rest[:end]


def _restore_results(output: Path, saved: list[dict[str, Any]], cases: list[TranslationCase]) -> list[dict[str, Any]]:
    """Rebuild report data from SQLite scores and the exact saved prompts."""
    evidence = (output / "events.md").read_text(encoding="utf-8")
    headings = list(re.finditer(r"(?m)^## (\d+)\. ", evidence))
    blocks = {
        int(match.group(1)): evidence[match.start():headings[index + 1].start() if index + 1 < len(headings) else len(evidence)]
        for index, match in enumerate(headings)
    }
    results: list[dict[str, Any]] = []
    for expected_position, row in enumerate(saved, start=1):
        position = int(row["position"])
        if position != expected_position or position > len(cases) or row["case_id"] != cases[position - 1].case_id:
            raise ValueError("resume results are not a contiguous prefix of the fixed-seed cases")
        block = blocks.get(position)
        if block is None:
            raise ValueError(f"resume evidence missing case {position}")
        observation = json.loads(_saved_fence(block, "Input observation"))
        comparison = json.loads(_saved_fence(block, "Parsed deterministic comparison"))
        if observation != cases[position - 1].observation:
            raise ValueError(f"resume observation changed at case {position}")
        result = {
            "case_id": row["case_id"], "shorttitle": row["shorttitle"],
            "choice_texts": {int(c["choice"]): str(c["action"]) for c in observation.get("choices", [])},
            "observation": observation,
            "input_prompt": _saved_fence(block, "Input prompt sent to Jiuwen"),
            "expected": json.loads(row["expected_json"]),
            "actual": json.loads(row["actual_json"]),
            "predicted": comparison["Pred"],
            "wrong_options": json.loads(row["wrong_options_json"]),
            "mismatches": json.loads(row["mismatches_json"]),
            "false_positives": comparison["false_positives"],
            "false_positive_metrics": json.loads(row["false_positive_metrics_json"]),
            "false_negative_metrics": json.loads(row["false_negative_metrics_json"]),
            "false_positive_count": row["false_positive_count"],
            "false_negative_count": row["false_negative_count"],
            "errors": json.loads(row["errors_json"]),
            "response_text": row["response_text"],
            "usage": json.loads(row["usage_json"]),
            "metric_correct": row["metric_correct"], "metric_total": row["metric_total"],
            "option_correct": row["option_correct"], "option_total": row["option_total"],
            "direction_scores": json.loads(row["direction_scores_json"]),
            "direction_score": row["direction_score"],
            "dictionary_activations": json.loads(row["dictionary_activations_json"]),
            "jiuwen_session_id": comparison["jiuwen_session_id"],
        }
        result.update(directional_error_summary(result["expected"], result["predicted"]))
        results.append(result)
    return results


async def run(
    *,
    solution: Path,
    skill_id: str,
    seed: str,
    limit: int,
    mode: str = "sample",
    ws_url: str,
    timeout_s: float,
    batch_size: int = DEV_BATCH_SIZE,
    offset: int = 0,
    output_root: Path = OUTPUT_ROOT,
    exclude_case_ids: set[str] | frozenset[str] | None = None,
    resume_run_id: str = "",
    transport: str = "jiuwen",
    continue_current_source: bool = False,
) -> dict[str, Any]:
    """Execute and persist one deterministic benchmark run."""
    if limit < 0:
        raise ValueError("limit must be >= 0; use 0 for the full event pool")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if batch_size < 1 or batch_size > 8:
        raise ValueError("batch_size must be between 1 and 8")
    if mode not in ("sample", "dev", "dev-batch", "dev-full") or (mode == "dev-full" and (limit != 0 or offset != 0 or exclude_case_ids)):
        raise ValueError("dev-full requires limit=0, offset=0, and no excluded cases")
    if resume_run_id and mode != "dev-full":
        raise ValueError("--resume-run requires --mode dev-full")
    cases, dataset_root = await load_cases(seed, 0 if offset else limit, exclude_case_ids)
    if offset:
        cases = cases[offset:offset + limit] if limit else cases[offset:]
    if not cases:
        raise RuntimeError("The installed CareerSim event pool contains no decision nodes")
    dictionary_path = resolve_dictionary(solution, skill_id)
    dictionary = dictionary_path.read_text(encoding="utf-8")
    rows = dictionary_rows(dictionary)
    candidate_rows, option_candidate_rows = _public_candidate_rows(dictionary_path, cases)
    prompt_dictionary = numbered_dictionary(dictionary, rows) if mode in ("dev", "dev-batch", "dev-full") else dictionary
    dictionary_sha256 = hashlib.sha256(dictionary.encode()).hexdigest()
    solution_skill_path, translator_path, translation_rules = resolve_translation_rules(solution, skill_id)
    solution_skill_sha256 = hashlib.sha256(solution_skill_path.read_bytes()).hexdigest()
    translator_sha256 = hashlib.sha256(translator_path.read_bytes()).hexdigest()
    translation_rules_sha256 = hashlib.sha256(translation_rules.encode()).hexdigest()
    ledger = Ledger(output_root / "benchmark.sqlite3")
    saved_rows: list[dict[str, Any]] = []
    if resume_run_id:
        saved_run, saved_rows = ledger.resumable_run(resume_run_id)
        if (saved_run["seed"] != seed or saved_run["mode"] != mode
                or (saved_run["dictionary_sha256"] != dictionary_sha256 and not continue_current_source)
                or int(saved_run["case_count"]) != len(cases)):
            raise ValueError("resume seed, mode, dictionary, or case count differs from the interrupted run")
        run_id = resume_run_id
        output = Path(saved_run["output_dir"])
        if output.resolve().parent != output_root.resolve():
            raise ValueError("resume output directory is outside the benchmark root")
        session_id = str(saved_run["session_id"])
        report_text = (output / "benchmark.md").read_text(encoding="utf-8")
        for label, value in (
            ("Solution SKILL.md", solution_skill_sha256),
            ("Observation Translator", translator_sha256),
            ("Extracted translation rules", translation_rules_sha256),
        ):
            if continue_current_source and transport == "model":
                continue
            if label == "Extracted translation rules" and transport == "model":
                continue  # Full observe/skill hashes above still enforce unchanged source policy.
            if f"- {label} SHA256: `{value}`" not in report_text:
                raise ValueError(f"resume {label} changed since the interrupted run")
        if f"- Events per batch: {batch_size}" not in report_text:
            raise ValueError("resume batch size differs from the interrupted run")
    else:
        run_id, output = allocate_output(output_root, seed)
        session_id = f"translation-benchmark-{uuid.uuid4().hex[:12]}"
    metadata = {
        "run_id": run_id,
        "seed": seed,
        "session_id": session_id,
        "dataset_root": dataset_root,
        "dictionary_sha256": dictionary_sha256,
        "solution_skill_sha256": solution_skill_sha256,
        "translator_sha256": translator_sha256,
        "translation_rules_sha256": translation_rules_sha256,
        "translator_path": str(translator_path.relative_to(solution.resolve())),
        "solution_path": str(solution.resolve()),
        "skill_id": skill_id,
        "output_dir": str(output),
        "case_count": len(cases),
        "scoring_mode": SCORING_MODE,
        "mode": mode,
        "batch_size": batch_size if mode in ("dev-batch", "dev-full") else None,
        "offset": offset,
        "dictionary_line_count": len(rows),
        "option_count": sum(len(case.expected) for case in cases),
        "compression_rate": len(rows) / sum(len(case.expected) for case in cases),
        "session_count": (len(cases) + batch_size - 1) // batch_size if mode in ("dev-batch", "dev-full") else 1,
        "selection_mode": "ledger_unseen" if exclude_case_ids else "all_cases",
        "excluded_case_count": len(exclude_case_ids or ()),
        "excluded_case_ids_sha256": hashlib.sha256("\n".join(sorted(exclude_case_ids or ())).encode()).hexdigest(),
    }
    if resume_run_id:
        results = _restore_results(output, saved_rows, cases)
        if transport != "model" and len(results) % batch_size and len(results) != len(cases):
            raise ValueError("resume requires a complete saved batch boundary")
    else:
        ledger.begin(metadata)
        results = []
    total_usage = TokenUsage()
    for result in results:
        _merge_usage(total_usage, TokenUsage(**result["usage"]))

    def record_case(
        position: int, case: TranslationCase, input_prompt: str, response_text: str,
        usage: TokenUsage, document: dict[str, Any], parse_errors: list[str], active_session: str,
    ) -> None:
        score = score_case(case, document)
        if parse_errors:
            score = replace(score, errors=parse_errors + score.errors)
        activations: dict[int, list[int]] = {}
        if mode in ("dev", "dev-batch", "dev-full"):
            activations, attribution_errors = parse_activations(document, set(case.expected), rows)
            if attribution_errors:
                score = replace(score, errors=score.errors + attribution_errors)
        result = _result_payload(case, input_prompt, response_text, usage, score, activations)
        result["jiuwen_session_id"] = active_session
        results.append(result)
        ledger.record(run_id, position, result)

    # Preserve source hashes even if the first batch disconnects before scoring.
    _write_outputs(output, metadata, results, dictionary)

    if transport == "model":
        from career_sim_runner.translation_benchmark.minimal import translate_remaining, PROMPT_VERSION
        cohort_path = output / "prompt-cohorts.json"
        cohorts = json.loads(cohort_path.read_text()) if cohort_path.exists() else []
        if not cohorts and results:
            cohorts.append({"start": 1, "end": len(results), "prompt_version": "legacy-agent-review",
                            "dictionary_sha256": saved_run["dictionary_sha256"] if resume_run_id else dictionary_sha256})
        if (not cohorts or cohorts[-1]["prompt_version"] != PROMPT_VERSION
                or cohorts[-1].get("dictionary_sha256") != dictionary_sha256):
            cohorts.append({"start": len(results) + 1, "prompt_version": PROMPT_VERSION,
                            "transport": "configured-model-json", "thinking": "disabled",
                            "dictionary_sha256": dictionary_sha256, "translator_sha256": translator_sha256})
        cohort_path.write_text(json.dumps(cohorts, ensure_ascii=False, indent=2) + "\n")
        metadata["prompt_cohorts"] = cohorts
        await translate_remaining(
            cases=cases, start=len(results), batch_size=batch_size, output=output,
            render=lambda batch: render_batch_prompt(batch, numbered_dictionary(dictionary, rows), translation_rules,
                                                     candidate_rows, option_candidate_rows),
            parse=parse_batch_response, record=record_case,
            flush=lambda: _write_outputs(output, metadata, results, dictionary), timeout_s=timeout_s,
        )
        total_usage = TokenUsage()
        for result in results:
            _merge_usage(total_usage, TokenUsage(**result["usage"]))
    elif mode in ("dev-batch", "dev-full"):
        async def translate_batch(batch_start: int) -> tuple[list[tuple[Any, ...]], TokenUsage, Path, list[dict[str, Any]]]:
            """Use one socket, fresh session, and isolated stream per bounded batch."""
            batch_output = output / f".batch-{batch_start // batch_size + 1:03d}-{uuid.uuid4().hex[:6]}"
            batch_output.mkdir()
            batch_usage = TokenUsage()
            records: list[tuple[Any, ...]] = []
            review_attempts: list[dict[str, Any]] = []
            async with websockets.connect(
                ws_url, max_size=None, open_timeout=10, ping_interval=None,
            ) as websocket:
                try:
                    await asyncio.wait_for(websocket.recv(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                batch = cases[batch_start:batch_start + batch_size]
                batch_number = batch_start // batch_size + 1
                batch_session = f"{session_id}-b{batch_number:03d}"
                input_prompt = render_batch_prompt(
                    batch, prompt_dictionary, translation_rules, candidate_rows, option_candidate_rows,
                )
                response_text, usage, active_session = await _chat_with_retries(
                    websocket, batch_session, input_prompt, timeout_s, batch_output,
                    parse_batch_response, attempts=2,
                )
                _merge_usage(batch_usage, usage)
                try:
                    documents = parse_batch_response(response_text)
                except ValueError:
                    documents = {}
                for offset, case in enumerate(batch):
                    document = documents.get(case.case_id)
                    case_prompt, case_response, case_usage = input_prompt, response_text, TokenUsage()
                    if offset == 0:
                        _merge_usage(case_usage, usage)
                    case_session = active_session
                    errors: list[str] = []
                    if document is None:
                        case_prompt = render_prompt(
                            case, prompt_dictionary, translation_rules, first=True, dev_full=True,
                            candidate_rows=candidate_rows.get(case.case_id),
                        )
                        case_response, retry_usage, case_session = await _chat_with_retries(
                            websocket, f"{batch_session}-retry-{offset + 1}",
                            case_prompt, timeout_s, batch_output, parse_json_object, attempts=3,
                        )
                        _merge_usage(batch_usage, retry_usage)
                        _merge_usage(case_usage, retry_usage)
                        try:
                            document = parse_json_object(case_response)
                        except ValueError as exc:
                            document = {}
                            errors.append(str(exc))
                    records.append((
                        batch_start + offset + 1, case, case_prompt, case_response,
                        case_usage, document, errors, case_session,
                    ))
            return records, batch_usage, batch_output, review_attempts

        async def translate_batch_resilient(batch_start: int) -> tuple[list[tuple[Any, ...]], TokenUsage, Path, list[dict[str, Any]]]:
            """Restart only an unfinished batch when its WebSocket goes away."""
            for attempt in range(5):
                try:
                    return await translate_batch(batch_start)
                except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError, TimeoutError):
                    if attempt == 4:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 8))
            raise RuntimeError("unreachable WebSocket retry state")

        starts = list(range(len(results), len(cases), batch_size))
        for group_start in range(0, len(starts), DEV_BATCH_WORKERS):
            group = await asyncio.gather(*(
                translate_batch_resilient(start)
                for start in starts[group_start:group_start + DEV_BATCH_WORKERS]
            ))
            with (output / "events.jsonl").open("ab") as stream:
                for records, batch_usage, batch_output, review_attempts in group:
                    _merge_usage(total_usage, batch_usage)
                    for record in records:
                        record_case(*record)
                    if review_attempts:
                        with (output / "review-attempts.jsonl").open("a", encoding="utf-8") as review_stream:
                            for attempt in review_attempts:
                                review_stream.write(json.dumps(attempt, ensure_ascii=False) + "\n")
                    event_path = batch_output / "events.jsonl"
                    if event_path.exists():
                        stream.write(event_path.read_bytes())
                        event_path.unlink()
                    batch_output.rmdir()
            _write_outputs(output, metadata, results, dictionary)
    else:
        async with websockets.connect(
            ws_url, max_size=None, open_timeout=10, ping_interval=None,
        ) as websocket:
            try:
                await asyncio.wait_for(websocket.recv(), timeout=3)
            except asyncio.TimeoutError:
                pass
            for position, case in enumerate(cases, start=1):
                input_prompt = render_prompt(
                    case, prompt_dictionary, translation_rules, first=position == 1,
                    dev_full=mode == "dev",
                    candidate_rows=candidate_rows.get(case.case_id),
                )
                response_text, usage = await _chat(websocket, session_id, input_prompt, timeout_s, output)
                _merge_usage(total_usage, usage)
                parse_errors: list[str] = []
                try:
                    document = parse_json_object(response_text)
                except ValueError as exc:
                    document = {}
                    parse_errors.append(str(exc))
                record_case(position, case, input_prompt, response_text, usage, document, parse_errors, session_id)
                _write_outputs(output, metadata, results, dictionary)

    totals = {
        "metric_correct": sum(result["metric_correct"] for result in results),
        "metric_total": sum(result["metric_total"] for result in results),
        "option_correct": sum(result["option_correct"] for result in results),
        "option_total": sum(result["option_total"] for result in results),
        "direction_score": sum(sum(float(score) for score in result["direction_scores"].values()) for result in results)
        / sum(int(result["option_total"]) for result in results),
        "direction_option_total": sum(int(result["option_total"]) for result in results),
        "false_positive_count": sum(int(result["false_positive_count"]) for result in results),
        "false_negative_count": sum(int(result["false_negative_count"]) for result in results),
        "omission_count": sum(result["omission_count"] for result in results),
        "reversal_count": sum(result["reversal_count"] for result in results),
        "effect_review_count": sum("-review-" in result["jiuwen_session_id"] for result in results),
    }
    if mode in ("dev", "dev-batch", "dev-full"):
        counts = activation_counts(results, rows)
        totals["active_dictionary_lines"] = sum(value > 0 for value in counts.values())
        totals["dictionary_event_activations"] = sum(counts.values())
        totals["mean_activations_per_line"] = sum(counts.values()) / len(rows) if rows else 0.0
    ledger.finish(run_id, totals)
    return {
        "ok": True,
        **metadata,
        **totals,
        "metric_score": totals["metric_correct"] / totals["metric_total"],
        "option_score": totals["option_correct"] / totals["option_total"],
        "accuracy_acceptable": totals["direction_score"] > 0.65,
        "token_usage": total_usage.to_dict(),
        "report": str(output / "benchmark.md"),
        "events": str(output / "events.md"),
        **({"dictionary_activation_report": str(output / "dictionary-activation.tsv")}
           if mode in ("dev", "dev-batch", "dev-full") else {}),
    }
