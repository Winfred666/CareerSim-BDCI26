"""One-shot translation requests; all validation and scoring stay in Python."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from openai import AsyncOpenAI, APIStatusError

from career_sim_runner.models import TokenUsage
from career_sim_runner.paths import jiuwenswarm_env_path
from career_sim_runner.setup import configured_model_name
from career_sim_runner.translation_benchmark.scoring import normalize_metric

PROMPT_VERSION = "solution-only-json-v1"
SYSTEM_PROMPT = "你是事件指标翻译器。遵循用户提供的 solution 规则，只返回指定 JSON。"


def valid_document(document: Any, case: Any) -> bool:
    """Validate output structure using public choices only, never hidden deltas."""
    if not isinstance(document, dict):
        return False
    options = document.get("options")
    choices = {c["choice"] for c in case.observation["choices"]}
    if not isinstance(options, list) or len(options) != len(choices):
        return False
    if any(not isinstance(o, dict) for o in options):
        return False
    if any(type(o.get("choice")) is not int for o in options):
        return False
    if {o.get("choice") for o in options} != choices:
        return False
    for option in options:
        metrics = option.get("metrics")
        if not isinstance(metrics, dict):
            return False
        if any(normalize_metric(m, v).startswith("invalid:") for m, v in metrics.items() if m in "HDSNOWR"):
            return False
    return True


def recover_documents(text: str, parse: Callable) -> dict:
    """Salvage complete case objects from a partially malformed batch envelope."""
    try:
        return parse(text)
    except ValueError:
        decoder = json.JSONDecoder()
        documents = {}
        for offset, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("case_id"), str) and isinstance(value.get("options"), list):
                documents.setdefault(value["case_id"], value)
        return documents


async def translate_remaining(*, cases: list, start: int, batch_size: int, output: Path,
                              render: Callable, parse: Callable, record: Callable,
                              flush: Callable, timeout_s: float) -> dict:
    """Cache each valid response; retry only missing cases once and never self-review."""
    env = dotenv_values(jiuwenswarm_env_path())
    model = configured_model_name()
    if not env.get("API_KEY") or not env.get("API_BASE"):
        raise RuntimeError("Configured model endpoint/credentials missing")
    attempt_path = output / "translation-attempts.jsonl"
    cache: dict[str, dict] = {}
    prompts = {c.case_id: hashlib.sha256(render([c]).encode()).hexdigest() for c in cases[start:]}
    by_id = {c.case_id: c for c in cases[start:]}
    if attempt_path.exists():
        for line in attempt_path.read_text().splitlines():
            attempt = json.loads(line)
            requested = [by_id[c] for c in attempt.get("case_ids", []) if c in by_id]
            if (attempt.get("prompt_version") != PROMPT_VERSION or not requested
                    or attempt["prompt"] != render([next(c for c in cases if c.case_id == cid)
                                                     for cid in attempt["case_ids"]])):
                continue
            # Reuse original valid objects even if another object's malformed JSON
            # made the old whole-envelope validator reject the response.
            for case_id, document in recover_documents(attempt["response"], parse).items():
                if case_id in by_id and valid_document(document, by_id[case_id]):
                    cache.setdefault(case_id, {"document": document, "source_hash": prompts[case_id],
                                              "prompt": attempt["prompt"], "response": attempt["response"],
                                              "session": attempt["session"]})
    position = start
    pending_usage = TokenUsage()

    def commit() -> None:
        nonlocal position, pending_usage
        while position < len(cases) and cases[position].case_id in cache:
            case = cases[position]
            saved = cache[case.case_id]
            record(position + 1, case, saved["prompt"], saved["response"], pending_usage,
                   saved["document"], [], saved["session"])
            pending_usage = TokenUsage()
            position += 1
            flush()

    commit()
    async with AsyncOpenAI(api_key=env["API_KEY"], base_url=env["API_BASE"],
                           timeout=timeout_s, max_retries=0) as client:
        while position < len(cases):
            batch = [c for c in cases[position:position + batch_size] if c.case_id not in cache]
            missing = batch
            for attempt_number in (1, 2):
                prompt = render(missing)
                session = f"model-{PROMPT_VERSION}-{position+1}-a{attempt_number}"
                evidence = {"prompt_version": PROMPT_VERSION, "model": model,
                            "session": session, "case_ids": [c.case_id for c in missing],
                            "system_prompt": SYSTEM_PROMPT, "prompt": prompt,
                            "response": "", "accepted": {}, "usage": None}
                fatal = False
                try:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                  {"role": "user", "content": prompt}],
                        temperature=0.95, top_p=0.1, max_tokens=8192,
                        response_format={"type": "json_object"},
                        extra_body={"thinking": {"type": "disabled"}},
                    )
                    evidence["response"] = response.choices[0].message.content or ""
                    if response.usage is not None:
                        u = response.usage
                        detail = getattr(u, "completion_tokens_details", None)
                        evidence["usage"] = {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens,
                                             "total_tokens": u.total_tokens,
                                             "reasoning_tokens": getattr(detail, "reasoning_tokens", None)}
                        pending_usage.input_tokens += u.prompt_tokens
                        pending_usage.output_tokens += u.completion_tokens
                        pending_usage.total_tokens += u.total_tokens
                    documents = recover_documents(evidence["response"], parse)
                    for case in missing:
                        doc = documents.get(case.case_id)
                        if valid_document(doc, case):
                            saved = {"source_hash": prompts[case.case_id], "document": doc}
                            evidence["accepted"][case.case_id] = saved
                            cache[case.case_id] = {**saved, "prompt": prompt, "response": evidence["response"],
                                                  "session": session}
                    evidence["missing"] = [c.case_id for c in missing if c.case_id not in cache]
                except Exception as exc:
                    # Do not log credentials, endpoint URLs, or arbitrary API response bodies.
                    evidence["error"] = type(exc).__name__
                    if isinstance(exc, APIStatusError):
                        evidence["http_status"] = exc.status_code
                        fatal = 400 <= exc.status_code < 500 and exc.status_code != 429
                with attempt_path.open("a") as handle:
                    handle.write(json.dumps(evidence, ensure_ascii=False) + "\n")
                commit()
                missing = [c for c in missing if c.case_id not in cache]
                if not missing:
                    usage = evidence.get("usage") or {}
                    if (usage.get("input_tokens", 0) > 20000
                            or (usage.get("reasoning_tokens") or 0) > 512):
                        raise RuntimeError("Translation context/reasoning cost exceeds the minimal-runtime guard; responses saved")
                    print(json.dumps({"completed": position, "total": len(cases), "usage": usage}, ensure_ascii=False), flush=True)
                    break
                if fatal or attempt_number == 2:
                    raise RuntimeError(f"Translation request failed for {len(missing)} cases; see {attempt_path}")
    return {"prompt_version": PROMPT_VERSION, "transport": "configured-model-json",
            "thinking": "disabled", "model": model}
