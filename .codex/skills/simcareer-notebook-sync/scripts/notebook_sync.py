#!/usr/bin/env python3
"""Audit CareerSim notebooks and read-only runtime evolution diagnostics."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


MISSING = "MISSING"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SyncError(RuntimeError):
    """A safe, user-facing synchronization error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_keep(relative: Path) -> bool:
    """Return whether any path component declares notebook keep state."""
    return any(
        re.search(r"(?:^|[-_.])keeps?$", Path(part).stem, re.IGNORECASE)
        for part in relative.parts
    )


def discover_active_install(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not candidate.is_file():
            raise SyncError(f"active install record does not exist: {candidate}")
        return candidate

    script = Path(__file__).resolve()
    roots = [Path.cwd().resolve(), *script.parents]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(
            [
                root / ".career_sim_runner" / "career_emu" / "active_install.json",
                root
                / "CareerSim-BDCI26"
                / ".career_sim_runner"
                / "career_emu"
                / "active_install.json",
            ]
        )
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate not in seen and candidate.is_file():
            return candidate
        seen.add(candidate)
    raise SyncError(
        "could not find active_install.json; pass --active-install or both notebook paths"
    )


def load_locations(args: argparse.Namespace) -> dict[str, Any]:
    source_arg = args.source_notebooks
    runtime_arg = args.runtime_notebooks
    if (source_arg is None) != (runtime_arg is None):
        raise SyncError(
            "pass --source-notebooks and --runtime-notebooks together, or neither"
        )

    record_path: Path | None = None
    if source_arg is not None and runtime_arg is not None:
        source = source_arg.expanduser().absolute()
        runtime = runtime_arg.expanduser().absolute()
        skill_name = args.skill_name or source.parent.name
        basis = "explicit"
    else:
        record_path = discover_active_install(args.active_install)
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncError(f"cannot read active install record: {exc}") from exc

        manifest = record.get("manifest") or {}
        installed = manifest.get("participant_skill_names") or []
        if args.skill_name:
            skill_name = args.skill_name
        elif len(installed) == 1 and isinstance(installed[0], str):
            skill_name = installed[0]
        elif "observe-decide-review" in installed:
            skill_name = "observe-decide-review"
        else:
            raise SyncError("cannot select one installed skill; pass --skill-name")

        submission_dir = record.get("submission_dir")
        skill_dir = record.get("skill_dir")
        if not isinstance(submission_dir, str) or not isinstance(skill_dir, str):
            raise SyncError("active install record lacks submission_dir or skill_dir")
        source = (
            Path(submission_dir).expanduser().absolute()
            / "skills"
            / skill_name
            / "notebooks"
        )
        runtime = (
            Path(skill_dir).expanduser().absolute() / skill_name / "notebooks"
        )
        basis = "active_install"

    if not source.is_dir():
        raise SyncError(f"source notebooks directory does not exist: {source}")
    if not runtime.is_dir():
        raise SyncError(f"runtime notebooks directory does not exist: {runtime}")

    source_skill = source.parent
    runtime_skill = runtime.parent

    return {
        "basis": basis,
        "active_install": str(record_path) if record_path else None,
        "skill_name": skill_name,
        "source_notebooks": str(source),
        "source_resolved": str(source.resolve()),
        "runtime_notebooks": str(runtime),
        "runtime_resolved": str(runtime.resolve()),
        "source_skill": str(source_skill),
        "source_skill_resolved": str(source_skill.resolve()),
        "runtime_skill": str(runtime_skill),
        "runtime_skill_resolved": str(runtime_skill.resolve()),
        "source_evolutions": str(source_skill / "evolutions.json"),
        "runtime_evolutions": str(runtime_skill / "evolutions.json"),
        "source_path": source,
        "runtime_path": runtime,
        "source_evolutions_path": source_skill / "evolutions.json",
        "runtime_evolutions_path": runtime_skill / "evolutions.json",
    }


def relative_files(base: Path) -> dict[Path, Path]:
    files: dict[Path, Path] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            files[path.relative_to(base)] = path
    return files


def text_diff(
    source: Path | None, runtime: Path | None, relative: Path
) -> dict[str, Any]:
    try:
        before = (
            source.read_text(encoding="utf-8").splitlines(keepends=True)
            if source is not None
            else []
        )
        after = (
            runtime.read_text(encoding="utf-8").splitlines(keepends=True)
            if runtime is not None
            else []
        )
    except UnicodeDecodeError:
        return {"available": False, "reason": "non-UTF-8 content"}
    diff = "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"source/{relative.as_posix()}" if source else "/dev/null",
            tofile=f"runtime/{relative.as_posix()}" if runtime else "/dev/null",
        )
    )
    return {"available": True, "unified": diff}


