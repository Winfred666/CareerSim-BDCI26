"""Command-line interface for the observation translation benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from career_sim_runner.constants import REPO_ROOT
from career_sim_runner.setup import resolve_instance_ws_url
from career_sim_runner.translation_benchmark import DEFAULT_LIMIT, DEFAULT_SEED
from career_sim_runner.translation_benchmark.driver import OUTPUT_ROOT, run
from career_sim_runner.translation_benchmark.rescore import rescore_existing
from career_sim_runner.translation_benchmark.storage import Ledger


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark observation-translator against hidden event deltas")
    parser.add_argument("--solution", default=str(REPO_ROOT / "solution"))
    parser.add_argument("--skill-id", default="observe-decide-review")
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--mode", choices=("sample", "dev", "dev-batch", "dev-full"), default="sample",
                        help="dev tracks dictionary use; dev-batch probes a fixed-seed sample in fresh batches; dev-full tests every dev node")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Decision nodes to test; 0 tests all nodes")
    parser.add_argument("--offset", type=int, default=0,
                        help="Skip this many fixed-seed nodes before a sample; dev-full requires 0")
    parser.add_argument("--batch-size", type=int, default=4,
                        help="Events per fresh Jiuwen session in dev-batch/dev-full (default: 4)")
    parser.add_argument("--timeout-s", type=float, default=180.0, help="Timeout for each Jiuwen translation")
    parser.add_argument("--ws-url", default="")
    parser.add_argument("--transport", choices=("model", "jiuwen"), default="model",
                        help="model uses the configured model with only translation context; jiuwen is legacy")
    parser.add_argument("--continue-current-source", action="store_true",
                        help="Explicitly continue untranscribed cases with current source, preserving old cohorts")
    parser.add_argument("--run-id", default="", help="With --rescore-existing, update only this run")
    parser.add_argument("--resume-run", default="", help="Resume an interrupted dev-full run after source-hash checks")
    parser.add_argument(
        "--rescore-existing",
        action="store_true",
        help="Recompute direction cosine for ledgered outputs without calling Jiuwen",
    )
    parser.add_argument(
        "--unseen-only",
        action="store_true",
        help="Exclude every case ID already present in the benchmark ledger",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one benchmark in a fresh Jiuwen session."""
    args = _parser().parse_args(argv)
    try:
        if args.rescore_existing:
            if args.resume_run:
                raise ValueError("--resume-run cannot be used with --rescore-existing")
            result = rescore_existing(OUTPUT_ROOT, args.run_id or None)
        else:
            if args.run_id:
                raise ValueError("--run-id requires --rescore-existing")
            if args.mode == "dev-full" and args.unseen_only:
                raise ValueError("dev-full requires every dev event; remove --unseen-only")
            if args.mode == "dev-full" and args.offset:
                raise ValueError("dev-full requires --offset 0")
            if args.mode == "dev-full" and args.limit not in (DEFAULT_LIMIT, 0):
                raise ValueError("dev-full always tests all decision nodes; remove --limit")
            excluded = Ledger(OUTPUT_ROOT / "benchmark.sqlite3").case_ids() if args.unseen_only else None
            result = asyncio.run(
                run(
                    solution=Path(args.solution),
                    skill_id=args.skill_id,
                    seed=args.seed,
                    limit=0 if args.mode == "dev-full" else args.limit,
                    mode=args.mode,
                    ws_url=args.ws_url or (resolve_instance_ws_url() if args.transport == "jiuwen" else ""),
                    transport=args.transport,
                    continue_current_source=args.continue_current_source,
                    timeout_s=args.timeout_s,
                    batch_size=args.batch_size,
                    offset=args.offset,
                    exclude_case_ids=excluded,
                    resume_run_id=args.resume_run,
                )
            )
    except (ConnectionError, FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    if result.get("output_dir"):
        from career_sim_runner.translation_benchmark.audit import audit_run
        result["automated_audit"] = audit_run(Path(result["output_dir"]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
