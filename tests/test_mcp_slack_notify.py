import unittest
from argparse import Namespace
from pathlib import Path

from scripts import mcp_slack_notify


class FakeClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {}


class SlackNotifyTests(unittest.TestCase):
    def test_matches_expected_passphrase_normalizes_common_reply_formats(self):
        self.assertTrue(
            mcp_slack_notify._matches_expected_passphrase("Widespread Panic", "pass phrase: widespread panic!")
        )
        self.assertTrue(
            mcp_slack_notify._matches_expected_passphrase("Widespread Panic", "Widespread   Panic")
        )
        self.assertFalse(
            mcp_slack_notify._matches_expected_passphrase("Widespread Panic", "panic")
        )

    def test_latest_status_transition_id_prefers_current_status_event(self):
        task = {"status": "review", "updated_at": "2026-03-27 14:55:45.409"}
        events = [
            {
                "id": 48,
                "entity_type": "task",
                "action": "status_changed",
                "details": '{"from":"in_progress","to":"review"}',
            },
            {
                "id": 37,
                "entity_type": "task",
                "action": "status_changed",
                "details": '{"from":"claimed","to":"in_progress"}',
            },
        ]

        fingerprint = mcp_slack_notify._latest_status_transition_id(task, events)

        self.assertEqual("48", fingerprint)

    def test_latest_summary_uses_decision_summary(self):
        events = [
            {
                "entity_type": "decision",
                "action": "recorded",
                "details": '{"summary":"Task is ready for review."}',
            }
        ]

        self.assertEqual("Task is ready for review.", mcp_slack_notify._latest_summary(events))

    def test_focus_events_for_status_ignores_later_unrelated_updates(self):
        task = {"id": "8305deae-2283-4868-b444-e85c3df521e7", "status": "review"}
        events = [
            {
                "id": 99,
                "timestamp": "2026-03-27 15:14:37",
                "entity_id": "other-task",
                "entity_type": "decision",
                "action": "recorded",
                "details": '{"summary":"Unrelated later decision."}',
            },
            {
                "id": 51,
                "timestamp": "2026-03-27 14:55:52",
                "entity_id": "artifact-1",
                "entity_type": "artifact",
                "action": "commit_recorded",
                "details": '{"sha":"b124b86"}',
            },
            {
                "id": 48,
                "timestamp": "2026-03-27 14:55:45",
                "entity_id": "8305deae-2283-4868-b444-e85c3df521e7",
                "entity_type": "task",
                "action": "status_changed",
                "details": '{"from":"in_progress","to":"review"}',
            },
            {
                "id": 47,
                "timestamp": "2026-03-27 14:55:45",
                "entity_id": "decision-1",
                "entity_type": "decision",
                "action": "recorded",
                "details": '{"summary":"Ready for review."}',
            },
        ]

        focused = mcp_slack_notify._focus_events_for_status(task, events, window_seconds=120)

        self.assertEqual("b124b86", mcp_slack_notify._latest_commit(focused))
        self.assertEqual("Ready for review.", mcp_slack_notify._latest_summary(focused))

    def test_focus_events_for_status_anchors_to_current_task_id(self):
        task = {"id": "5ba22959-5acf-4146-989c-1404963eaf0c", "status": "review"}
        events = [
            {
                "id": 129,
                "timestamp": "2026-03-27 23:25:59",
                "entity_id": "d1fd08a4-b316-4502-b7b9-50f9a79da617",
                "entity_type": "task",
                "action": "status_changed",
                "details": '{"from":"in_progress","to":"review"}',
            },
            {
                "id": 128,
                "timestamp": "2026-03-27 23:25:59",
                "entity_id": "decision-later",
                "entity_type": "decision",
                "action": "recorded",
                "details": '{"summary":"Wrong later summary."}',
            },
            {
                "id": 109,
                "timestamp": "2026-03-27 21:28:21",
                "entity_id": "5ba22959-5acf-4146-989c-1404963eaf0c",
                "entity_type": "task",
                "action": "status_changed",
                "details": '{"from":"claimed","to":"review"}',
            },
            {
                "id": 108,
                "timestamp": "2026-03-27 21:28:21",
                "entity_id": "decision-correct",
                "entity_type": "decision",
                "action": "recorded",
                "details": '{"summary":"Correct task summary."}',
            },
            {
                "id": 107,
                "timestamp": "2026-03-27 21:28:21",
                "entity_id": "artifact-correct",
                "entity_type": "artifact",
                "action": "commit_recorded",
                "details": '{"sha":"abc1234"}',
            },
        ]

        focused = mcp_slack_notify._focus_events_for_status(task, events, window_seconds=120)

        self.assertEqual("abc1234", mcp_slack_notify._latest_commit(focused))
        self.assertEqual("Correct task summary.", mcp_slack_notify._latest_summary(focused))

    def test_task_payload_includes_commit_status_and_reply_help(self):
        task = {
            "id": "8305deae-2283-4868-b444-e85c3df521e7",
            "title": "OP25 hit pipeline",
            "status": "review",
            "owner_id": "claude",
            "priority": 92,
            "description": "Fix the hit route filter.",
            "acceptance_criteria": "Task reaches review with artifacts.",
        }
        events = [
            {
                "entity_type": "artifact",
                "action": "commit_recorded",
                "details": '{"sha":"b124b86"}',
            },
            {
                "entity_type": "decision",
                "action": "recorded",
                "details": '{"summary":"Ready for review."}',
            },
        ]

        payload = mcp_slack_notify._task_payload(
            "/repo",
            task,
            events,
            reply_enabled=True,
            command_help=mcp_slack_notify.THREAD_COMMANDS,
        )

        self.assertIn("REVIEW", payload["text"])
        self.assertIn("b124b86", str(payload["blocks"]))
        self.assertIn("Reply in thread", payload["text"])
        self.assertIn("[repo]", payload["text"])

    def test_task_payload_can_render_dm_help(self):
        task = {
            "id": "8305deae-2283-4868-b444-e85c3df521e7",
            "title": "OP25 hit pipeline",
            "status": "review",
            "owner_id": "claude",
            "priority": 92,
            "description": "Fix the hit route filter.",
            "acceptance_criteria": "Task reaches review with artifacts.",
        }

        payload = mcp_slack_notify._task_payload(
            "/repo",
            task,
            [],
            reply_enabled=True,
            command_help=mcp_slack_notify.DM_COMMANDS,
        )

        self.assertIn("task <id>", payload["text"])

    def test_parse_reply_command_supports_reassign_reason(self):
        command = mcp_slack_notify._parse_reply_command("reassign: claude | finish this next")

        self.assertEqual(
            {"action": "reassign", "owner": "claude", "reason": "finish this next"},
            command,
        )

    def test_parse_reply_command_understands_conversational_approval(self):
        command = mcp_slack_notify._parse_reply_command("looks good to me")

        self.assertEqual(
            {"action": "decision", "summary": "Approved in Slack."},
            command,
        )

    def test_parse_reply_command_understands_push_and_deploy(self):
        self.assertEqual({"action": "push"}, mcp_slack_notify._parse_reply_command("push"))
        self.assertEqual({"action": "deploy"}, mcp_slack_notify._parse_reply_command("deploy"))
        self.assertEqual({"action": "push_deploy"}, mcp_slack_notify._parse_reply_command("push and deploy"))

    def test_parse_dm_command_extracts_task_ref(self):
        command = mcp_slack_notify._parse_dm_command("task 8305deae decision: approve it")

        self.assertEqual(
            {"action": "decision", "summary": "approve it", "task_ref": "8305deae"},
            command,
        )

    def test_parse_dm_command_understands_review_queue_question(self):
        command = mcp_slack_notify._parse_dm_command("what needs review?")

        self.assertEqual({"action": "tasks"}, command)

    def test_parse_dm_command_understands_team_status_question(self):
        command = mcp_slack_notify._parse_dm_command("who is doing what?")

        self.assertEqual({"action": "team_status"}, command)

    def test_parse_dm_command_understands_owner_summary_question(self):
        command = mcp_slack_notify._parse_dm_command("what is claude working on?")

        self.assertEqual({"action": "owner_summary", "owner": "claude"}, command)

    def test_parse_dm_command_understands_assign_next_request(self):
        command = mcp_slack_notify._parse_dm_command("have claude take the next task")

        self.assertEqual({"action": "assign_next", "owner": "claude"}, command)

    def test_parse_dm_command_understands_new_task_request(self):
        command = mcp_slack_notify._parse_dm_command("new task. Review contents of the repo /dillard-it-transition")

        self.assertEqual(
            {"action": "create_task", "request": "Review contents of the repo /dillard-it-transition"},
            command,
        )

    def test_parse_dm_command_defaults_next_task_to_claude(self):
        command = mcp_slack_notify._parse_dm_command("take the next task")

        self.assertEqual({"action": "assign_next", "owner": "claude"}, command)

    def test_parse_dm_command_can_find_task_id_inside_sentence(self):
        command = mcp_slack_notify._parse_dm_command("approve 8305deae")

        self.assertEqual(
            {"action": "decision", "summary": "Approved in Slack.", "task_ref": "8305deae"},
            command,
        )

    def test_format_owner_tasks_summarizes_active_work(self):
        class WorkspaceClient:
            def call_tool(self, name, arguments):
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "list_tasks":
                    return [
                        {
                            "id": "8305deae-2283-4868-b444-e85c3df521e7",
                            "status": "review",
                            "owner_id": "claude",
                            "priority": 92,
                            "title": "OP25 hit pipeline",
                        },
                        {
                            "id": "4d7a3e1a-122a-418b-b714-bed15da30326",
                            "status": "blocked",
                            "owner_id": "claude",
                            "priority": 75,
                            "title": "OP25 whitelist",
                        },
                    ]
                raise AssertionError(name)

        text = mcp_slack_notify._format_owner_tasks(
            WorkspaceClient(),
            "/Users/willminkoff/Documents/scannerproject",
            "claude",
        )

        self.assertIn("claude is currently handling in scannerproject", text)
        self.assertIn("8305deae REVIEW OP25 hit pipeline", text)
        self.assertIn("4d7a3e1a BLOCKED OP25 whitelist", text)

    def test_format_team_status_summarizes_claude_codex_and_attention(self):
        class WorkspaceClient:
            def call_tool(self, name, arguments):
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "list_tasks":
                    return [
                        {
                            "id": "10f18ba7-8519-4274-9817-81b23948c37e",
                            "status": "review",
                            "owner_id": "claude",
                            "priority": 95,
                            "title": "Fix OP25 runtime system mismatch on Micro",
                        },
                        {
                            "id": "9dcd3e52-c223-43bf-8add-7d37fbc98ac3",
                            "status": "blocked",
                            "owner_id": "codex",
                            "priority": 96,
                            "title": "Restore live OP25 digital hits after runtime coherence",
                        },
                    ]
                if name == "get_event_log":
                    return [
                        {
                            "timestamp": "2026-03-28 02:58:39",
                            "entity_type": "task",
                            "entity_id": "9dcd3e52-c223-43bf-8add-7d37fbc98ac3",
                            "action": "status_changed",
                            "actor": "codex",
                            "details": '{"from":"blocked","to":"in_progress"}',
                        },
                        {
                            "timestamp": "2026-03-28 01:32:01",
                            "entity_type": "task",
                            "entity_id": "10f18ba7-8519-4274-9817-81b23948c37e",
                            "action": "status_changed",
                            "actor": "claude",
                            "details": '{"from":"claimed","to":"review"}',
                        },
                    ]
                raise AssertionError(name)

        text = mcp_slack_notify._format_team_status(
            WorkspaceClient(),
            "/Users/willminkoff/Documents/scannerproject",
        )

        self.assertIn("Team status in scannerproject", text)
        self.assertIn("PROGRESS IS HALTED", text)
        self.assertIn("- Claude active: 10f18ba7 REVIEW Fix OP25 runtime system mismatch on Micro", text)
        self.assertIn("- Last Claude action: 2026-03-28 01:32:01 moved 10f18ba7 to REVIEW", text)
        self.assertIn("- Codex active: 9dcd3e52 BLOCKED Restore live OP25 digital hits after runtime coherence", text)
        self.assertIn("- Last Codex action: 2026-03-28 02:58:39 moved 9dcd3e52 to IN_PROGRESS", text)
        self.assertIn(
            "- Needs attention: 9dcd3e52 BLOCKED Restore live OP25 digital hits after runtime coherence; 10f18ba7 REVIEW Fix OP25 runtime system mismatch on Micro",
            text,
        )
        self.assertIn(
            "- Blocked work: 9dcd3e52 BLOCKED Restore live OP25 digital hits after runtime coherence",
            text,
        )

    def test_format_team_status_flags_stale_active_work_as_halted(self):
        class WorkspaceClient:
            def call_tool(self, name, arguments):
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "list_tasks":
                    return [
                        {
                            "id": "10f18ba7-8519-4274-9817-81b23948c37e",
                            "status": "in_progress",
                            "owner_id": "claude",
                            "priority": 95,
                            "title": "Fix OP25 runtime system mismatch on Micro",
                            "updated_at": "2026-03-28 01:32:01",
                        }
                    ]
                if name == "get_event_log":
                    return []
                raise AssertionError(name)

        text = mcp_slack_notify._format_team_status(
            WorkspaceClient(),
            "/Users/willminkoff/Documents/scannerproject",
        )

        self.assertIn("PROGRESS IS HALTED", text)
        self.assertIn(
            "- Stalled work: 10f18ba7 IN_PROGRESS Fix OP25 runtime system mismatch on Micro",
            text,
        )

    def test_assign_next_task_to_owner_claims_unique_highest_priority_todo(self):
        class WorkspaceClient(FakeClient):
            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "list_tasks":
                    return [
                        {
                            "id": "3274d57f-fe8b-4d21-81eb-053866187f92",
                            "status": "todo",
                            "owner_id": None,
                            "priority": 90,
                            "title": "Analog validation",
                        },
                        {
                            "id": "11111111-fe8b-4d21-81eb-053866187f92",
                            "status": "todo",
                            "owner_id": None,
                            "priority": 40,
                            "title": "Lower priority task",
                        },
                    ]
                return {}

        client = WorkspaceClient()
        ack = mcp_slack_notify._assign_next_task_to_owner(
            client,
            "/Users/willminkoff/Documents/scannerproject",
            "claude",
            slack_user_id="U123",
        )

        self.assertEqual("Assigned next task 3274d57f to claude in scannerproject.", ack)
        self.assertEqual("claim_task", client.calls[2][0])
        self.assertEqual("claude", client.calls[2][1]["owner_id"])
        self.assertEqual("append_decision_log", client.calls[3][0])

    def test_assign_next_task_to_owner_refuses_priority_tie(self):
        class WorkspaceClient:
            def call_tool(self, name, arguments):
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "list_tasks":
                    return [
                        {
                            "id": "3274d57f-fe8b-4d21-81eb-053866187f92",
                            "status": "todo",
                            "owner_id": None,
                            "priority": 90,
                            "title": "Analog validation",
                        },
                        {
                            "id": "22222222-fe8b-4d21-81eb-053866187f92",
                            "status": "todo",
                            "owner_id": None,
                            "priority": 90,
                            "title": "Equally urgent task",
                        },
                    ]
                raise AssertionError(name)

        ack = mcp_slack_notify._assign_next_task_to_owner(
            WorkspaceClient(),
            "/Users/willminkoff/Documents/scannerproject",
            "claude",
            slack_user_id="U123",
        )

        self.assertIn("didn't assign a next task in scannerproject", ack)
        self.assertIn("3274d57f", ack)
        self.assertIn("22222222", ack)

    def test_assign_reply_claims_unowned_task_and_logs_decision(self):
        client = FakeClient()
        task = {
            "id": "3274d57f-fe8b-4d21-81eb-053866187f92",
            "status": "todo",
            "owner_id": None,
        }

        ack = mcp_slack_notify._apply_reply_command(
            client,
            task,
            {"action": "assign", "owner": "claude"},
            repo_root="/Users/willminkoff/Documents/scannerproject",
            slack_user_id="U123",
        )

        self.assertEqual("Applied to MCP: 3274d57f is now assigned to claude.", ack)
        self.assertEqual("claim_task", client.calls[0][0])
        self.assertEqual("claude", client.calls[0][1]["owner_id"])
        self.assertEqual("append_decision_log", client.calls[1][0])

    def test_push_reply_attaches_log_and_records_decision(self):
        client = FakeClient()
        task = {
            "id": "8305deae-2283-4868-b444-e85c3df521e7",
            "status": "review",
            "owner_id": "codex",
        }

        original_workspace_for_repo = mcp_slack_notify.mcp_queue._workspace_for_repo
        original_run_action_command = mcp_slack_notify._run_action_command
        try:
            mcp_slack_notify.mcp_queue._workspace_for_repo = lambda client, repo_root: {"id": "workspace-1"}
            mcp_slack_notify._run_action_command = lambda repo_root, action: Path("/tmp/push.log")
            ack = mcp_slack_notify._apply_reply_command(
                client,
                task,
                {"action": "push"},
                repo_root="/Users/willminkoff/Documents/scannerproject",
                slack_user_id="U123",
            )
        finally:
            mcp_slack_notify.mcp_queue._workspace_for_repo = original_workspace_for_repo
            mcp_slack_notify._run_action_command = original_run_action_command

        self.assertEqual("Applied to MCP: 8305deae push completed.", ack)
        self.assertEqual("attach_artifact", client.calls[0][0])
        self.assertEqual("append_decision_log", client.calls[1][0])

    def test_parse_reply_command_understands_have_claude_take_this(self):
        command = mcp_slack_notify._parse_reply_command("have claude take this")

        self.assertEqual({"action": "assign", "owner": "claude"}, command)

    def test_parse_reply_command_understands_work_on_as_assign_claude(self):
        command = mcp_slack_notify._parse_reply_command("work on")

        self.assertEqual({"action": "assign", "owner": "claude"}, command)

    def test_reassign_reply_releases_then_claims_and_logs_branch_note(self):
        client = FakeClient()
        task = {
            "id": "8305deae-2283-4868-b444-e85c3df521e7",
            "status": "review",
            "owner_id": "claude",
        }

        ack = mcp_slack_notify._apply_reply_command(
            client,
            task,
            {"action": "reassign", "owner": "codex", "reason": "Need architecture review now"},
            repo_root="/Users/willminkoff/Documents/scannerproject",
            slack_user_id="U999",
        )

        self.assertEqual("Applied to MCP: 8305deae reassigned to codex.", ack)
        self.assertEqual(
            ["append_decision_log", "release_task", "claim_task", "append_decision_log"],
            [name for name, _args in client.calls],
        )
        self.assertIn("new MCP branch", client.calls[-1][1]["rationale"])

    def test_unblock_reply_moves_blocked_owned_task_to_claimed(self):
        client = FakeClient()
        task = {
            "id": "4d7a3e1a-122a-418b-b714-bed15da30326",
            "status": "blocked",
            "owner_id": "claude",
        }

        ack = mcp_slack_notify._apply_reply_command(
            client,
            task,
            {"action": "unblock", "summary": "Proceed with the next step."},
            repo_root="/Users/willminkoff/Documents/scannerproject",
            slack_user_id="U123",
        )

        self.assertEqual("Applied to MCP: 4d7a3e1a is now claimed.", ack)
        self.assertEqual("set_blocker", client.calls[0][0])
        self.assertEqual("closed", client.calls[0][1]["status"])
        self.assertEqual("update_task_status", client.calls[1][0])
        self.assertEqual("claimed", client.calls[1][1]["status"])

    def test_tracked_task_lookup_indexes_short_ids(self):
        lookup = mcp_slack_notify._tracked_task_lookup(
            {
                "8305deae-2283-4868-b444-e85c3df521e7": {"thread_ts": "1"},
                "4d7a3e1a-122a-418b-b714-bed15da30326": {"thread_ts": "2"},
            }
        )

        self.assertEqual(
            {
                "8305deae": "8305deae-2283-4868-b444-e85c3df521e7",
                "4d7a3e1a": "4d7a3e1a-122a-418b-b714-bed15da30326",
            },
            lookup,
        )

    def test_workspace_task_lookup_indexes_all_workspace_tasks(self):
        class WorkspaceClient:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "list_tasks":
                    return [
                        {"id": "8305deae-2283-4868-b444-e85c3df521e7"},
                        {"id": "4d7a3e1a-122a-418b-b714-bed15da30326"},
                    ]
                raise AssertionError(name)

        lookup = mcp_slack_notify._workspace_task_lookup(WorkspaceClient(), "/repo")

        self.assertEqual(
            {
                "8305deae": "8305deae-2283-4868-b444-e85c3df521e7",
                "4d7a3e1a": "4d7a3e1a-122a-418b-b714-bed15da30326",
            },
            lookup,
        )

    def test_open_task_from_request_creates_mcp_task(self):
        class WorkspaceClient(FakeClient):
            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "get_workspace":
                    return {"id": "workspace-1"}
                if name == "create_task":
                    return {
                        "id": "12345678-fe8b-4d21-81eb-053866187f92",
                        "title": arguments["title"],
                    }
                return {}

        client = WorkspaceClient()
        created = mcp_slack_notify._open_task_from_request(
            client,
            "/Users/willminkoff/Documents/dillard-it-transition",
            "Review contents of the repo /dillard-it-transition",
            slack_user_id="U123",
        )

        self.assertEqual("12345678-fe8b-4d21-81eb-053866187f92", created["id"])
        self.assertEqual("create_task", client.calls[1][0])
        self.assertIn("Review contents of the repo", client.calls[1][1]["title"])
        self.assertEqual("append_decision_log", client.calls[2][0])

    def test_process_dm_replies_accepts_expected_passphrase(self):
        args = Namespace(
            reply_token="token",
            post_token="token",
            channel="D123",
            repo_root="/Users/willminkoff/Documents/scannerproject",
        )
        state = {"dm_expected_passphrase": "Widespread Panic", "threads": {}, "notified": {}}
        acknowledgements: list[tuple[str, str | None]] = []

        def fake_api_call(method, token, payload):
            self.assertEqual("conversations.history", method)
            self.assertEqual("D123", payload["channel"])
            return {
                "ok": True,
                "messages": [
                    {"ts": "200.0", "user": "U123", "text": "pass phrase: Widespread Panic!", "thread_ts": "200.0"}
                ],
            }

        def fake_post_ack(passed_args, channel, text, *, thread_ts=None):
            self.assertIs(args, passed_args)
            self.assertEqual("D123", channel)
            acknowledgements.append((text, thread_ts))
            return "201.0"

        original_api_call = mcp_slack_notify._slack_api_call
        original_post_ack = mcp_slack_notify._post_ack
        try:
            mcp_slack_notify._slack_api_call = fake_api_call
            mcp_slack_notify._post_ack = fake_post_ack

            processed = mcp_slack_notify._process_dm_replies(args, FakeClient(), state)
        finally:
            mcp_slack_notify._slack_api_call = original_api_call
            mcp_slack_notify._post_ack = original_post_ack

        self.assertEqual(
            ["Pass phrase accepted for scannerproject. DM replies are working."],
            processed,
        )
        self.assertEqual(
            [("Pass phrase accepted for scannerproject. DM replies are working.", None)],
            acknowledgements,
        )
        self.assertNotIn("dm_expected_passphrase", state)
        self.assertEqual("201.0", state["dm_last_ts"])

    def test_process_dm_replies_answers_smalltalk_with_help(self):
        args = Namespace(
            reply_token="token",
            post_token="token",
            channel="D123",
            repo_root="/Users/willminkoff/Documents/scannerproject",
        )
        state = {"threads": {}, "notified": {}}
        acknowledgements: list[tuple[str, str | None]] = []

        def fake_api_call(method, token, payload):
            self.assertEqual("conversations.history", method)
            return {
                "ok": True,
                "messages": [
                    {"ts": "300.0", "user": "U123", "text": "thanks", "thread_ts": "300.0"}
                ],
            }

        def fake_post_ack(passed_args, channel, text, *, thread_ts=None):
            acknowledgements.append((text, thread_ts))
            return "301.0"

        original_api_call = mcp_slack_notify._slack_api_call
        original_post_ack = mcp_slack_notify._post_ack
        try:
            mcp_slack_notify._slack_api_call = fake_api_call
            mcp_slack_notify._post_ack = fake_post_ack

            processed = mcp_slack_notify._process_dm_replies(args, FakeClient(), state)
        finally:
            mcp_slack_notify._slack_api_call = original_api_call
            mcp_slack_notify._post_ack = original_post_ack

        self.assertEqual(
            [
                "I'm watching scannerproject. Ask `who is doing what?`, `what needs review?`, `what is claude working on?`, "
                "or `have claude take the next task`. Claude is the default implementation owner here; Codex handles "
                "review, architecture, and merge/deploy calls. You can also reply in-thread to a task message or send "
                "`task <id> approve this`."
            ],
            processed,
        )
        self.assertEqual(
            [
                (
                    "I'm watching scannerproject. Ask `who is doing what?`, `what needs review?`, `what is claude working on?`, "
                    "or `have claude take the next task`. Claude is the default implementation owner here; Codex handles "
                    "review, architecture, and merge/deploy calls. You can also reply in-thread to a task message or send "
                    "`task <id> approve this`.",
                    None,
                )
            ],
            acknowledgements,
        )
        self.assertEqual("301.0", state["dm_last_ts"])


if __name__ == "__main__":
    unittest.main()
