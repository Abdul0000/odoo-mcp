# Fast-safe write strategy

This connector keeps strict Odoo validation but avoids the slow git worktree transaction for ordinary small edits.

## Preferred tool

Use `repo.write_many_smart` for multi-file writes.

It chooses:

- `repo.write_many_fast_safe` for small related edits.
- `repo.write_many_atomic` for larger/riskier edits that exceed configured limits.

## Safety preserved in fast-safe mode

`repo.write_many_fast_safe` still does all of these:

1. Validates every target path inside the allowed project root.
2. Blocks `.git` internal paths.
3. Takes the repository lock.
4. Backs up original file bytes.
5. Writes through temporary files and atomic replace.
6. Rolls back every touched file if any write fails.
7. Returns changed files so the agent can run `git.diff` and `workflow.verify_against_core` only on files changed by the task.

It avoids only the expensive parts of `repo.write_many_atomic`:

- temporary git worktree
- temporary branch
- whole-repo binary diff
- git apply check/apply roundtrip

## Environment controls

```env
ODOO_AGENT_FAST_WRITE_MAX_FILES=8
ODOO_AGENT_FAST_WRITE_MAX_BYTES_EACH=300000
ODOO_AGENT_FAST_WRITE_MAX_TOTAL_BYTES=750000
```

## Agent rule

- One small file: `repo.write`.
- Normal multi-file change: `repo.write_many_smart`.
- Forced small fast path: `repo.write_many_fast_safe`.
- Large/risky refactor/new module: `repo.write_many_atomic`.
- Never skip `git.diff`, `qa.self_run`, or `workflow.verify_against_core` on changed files.
