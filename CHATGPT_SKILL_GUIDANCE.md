# ChatGPT Skill Guidance: Odoo MCP Repo-First Coding

Use this guidance for every Odoo coding task through this MCP connector.

## Mandatory first step

Always call `repo.prepare` before reading, patching, committing, pushing, or upgrading a module.

`repo.prepare` will:

1. Use `TARGET_GIT_REPO` and `TARGET_GIT_BRANCH` when the request does not provide repo/branch.
2. Clone into `WORKSPACE_ROOT/<repo_name>` when missing.
3. Reuse the existing clone when present.
4. Run `git fetch`, `git checkout <branch>`, and `git pull --ff-only origin <branch>`.
5. Return `project_path`, `branch`, `sha`, and `git status`.

## Coding flow

```text
repo.prepare
repo.list_custom_modules
repo.read target module files
repo.apply_patch or repo.write
git.diff
qa.self_run
git.commit
git.push
odoosh.wait_for_commit
odoosh.verify_module_manifest
odoosh.module_update
odoo.module_status
odoosh.grep_errors
browser.screenshot when UI changed
```

## Rules

- Never start coding from `odoo.search_read`; use it only after repo context is loaded or for runtime verification.
- Never push to default/main by assumption. Use `TARGET_GIT_BRANCH`.
- Never edit Odoo core, enterprise addons, `.git`, generated `.pyc`, or unrelated modules.
- Read the target module before patching.
- Commit only related files.
- Wait for the pushed SHA on Odoo.sh before upgrading the module.
- Treat pytest failures caused by missing local `odoo` package as environment failures, not module failures.

## Minimal ChatGPT tool sequence

```json
{"name":"repo.prepare","arguments":{}}
{"name":"repo.list_custom_modules","arguments":{"project_path":"<returned project_path>"}}
{"name":"repo.read","arguments":{"project_path":"<returned project_path>","relative_path":"<module>/__manifest__.py"}}
```

After reading related files, patch and deploy.
