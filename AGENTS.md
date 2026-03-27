# Agent Workflow

## Source of truth

- MCP is the system of record for task status, ownership, branch ownership, artifacts, blockers, and decisions.
- If a fact about current work is not in MCP, treat it as not current.
- The user opens or approves work in MCP and owns product direction, priorities, and risk calls.

## Role split

- Claude handles bounded implementation tasks that already exist in MCP.
- Codex reviews diffs, resolves architecture conflicts, and decides merge or deploy actions.
- The user intervenes only for product decisions, priority changes, or explicit risk tradeoffs.

## Exclusivity rules

- Do not touch code until the MCP task is claimed.
- If `claim_task` fails because someone else owns the task, stop and do not work it silently.
- Reserve a unique implementation branch in MCP with `set_branch_owner` before creating or using that branch locally.
- If branch reservation fails because another owner already has it, stop and pick a different branch or wait for reassignment.
- Keep one bounded implementation task per MCP task and one implementation branch per MCP task.
- Do not start broad exploratory work outside an MCP task unless the user explicitly asks for it.
- When scope expands, create a blocker or a follow-up task instead of silently widening the change.

## Required MCP records

Every active task should be reconstructible from MCP alone. Record these items in MCP:

- Current task owner and task status.
- Reserved branch name and branch owner.
- Latest commit or branch artifact that reflects the current working state.
- PR record when a PR exists.
- Verification artifacts relevant to the change, such as tests, screenshots, logs, or diffs.
- Open blockers and the reason they block progress.
- Decision log entries for review outcomes, architecture calls, reassignment, merge decisions, and deploy decisions.

## Standard flow

1. User opens or approves a task in MCP.
2. Claude claims the task. If the claim fails, Claude does not proceed.
3. Claude reserves a task branch in MCP, then creates or updates that branch locally.
4. Claude sets the task to `in_progress` and implements only the accepted scope.
5. Claude records progress in MCP as work advances: commits, artifacts, blockers, and handoff notes.
6. Claude records verification artifacts, updates MCP to `review`, and hands the task to Codex when the diff is ready.
7. Codex reviews the diff, resolves architecture conflicts, and records the review outcome in MCP.
8. Codex records the final merge or deploy decision in MCP and sets the task to `done` or `canceled`.

## Status expectations

- Use `todo` for work that is approved but not yet picked up.
- Use `claimed` or `in_progress` while implementation is active.
- Use `blocked` when waiting on user input, a dependency, or an architecture decision.
- Use `review` when implementation is ready for Codex review and the current state is recorded in MCP.
- Use `done` only after Codex approves the outcome or the user explicitly closes the task.
- Use `canceled` when the user or Codex decides the task should stop without shipping.

## Restart and reassignment

- Agents must be able to resume from MCP alone after a restart. On restart, read the task, branch ownership, artifacts, blockers, and decision log before doing new work.
- Record progress in MCP whenever an agent pauses, hands off, opens a PR, hits a blocker, or finishes a meaningful checkpoint.
- To force-reassign a task, record a decision log entry with the reason, move the task to `blocked` or `todo`, release the task, and let the new assignee claim it.
- If the original branch cannot be reassigned because MCP branch ownership is already held, create a successor branch, reserve that new branch in MCP, and record the handoff reason in the decision log.

## Tooling constraint

- This workflow uses standard MCP and Codex CLI capabilities only. Do not require repo-specific wrappers, custom server patches, or local monkey-patching to operate the process.
