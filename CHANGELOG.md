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