def load_evolution_records(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"valid": False, "record_count": 0, "error": "file is missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "record_count": 0, "error": str(exc)}
    if not isinstance(payload, list):
        return {
            "valid": False,
            "record_count": 0,
            "error": "top-level JSON value must be an array",
        }
    return {"valid": True, "record_count": len(payload), "records": payload}


def evolution_diagnostics(locations: dict[str, Any]) -> dict[str, Any]:
    source: Path = locations["source_evolutions_path"]
    runtime: Path = locations["runtime_evolutions_path"]
    source_file = source if source.is_file() else None
    runtime_file = runtime if runtime.is_file() else None
    source_hash = sha256(source_file) if source_file else MISSING
    runtime_hash = sha256(runtime_file) if runtime_file else MISSING

    if source_file is None and runtime_file is None:
        status_name = "missing"
    elif source_file is None:
        status_name = "runtime_only"
    elif runtime_file is None:
        status_name = "source_only"
    elif source_hash == runtime_hash:
        status_name = "unchanged"
    else:
        status_name = "modified"

    source_json = load_evolution_records(source)
    runtime_json = load_evolution_records(runtime)
    added_records: list[Any] = []
    comparison_basis = "unavailable"
    if runtime_json["valid"]:
        runtime_records = runtime_json["records"]
        if source_json["valid"]:
            source_counts: dict[str, int] = {}
            for record in source_json["records"]:
                key = json.dumps(record, ensure_ascii=False, sort_keys=True)
                source_counts[key] = source_counts.get(key, 0) + 1
            for record in runtime_records:
                key = json.dumps(record, ensure_ascii=False, sort_keys=True)
                if source_counts.get(key, 0):
                    source_counts[key] -= 1
                else:
                    added_records.append(record)
            comparison_basis = "runtime records absent from source"
        else:
            added_records = runtime_records
            comparison_basis = "all runtime records because source JSON is unavailable"

    return {
        "purpose": "diagnostic_only",
        "sync_eligible": False,
        "status": status_name,
        "source": {
            "path": str(source),
            "resolved": str(source.resolve(strict=False)),
            "sha256": source_hash,
            "json": {
                key: value for key, value in source_json.items() if key != "records"
            },
        },
        "runtime": {
            "path": str(runtime),
            "resolved": str(runtime.resolve(strict=False)),
            "sha256": runtime_hash,
            "json": {
                key: value for key, value in runtime_json.items() if key != "records"
            },
        },
        "comparison_basis": comparison_basis,
        "runtime_added_count": len(added_records),
        "runtime_added_records": added_records,
    }


def audit(locations: dict[str, Any]) -> dict[str, Any]:
    source: Path = locations["source_path"]
    runtime: Path = locations["runtime_path"]
    source_files = relative_files(source)
    runtime_files = relative_files(runtime)
    all_paths = sorted(set(source_files) | set(runtime_files))

    excluded = [path.as_posix() for path in all_paths if is_keep(path)]
    changes: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for relative in all_paths:
        if is_keep(relative):
            continue
        source_file = source_files.get(relative)
        runtime_file = runtime_files.get(relative)
        source_hash = sha256(source_file) if source_file else MISSING
        runtime_hash = sha256(runtime_file) if runtime_file else MISSING
        if source_file is None:
            status_name = "runtime_only"
        elif runtime_file is None:
            status_name = "source_only"
        elif source_hash == runtime_hash:
            unchanged.append(relative.as_posix())
            continue
        else:
            status_name = "modified"

        item: dict[str, Any] = {
            "file": relative.as_posix(),
            "status": status_name,
            "source_sha256": source_hash,
            "runtime_sha256": runtime_hash,
        }
        item["diff"] = text_diff(source_file, runtime_file, relative)
        changes.append(item)

    evolution_diagnostics_result = evolution_diagnostics(locations)

    return {
        "operation": "audit",
        "wrote_files": False,
        "basis": locations["basis"],
        "active_install": locations["active_install"],
        "skill_name": locations["skill_name"],
        "source_notebooks": locations["source_notebooks"],
        "source_resolved": locations["source_resolved"],
        "runtime_notebooks": locations["runtime_notebooks"],
        "runtime_resolved": locations["runtime_resolved"],
        "source_skill": locations["source_skill"],
        "source_skill_resolved": locations["source_skill_resolved"],
        "runtime_skill": locations["runtime_skill"],
        "runtime_skill_resolved": locations["runtime_skill_resolved"],
        "excluded_keep_files": excluded,
        "unchanged_non_keep_files": unchanged,
        "changes": changes,
        "evolution_diagnostics": evolution_diagnostics_result,
        "summary": {
            "changed": len(changes),
            "unchanged": len(unchanged),
            "excluded_keep": len(excluded),
            "evolution_runtime_added": evolution_diagnostics_result[
                "runtime_added_count"
            ],
        },
    }


def validate_relative(raw: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise SyncError(f"unsafe relative path: {raw}")
    if is_keep(relative):
        raise SyncError(f"keep notebook is never writable through this tool: {raw}")
    if relative.name.casefold() == "evolutions.json":
        raise SyncError("evolutions.json is diagnostic-only and is never writable")
    return relative


def checked_child(base: Path, relative: Path) -> Path:
    base_real = base.resolve()
    child = base / relative
    child_real = child.resolve(strict=False)
    if not child_real.is_relative_to(base_real):
        raise SyncError(f"path escapes notebooks directory: {relative}")
    return child


def normalize_expected(value: str, label: str, allow_missing: bool) -> str:
    normalized = value.strip()
    if allow_missing and normalized.upper() == MISSING:
        return MISSING
    normalized = normalized.lower()
    if not SHA256_RE.fullmatch(normalized):
        suffix = " or MISSING" if allow_missing else ""
        raise SyncError(f"{label} must be a SHA-256{suffix}")
    return normalized


def apply_approved(args: argparse.Namespace, locations: dict[str, Any]) -> dict[str, Any]:
    if not args.approved_by_user:
        raise SyncError("apply requires --approved-by-user after explicit user approval")
    relative = validate_relative(args.file)
    expected_runtime = normalize_expected(
        args.expected_runtime_sha256, "expected runtime hash", False
    )
    expected_source = normalize_expected(
        args.expected_source_sha256, "expected source hash", True
    )

    source = checked_child(locations["source_path"], relative)
    runtime = checked_child(locations["runtime_path"], relative)
    if not runtime.is_file() or runtime.is_symlink():
        raise SyncError(f"runtime file is missing, non-regular, or a symlink: {runtime}")
    if source.is_symlink():
        raise SyncError(f"source file is a symlink: {source}")
    if source.exists() and not source.is_file():
        raise SyncError(f"source path exists but is not a regular file: {source}")

    actual_runtime = sha256(runtime)
    actual_source = sha256(source) if source.is_file() else MISSING
    if actual_runtime != expected_runtime:
        raise SyncError(
            f"runtime changed after audit: expected {expected_runtime}, got {actual_runtime}"
        )
    if actual_source != expected_source:
        raise SyncError(
            f"source changed after audit: expected {expected_source}, got {actual_source}"
        )
    if actual_source == actual_runtime:
        raise SyncError("source already equals runtime; no write is needed")

    source.parent.mkdir(parents=True, exist_ok=True)
    mode = (
        stat.S_IMODE(source.stat().st_mode)
        if source.exists()
        else stat.S_IMODE(runtime.stat().st_mode)
    )
    data = runtime.read_bytes()
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=source.parent, prefix=f".{source.name}.", delete=False
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.chmod(temp_name, mode)
        os.replace(temp_name, source)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)

    result_hash = sha256(source)
    return {
        "operation": "apply",
        "wrote_files": True,
        "file": relative.as_posix(),
        "source_path": str(source),
        "runtime_path": str(runtime),
        "previous_source_sha256": actual_source,
        "runtime_sha256": actual_runtime,
        "result_sha256": result_hash,
    }


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--active-install", type=Path)
    common.add_argument("--source-notebooks", type=Path)
    common.add_argument("--runtime-notebooks", type=Path)
    common.add_argument("--skill-name")

    root = argparse.ArgumentParser(
        description=(
            "Audit CareerSim notebook changes and read-only evolution diagnostics, "
            "or explicitly apply approved notebook changes."
        )
    )
    subparsers = root.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit", parents=[common])
    apply_parser = subparsers.add_parser("apply", parents=[common])
    apply_parser.add_argument("--file", required=True)
    apply_parser.add_argument("--expected-runtime-sha256", required=True)
    apply_parser.add_argument("--expected-source-sha256", required=True)
    apply_parser.add_argument("--approved-by-user", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        locations = load_locations(args)
        result = audit(locations) if args.command == "audit" else apply_approved(args, locations)
    except SyncError as exc:
        print(json.dumps({"error": str(exc), "wrote_files": False}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
