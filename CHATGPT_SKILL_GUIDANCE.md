# ChatGPT Custom Instructions — V14 Self-Learning Agent

## What to put in ChatGPT "Custom Instructions"

```
Before any task, call workflow.agent_instructions first and follow exactly.
```

That's it. One line. Everything else is in the connector.

## What NOT to put in Custom Instructions
- No workflow steps
- No tool lists
- No Odoo rules
- No imports list

All of that lives in `GET /agent-instructions` returned by the connector at runtime.
ChatGPT reads it fresh every session — no stale skill needed.

## V14 New Tools (automatically included in agent-instructions)

| Tool | Purpose |
|---|---|
| `workflow.find_core_file` | Find real Odoo core source before _inherit |
| `workflow.find_similar_view` | Find real view XML before xpath writing |

The agent calls these automatically when the instructions say to — no extra ChatGPT config.
