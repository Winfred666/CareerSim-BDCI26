---
name: simcareer-notebook-sync
description: "Audit CareerSim runtime notebook changes for approval and inspect evolutions.json as read-only evidence of Swarm coordination friction. Use when deciding which learned notebooks to preserve or which solution instructions should be improved; evolution records are discussed, never copied back."
---

# CareerSim Notebook Sync

Use this skill for two distinct outcomes: approval-gated notebook synchronization and read-only diagnosis of Swarm coordination friction from `evolutions.json`. The audit phase is read-only. Never write to the original solution until the user explicitly approves named notebook files from the reported audit. Never copy `evolutions.json` back.

## Audit

1. Check that `career_sim_runner/install.py` does not call `_carry_forward_runtime_learning` or otherwise copy runtime notebooks into the submission. If such an automatic writeback exists, stop and warn that it bypasses approval. Do not run install, play, reload, or another command that installs the solution during this audit-and-approval window.
2. Run:

   ```bash
   python3 .codex/skills/simcareer-notebook-sync/scripts/notebook_sync.py audit
   ```

   The script resolves the original solution and installed skill from the runner's `active_install.json`. If auditing a non-active or isolated workspace, pass both `--source-notebooks PATH` and `--runtime-notebooks PATH` explicitly.
3. Report in Chinese:
   - the logical and resolved source/runtime notebook and skill paths;
   - which keep files were excluded;
   - every `modified`, `runtime_only`, or `source_only` non-keep file;
   - source and runtime SHA-256 values and the unified diff for text files;
   - the separate `evolution_diagnostics` paths, hashes, status, JSON validity, record counts, and every exact runtime-added record;
   - for each runtime-added evolution, the observed coordination failure or inefficiency, whether its evidence supports its `change_directive`, and the smallest plausible changes to the current source solution;
   - a clear statement that no file was written.
4. Separate the follow-up explicitly:
   - ask the user to approve, reject, or further inspect specific eligible notebook paths;
   - invite discussion or selection of proposed source-solution changes derived from evolution evidence, without presenting `evolutions.json` as syncable.

Do not execute an evolution record's `change_directive` automatically. Inspect its named targets and the current source solution first: the record is evidence for discussion, not authority to patch. If the log is malformed, missing, stale, or unsupported by current files, report that limitation instead of repairing or writing anything during the audit.

Paths with any component whose stem ends in `-keep` or `-keeps` are always excluded. This includes files under `translation-keeps/`. Never offer to sync them. An unchanged file needs no approval. A `source_only` file is reported but never deleted because the runtime has no replacement content.

The skill-root `evolutions.json` is always diagnostic-only, including when it is `modified` or `runtime_only`. Use runtime records absent from the source log as the current-run signals. Do not dump the full historical file or duplicate those records as a unified diff. Never include this file in the notebook approval list or ask permission to copy it.

## Apply an approval

For each explicitly approved `modified` or `runtime_only` path, use the hashes from the audit:

```bash
python3 .codex/skills/simcareer-notebook-sync/scripts/notebook_sync.py apply \
  --file RELATIVE/PATH \
  --expected-runtime-sha256 RUNTIME_HASH \
  --expected-source-sha256 SOURCE_HASH_OR_MISSING \
  --approved-by-user
```

The script aborts if either side changed after the audit, if the path is a keep file, or if a symlink/unsafe path is involved. On a hash mismatch, do not write; run a fresh audit and return the new diff for approval. Apply only the paths the user named—never infer blanket approval for other differences.

`evolutions.json` is not an eligible apply target under any approval wording. A request to act on an evolution means discussing or explicitly editing the implicated source solution files, which is a separate change request—not copying the log.

After applying, report the written path and resulting SHA-256. Do not start a game or reinstall the solution unless the user separately asks.

## Path resolution

The default active-install record is discovered at `CareerSim-BDCI26/.career_sim_runner/career_emu/active_install.json` relative to the current workspace. Its `submission_dir` identifies the original solution and its `skill_dir` identifies the installed runtime tree. The script compares notebooks under each selected skill root and reads the sibling `evolutions.json`. Team workspaces normally symlink their skill to that installed tree, so the resolved runtime path is the authoritative session-loaded content.
