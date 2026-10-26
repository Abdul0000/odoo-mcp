# Review fixes (post v3)

- tests: fake clocks scoped per module (`patch_module_clock`, `advance_clock_on_job_wait`); the old patches mutated the global `time` module and hung the suite. Flaky timing test uses CPU time, best of 3.
- tests: stale tool-set expectations include ws_* (`WS_TOOLS`); size budgets re-measured (tools/list 80 KB, instructions 15 KB).
- deps: `requirements-dev.txt` (jsonschema, pytest-asyncio); `pytest.ini` asyncio strict mode.
- security: OAuth signing never uses a public fallback key (derived HMAC from API token, or random per-process); connector token compared in constant time; hosted-deployment guard covers Render, Koyeb, Fly, Heroku, Cloud Run. NOTE: OAuth access tokens issued before this change stop validating once; clients re-authorise.
- bug: `CommandResult` was undefined on the job-resume fallback path in routers/tools.py.
- arch: ws.* dispatch is one handler table (`_WS_HANDLERS`); input models derived from it.
- hygiene: FastAPI `lifespan` replaces `on_event`; `ruff.toml` (real-bug rules) and a CI workflow with a 10-minute timeout.

# odoo-mcp ws upgrade (v3)

v3: visible WIP branch (real files), ws_status, commit links + default-branch note, edit results say 'published: false'.

New tools: ws_open, ws_read, ws_grep, ws_edit, ws_diff, ws_lint, ws_commit, ws_discard,
           ws_outline, ws_scaffold, ws_extend, ws_symbol, ws_status
New services: toon_service, lint_service, github_commit_service, workspace_service, workspace_factory,
              outline_service, scaffold_service, symbol_service, job_retention
Fixes: delete->unwire+lint, binary/size guards, assets warning, lazy open, journal-only checkpoints (async),
       blob dedupe, keep-alive transport, hot-path commit, job prune/wait, tools/list cache, blobless clones
Tests: tests/test_toon_service.py test_workspace_service.py test_scaffold_outline.py test_job_retention.py
       test_ws_tool_wiring.py test_evals.py (+ tests/evals, tests/support/fake_github.py)
Run:   python -m unittest tests.test_toon_service tests.test_workspace_service tests.test_scaffold_outline \
                          tests.test_job_retention tests.test_ws_tool_wiring tests.test_evals
