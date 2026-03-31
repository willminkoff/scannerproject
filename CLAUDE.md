# Claude Instructions

Follow the shared workflow in `AGENTS.md`.

Your role in this repo is bounded implementation, not product ownership.

- Only pick up work that exists in MCP and has been opened or approved by the user.
- Before editing code, claim the task in MCP and reserve a unique branch in MCP for that task. If either step fails, stop.
- Keep your branch scoped to one MCP task and keep your implementation bounded to the task acceptance criteria.
- Update MCP as you work: status changes, branch ownership, commits, blockers, test evidence, screenshots, logs, and review handoff notes.
- If you pause or restart, resume from MCP state first instead of relying on prior chat context.
- If the change starts to become architectural, cross-cutting, or risky, stop expanding scope and hand the decision to Codex or the user through MCP.
- When implementation is ready, record verification artifacts, set the task to `review`, and hand off to Codex for diff review.
- Do not merge, deploy, or make final product or risk calls unless the user explicitly overrides this workflow.
