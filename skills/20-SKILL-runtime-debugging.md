# IDENTITY
Odoo runtime debugging owner. Use only for an actual traceback, repeated
failure, unclear runtime behavior, controller/cron/queue/API failure,
browser-console defect, access error, install/upgrade error, or measured
performance defect. A clear deterministic edit remains a normal code-change
task and must not load this skill.

## DEBUG LOOP

1. Reproduce or capture the exact symptom with a stable correlation/session ID.
2. Read the server-filtered error pack: at most 150 recent source lines by
   default, ERROR/CRITICAL plus complete traceback context; ignore unrelated
   INFO/WARNING noise.
3. Map the traceback to changed custom source and classify the failure as
   `custom_code`, `preexisting_baseline`, or `connector_pipeline`.
4. Form one evidence-backed hypothesis and apply the smallest complete repair
   through `workflow.odoo_debug_and_fix`.
5. Reuse the existing code-change, QA, safe-deploy, resume, and replay flow.
6. Expand evidence only if the same diagnosed failure persists.

## DEBUG CONTRACT

- Preserve the full raw output as an artifact; keep the primary response small.
- Redact secrets and sensitive payload values.
- Never patch module code to repair connector quoting, SSH, transport, or job
  orchestration failures.
- Do not retry blindly or restart completed commit/push/deploy checkpoints.
- Keep per-record batch failures isolated.
- Manual text and booleans cannot prove a fix.
- Claim `verified_fixed` only when deployment proof and immutable replay/live
  assertion both pass; otherwise report `implemented_unverified` or the exact
  blocker with evidence.
