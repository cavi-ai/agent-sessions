import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "agent_sessions.py"
GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
       "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


def git(*args, cwd):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


class Machine(unittest.TestCase):
    """A scratch Codex home, Claude home and repository with one linked worktree."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git("init", cwd=self.repo)
        (self.repo / "a.txt").write_text("one\n")
        git("add", "a.txt", cwd=self.repo)
        git("commit", "-m", "start", cwd=self.repo)
        self.worktree = self.root / "worktree"
        git("worktree", "add", "-b", "feat/other", str(self.worktree), cwd=self.repo)
        (self.repo / "a.txt").write_text("two\n")
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        self.data = self.root / "data"
        self.env = {**os.environ, "CODEX_HOME": str(self.codex),
                    "CLAUDE_CONFIG_DIR": str(self.claude), "AGENT_SESSIONS_DATA": str(self.data)}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.env.pop("CODEX_THREAD_ID", None)
        self.now = time.time()
        self.rows = []
        self.codex.mkdir()
        db = sqlite3.connect(self.codex / "state_5.sqlite")
        db.execute("create table threads (id text primary key, rollout_path text, created_at int,"
                   " updated_at int, updated_at_ms int, source text, cwd text, title text,"
                   " name text, archived int, agent_nickname text)")
        self.thread("codex-main-0001", self.repo, "Edit the main checkout", "vscode",
                    ["task_complete", "task_started"], edited=[self.repo / "a.txt"])
        self.thread("codex-sub-0002", self.worktree, "", json.dumps(
            {"subagent": {"thread_spawn": {"parent_thread_id": "codex-main-0001"}}}),
            ["task_started", "task_complete"], nickname="Helper")
        self.thread("codex-review-0003", self.repo, "Guardian review",
                    json.dumps({"subagent": {"other": "guardian"}}), [])
        self.thread("codex-old-0004", self.repo, "Last week", "vscode", [], age=7 * 86400)
        self.thread("codex-gone-0005", self.repo, "Archived", "vscode", [], archived=1)
        for row in self.rows:
            db.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?,?)", row)
        db.commit()
        db.close()
        project = self.claude / "projects" / "-repo"
        project.mkdir(parents=True)
        (project / "claude-0006.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"type": "user", "cwd": str(self.worktree), "message": {"content": "hi"}},
            {"type": "last-prompt", "lastPrompt": "Review the worktree"},
            {"type": "assistant", "cwd": str(self.worktree), "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": str(self.worktree / "a.txt")}}]}},
        ]) + "\n")

    def thread(self, thread_id, cwd, title, source, turns, edited=(), nickname=None, age=0,
               archived=0):
        rollout = self.codex / f"{thread_id}.jsonl"
        records = [{"type": "event_msg", "payload": {"type": turn}} for turn in turns]
        records += [{"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "FileChange", "changes": {str(path): {"type": "update"}}}}} for path in edited]
        rollout.write_text("".join(json.dumps(r) + "\n" for r in records))
        updated = self.now - age
        self.rows.append((thread_id, str(rollout), int(updated), int(updated), int(updated * 1000),
                          source, str(cwd), title, None, archived, nickname))

    def run_tool(self, *args, stdin=None, env=None):
        result = subprocess.run([sys.executable, str(TOOL), *args], input=stdin, text=True,
                                capture_output=True, env=env or self.env, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class ListTests(Machine):
    def test_lists_active_sessions_with_their_worktree_and_turn_state(self):
        found = {s["id"]: s for s in json.loads(self.run_tool("--json", "list"))}
        self.assertEqual(set(found), {"codex-main-0001", "codex-sub-0002", "claude-0006"})
        main, sub, claude = found["codex-main-0001"], found["codex-sub-0002"], found["claude-0006"]
        self.assertEqual((main["status"], main["git"]["root"], main["git"]["linked"],
                          main["git"]["branch"], main["git"]["dirty"]),
                         ("running", str(self.repo), False, "main", ["a.txt"]))
        self.assertEqual((sub["status"], sub["title"], sub["parent"], sub["git"]["root"],
                          sub["git"]["linked"], sub["git"]["branch"]),
                         ("idle", "Helper", "codex-main-0001", str(self.worktree), True, "feat/other"))
        self.assertEqual((claude["host"], claude["title"], claude["edited"]),
                         ("claude", "Review the worktree", [str(self.worktree / "a.txt")]))

    def test_reviews_appear_only_when_asked(self):
        found = {s["id"] for s in json.loads(self.run_tool("--json", "list", "--reviews"))}
        self.assertIn("codex-review-0003", found)


class WhoTests(Machine):
    def test_names_sessions_in_the_same_repository_and_the_files_they_change(self):
        result = json.loads(self.run_tool("--json", "who", str(self.repo)))
        peers = {s["id"]: (s["same_worktree"], s["overlap"]) for s in result["sessions"]}
        self.assertEqual(peers, {"codex-main-0001": (True, ["a.txt"]),
                                 "codex-sub-0002": (False, []),
                                 "claude-0006": (False, ["a.txt"])})


class PartnerTests(Machine):
    def test_claude_invitation_is_not_consumed_by_codex_in_the_same_directory(self):
        pair = json.loads(self.run_tool("partner", "claude", str(self.worktree), "Claude only",
                                       "--session", "codex-main-0001"))
        payload = json.dumps({"session_id": "codex-sub-0002", "cwd": str(self.worktree),
                              "hook_event_name": "PostToolUse"})
        self.assertEqual(self.run_tool("hook", "--host", "codex", stdin=payload), "")
        self.assertEqual(json.loads(self.run_tool("--json", "inbox", "--session",
                                                 "codex-sub-0002")), [])
        started = json.dumps({"session_id": "new-claude-0009", "cwd": str(self.worktree),
                              "hook_event_name": "SessionStart", "source": "startup"})
        context = json.loads(self.run_tool("hook", "--host", "claude", stdin=started))[
            "hookSpecificOutput"]["additionalContext"]
        self.assertIn(pair["id"], context)

    def test_bad_pair_metadata_does_not_hide_other_sessions_or_messages(self):
        pair = json.loads(self.run_tool("partner", "claude", str(self.worktree), "Invitation",
                                       "--session", "codex-main-0001"))
        self.run_tool("send", "claude-0006", "ordinary message", "--session", "codex-main-0001")
        (self.data / "pairs" / f"{pair['id']}.json").unlink()
        (self.data / "pairs" / ("a" * 32 + ".json")).write_text("broken json")
        sessions = json.loads(self.run_tool("--json", "list"))
        self.assertEqual({s["id"] for s in sessions},
                         {"codex-main-0001", "codex-sub-0002", "claude-0006"})
        messages = json.loads(self.run_tool("--json", "inbox", "--session", "claude-0006"))
        self.assertEqual([m["text"] for m in messages], ["ordinary message"])

    def test_codex_creation_request_connects_to_a_discovered_session(self):
        caller = {**self.env, "CLAUDE_CODE_SESSION_ID": "claude-0006"}
        pair = json.loads(self.run_tool("--json", "partner", "codex", str(self.repo),
                                       "Check a.txt", "--project-id", "project-001", env=caller))
        self.assertEqual(pair["status"], "needs_creation")
        request = pair["request"]
        self.assertEqual(request["tool"], "mcp__codex_app__create_thread")
        self.assertEqual(request["arguments"]["target"], {
            "type": "project", "projectId": "project-001", "environment": {"type": "local"}})
        self.assertIn("Check a.txt", request["arguments"]["prompt"])
        self.assertIn(pair["id"], request["arguments"]["prompt"])
        self.assertIsNone(pair["partner"]["id"])
        self.assertEqual(json.loads(self.run_tool("partner", "show", pair["id"])), pair)
        connected = json.loads(self.run_tool("--json", "partner", "connect", pair["id"],
                                            "codex-main-0001", "--host-id", "local", env=caller))
        self.assertEqual(connected["status"], "connected")
        self.assertEqual(connected["partner"]["id"], "codex-main-0001")
        self.assertEqual(json.loads(self.run_tool("--json", "partner", "connect", pair["id"],
                                                 "codex-main-0001", env=caller)), connected)
        sessions = {s["id"]: s for s in json.loads(self.run_tool("--json", "list"))}
        self.assertEqual(sessions["claude-0006"]["partners"][0]["id"], "codex-main-0001")
        self.assertEqual(sessions["codex-main-0001"]["partners"][0]["id"], "claude-0006")
        self.assertIn("partner claude", self.run_tool("list"))

    def test_claude_brief_waits_for_a_future_session_and_records_its_identity(self):
        target = self.root / "new-project"
        target.mkdir()
        caller = {**self.env, "CODEX_THREAD_ID": "codex-main-0001"}
        pair = json.loads(self.run_tool("--json", "partner", "claude", str(target),
                                       "Review the parser", env=caller))
        self.assertEqual(pair["status"], "waiting")
        self.assertNotIn("request", pair)
        project = self.claude / "projects" / "new-project"
        project.mkdir()
        (project / "future-claude-0007.jsonl").write_text(json.dumps({
            "type": "user", "cwd": str(target), "message": {"content": "hello"}}) + "\n")
        recipient = {**self.env, "CLAUDE_CODE_SESSION_ID": "future-claude-0007"}
        payload = json.dumps({"session_id": "future-claude-0007", "cwd": str(target),
                              "hook_event_name": "SessionStart"})
        received = json.loads(self.run_tool("hook", "--host", "claude", stdin=payload,
                                            env=recipient))
        context = received["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Review the parser", context)
        self.assertIn(pair["id"], context)
        self.assertIn("partner connect", context)
        connected = json.loads(self.run_tool("--json", "partner", "connect", pair["id"],
                                            "future-claude-0007", env=recipient))
        self.assertEqual(connected["status"], "connected")
        sessions = {s["id"]: s for s in json.loads(self.run_tool("--json", "list"))}
        self.assertEqual(sessions["codex-main-0001"]["partners"][0]["id"], "future-claude-0007")
        self.assertEqual(sessions["future-claude-0007"]["partners"][0]["id"], "codex-main-0001")
        later = json.dumps({"session_id": "later-claude-0008", "cwd": str(target),
                            "hook_event_name": "PostToolUse"})
        self.assertEqual(self.run_tool("hook", stdin=later), "")

    def test_parent_can_add_returned_host_after_the_partner_connects_itself(self):
        caller = {**self.env, "CLAUDE_CODE_SESSION_ID": "claude-0006"}
        peer = {**self.env, "CODEX_THREAD_ID": "codex-main-0001"}
        pair = json.loads(self.run_tool("partner", "codex", str(self.repo), "Review",
                                       "--project-id", "project-001", env=caller))
        self.run_tool("partner", "connect", pair["id"], env=peer)
        connected = json.loads(self.run_tool("partner", "connect", pair["id"], "codex-main-0001",
                                            "--host-id", "local", env=caller))
        self.assertEqual(connected["partner"]["host_id"], "local")

    def test_connection_rejects_unknown_sessions_wrong_hosts_and_wrong_directories(self):
        pair = json.loads(self.run_tool("--json", "partner", "codex", str(self.repo), "Review",
                                       "--project-id", "project-001", "--session", "claude-0006"))
        for peer in ("not-a-real-session", "claude-0006", "codex-sub-0002"):
            with self.subTest(peer=peer):
                result = subprocess.run([sys.executable, str(TOOL), "partner", "connect",
                                         pair["id"], peer, "--session", "claude-0006"],
                                        env=self.env, text=True, capture_output=True,
                                        check=False, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                saved = json.loads((self.data / "pairs" / f"{pair['id']}.json").read_text())
                self.assertEqual(saved["status"], "needs_creation")

    def test_pending_pairs_appear_in_list_without_an_invented_recipient(self):
        pair = json.loads(self.run_tool("--json", "partner", "claude", str(self.worktree),
                                       "Review", "--session", "codex-main-0001"))
        self.assertIsNone(pair["partner"]["id"])
        sessions = {s["id"]: s for s in json.loads(self.run_tool("--json", "list"))}
        pending = sessions["codex-main-0001"]["partners"][0]
        self.assertEqual((pending["status"], pending["host"], pending["id"]),
                         ("waiting", "claude", None))
        for path in (self.data / "pairs").iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_queue_failure_preserves_the_invitation_without_claiming_delivery(self):
        self.data.mkdir()
        (self.data / "queue").write_text("not a directory")
        result = subprocess.run([sys.executable, str(TOOL), "partner", "claude",
                                 str(self.worktree), "Preserve this brief", "--session",
                                 "codex-main-0001"], env=self.env, text=True,
                                capture_output=True, check=False, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        records = list((self.data / "pairs").glob("*.json"))
        self.assertEqual(len(records), 1)
        saved = json.loads(records[0].read_text())
        self.assertEqual(saved["status"], "needs_delivery")
        self.assertIn("Preserve this brief", saved["invitation"])


class MessageTests(Machine):
    def test_codex_and_claude_can_reply_using_their_native_session_identity(self):
        codex = {**self.env, "CODEX_THREAD_ID": "codex-main-0001"}
        claude = {**self.env, "CLAUDE_CODE_SESSION_ID": "claude-0006"}
        sent = json.loads(self.run_tool("--json", "send", "claude-0006", "Please check a.txt",
                                        env=codex))
        self.assertEqual(sent["message"]["from_id"], "codex-main-0001")
        received = json.loads(self.run_tool("--json", "inbox", env=claude))
        self.assertEqual([m["text"] for m in received], ["Please check a.txt"])
        self.run_tool("send", received[0]["from_id"], "Checked a.txt", env=claude)
        payload = json.dumps({"thread_id": "codex-main-0001", "cwd": str(self.repo),
                              "hook_event_name": "PostToolUse"})
        reply = json.loads(self.run_tool("hook", stdin=payload, env=codex))
        self.assertIn("Checked a.txt", reply["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_tool("hook", stdin=payload, env=codex), "")
        self.assertEqual(json.loads(self.run_tool("--json", "inbox", env=codex)), [])
        self.assertIn("(this session)", self.run_tool("list", env=codex))

    def test_delivery_does_not_repeat_after_more_than_500_messages(self):
        inbox = self.data / "inbox" / "codex-main-0001.jsonl"
        inbox.parent.mkdir(parents=True)
        inbox.write_text("".join(json.dumps({"id": str(i), "text": "hello", "from": "peer",
                                            "at": "2026-01-01T00:00:00Z"}) + "\n"
                                 for i in range(502)))
        self.assertEqual(len(json.loads(self.run_tool("--json", "inbox", "--session",
                                                     "codex-main-0001"))), 502)
        self.assertEqual(json.loads(self.run_tool("--json", "inbox", "--session",
                                                 "codex-main-0001")), [])

    def test_invalid_session_ids_cannot_escape_the_data_directory(self):
        result = subprocess.run([sys.executable, str(TOOL), "inbox", "--session", "../../escape"],
                                env=self.env, capture_output=True, text=True, timeout=30, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid session id", result.stderr)

    def test_message_storage_is_private(self):
        self.run_tool("send", "codex-main-0001", "private message")
        self.run_tool("inbox", "--session", "codex-main-0001")
        for path in self.data.rglob("*"):
            with self.subTest(path=path.relative_to(self.data)):
                self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.data.stat().st_mode & 0o077, 0)

    def test_bad_message_file_does_not_hide_other_messages(self):
        self.run_tool("send", "codex-main-0001", "first")
        queue = self.data / "queue" / "s-codex-main-0001" / "new"
        (queue / "0-broken.json").write_text("broken json")
        self.run_tool("send", "codex-main-0001", "second")
        messages = json.loads(self.run_tool("--json", "inbox", "--session", "codex-main-0001"))
        self.assertEqual([m["text"] for m in messages], ["first", "second"])

    def test_corrupt_delivery_state_is_reported_without_replaying_messages(self):
        self.run_tool("send", "codex-main-0001", "already delivered")
        self.run_tool("inbox", "--session", "codex-main-0001")
        state = self.data / "state" / "codex-main-0001.json"
        state.parent.mkdir(exist_ok=True)
        state.write_text("broken json")
        result = subprocess.run([sys.executable, str(TOOL), "inbox", "--session", "codex-main-0001"],
                                env=self.env, capture_output=True, text=True, timeout=30, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("delivery state", result.stderr)
        self.assertEqual(state.read_text(), "broken json")

    def test_concurrent_readers_consume_a_message_only_once(self):
        self.run_tool("send", "codex-main-0001", "one delivery")
        command = [sys.executable, str(TOOL), "--json", "inbox", "--session", "codex-main-0001"]
        processes = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(2)]
        messages = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            messages.extend(json.loads(stdout))
        self.assertEqual([m["text"] for m in messages], ["one delivery"])

    def test_unsupported_hook_events_do_not_consume_messages(self):
        self.run_tool("send", "codex-main-0001", "keep until supported hook")
        for event in ("Stop", "PreToolUse"):
            self.assertEqual(self.run_tool("hook", stdin=json.dumps({
                "session_id": "codex-main-0001", "cwd": str(self.repo),
                "hook_event_name": event})), "")
        messages = json.loads(self.run_tool("--json", "inbox", "--session", "codex-main-0001"))
        self.assertEqual([m["text"] for m in messages], ["keep until supported hook"])

    def test_a_message_reaches_each_session_in_a_path_once_through_its_hook(self):
        sent = json.loads(self.run_tool("--json", "send", str(self.worktree), "Use your own worktree",
                                        "--session", "codex-main-0001"))
        self.assertEqual(sorted(sent["to"]), ["claude-0006", "codex-sub-0002"])
        payload = json.dumps({"session_id": "codex-sub-0002", "cwd": str(self.worktree),
                              "hook_event_name": "UserPromptSubmit"})
        first = json.loads(self.run_tool("hook", stdin=payload))["hookSpecificOutput"]
        self.assertEqual(first["hookEventName"], "UserPromptSubmit")
        self.assertIn("Use your own worktree", first["additionalContext"])
        self.assertIn("claude claude-0006", first["additionalContext"])
        self.assertNotIn("codex-main-0001 running", first["additionalContext"])
        self.assertEqual(self.run_tool("hook", stdin=payload), "")
        unread = json.loads(self.run_tool("--json", "inbox", "--session", "claude-0006"))
        self.assertEqual([m["text"] for m in unread], ["Use your own worktree"])
        self.assertEqual(json.loads(self.run_tool("--json", "inbox", "--session", "claude-0006")), [])

    def test_a_path_message_waits_for_a_session_that_starts_there_later(self):
        empty = self.root / "empty"
        empty.mkdir()
        sent = json.loads(self.run_tool("--json", "send", str(empty), "Set up your end",
                                        "--session", "claude-0006"))
        self.assertEqual(sent["to"], [])

        def hook(session, cwd):
            out = self.run_tool("hook", stdin=json.dumps(
                {"session_id": session, "cwd": str(cwd), "hook_event_name": "PostToolUse"}))
            return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

        self.assertIn("Set up your end", hook("new-partner-0007", empty / "src"))
        self.assertEqual(hook("new-partner-0007", empty / "src"), "")
        self.assertEqual(hook("elsewhere-0008", self.repo), "")
        self.assertEqual(hook("claude-0006", empty), "")

    def test_a_hook_without_a_session_or_with_bad_input_stays_silent(self):
        self.assertEqual(self.run_tool("hook", stdin="{}"), "")
        self.assertEqual(self.run_tool("hook", stdin="not json"), "")


class ClaudeHostTests(Machine):
    def test_a_subagent_hook_leaves_messages_for_the_main_thread(self):
        self.run_tool("send", "claude-0006", "for the main thread", "--session", "codex-main-0001")
        payload = {"session_id": "claude-0006", "cwd": str(self.worktree),
                   "hook_event_name": "PostToolUse"}
        subagent = json.dumps({**payload, "agent_id": "a1b2c3", "agent_type": "general-purpose"})
        self.assertEqual(self.run_tool("hook", "--host", "claude", stdin=subagent), "")
        main = json.loads(self.run_tool("hook", "--host", "claude", stdin=json.dumps(payload)))
        self.assertIn("for the main thread", main["hookSpecificOutput"]["additionalContext"])

    def test_claude_started_from_a_codex_shell_reads_its_own_inbox(self):
        self.run_tool("send", "claude-0006", "for claude", "--session", "codex-main-0001")
        self.run_tool("send", "codex-main-0001", "for codex", "--session", "claude-0006")
        nested = {**self.env, "CODEX_THREAD_ID": "codex-main-0001",
                  "CLAUDE_CODE_SESSION_ID": "claude-0006", "CLAUDE_PID": str(os.getpid())}
        received = json.loads(self.run_tool("--json", "inbox", env=nested))
        self.assertEqual([m["text"] for m in received], ["for claude"])
        codex = json.loads(self.run_tool("--json", "inbox", "--keep", "--session",
                                         "codex-main-0001"))
        self.assertEqual([m["text"] for m in codex], ["for codex"])

    def test_claude_invitation_skips_sessions_that_were_already_running(self):
        pair = json.loads(self.run_tool("partner", "claude", str(self.worktree), "New sessions only",
                                       "--session", "codex-main-0001"))
        running = {"session_id": "claude-0006", "cwd": str(self.worktree)}
        for event in ({"hook_event_name": "PostToolUse"},
                      {"hook_event_name": "SessionStart", "source": "resume"}):
            with self.subTest(event=event):
                self.assertNotIn("New sessions only", self.run_tool(
                    "hook", "--host", "claude", stdin=json.dumps({**running, **event})))
        self.assertEqual(json.loads(self.run_tool("--json", "inbox", "--session", "claude-0006")),
                         [])
        started = json.dumps({"session_id": "new-claude-0009", "cwd": str(self.worktree),
                              "hook_event_name": "SessionStart", "source": "startup"})
        context = json.loads(self.run_tool("hook", "--host", "claude", stdin=started))[
            "hookSpecificOutput"]["additionalContext"]
        self.assertIn("New sessions only", context)
        self.assertIn(pair["id"], context)


class ClaimTests(Machine):
    def path_message(self, text):
        sent = json.loads(self.run_tool("--json", "send", str(self.worktree), text,
                                        "--session", "codex-main-0001"))
        return sent["message"]["id"]

    def hook(self, session, event="PostToolUse", host="codex"):
        out = self.run_tool("hook", "--host", host, stdin=json.dumps(
            {"session_id": session, "cwd": str(self.worktree), "hook_event_name": event}))
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def claim(self, message_id, session, *text):
        return subprocess.run([sys.executable, str(TOOL), "--json", "claim", message_id, *text,
                               "--session", session], env=self.env, text=True,
                              capture_output=True, timeout=30, check=False)

    def test_claiming_a_path_message_removes_it_and_tells_the_sender_ready(self):
        message = self.path_message("Take the parser task")
        self.assertIn(message, self.hook("claude-0006"))
        claimed = self.claim(message, "claude-0006")
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        self.assertEqual(self.hook("later-session-0010"), "")
        replies = json.loads(self.run_tool("--json", "inbox", "--session", "codex-main-0001"))
        self.assertEqual([(m["text"], m["re"], m["from_id"]) for m in replies],
                         [("ready", message, "claude-0006")])

    def test_only_one_session_can_claim_a_message(self):
        message = self.path_message("One owner only")
        self.hook("claude-0006")
        self.hook("codex-sub-0002")
        first = self.claim(message, "codex-sub-0002", "ready, taking it")
        second = self.claim(message, "claude-0006")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already claimed by codex-sub-0002", second.stderr)
        replies = json.loads(self.run_tool("--json", "inbox", "--session", "codex-main-0001"))
        self.assertEqual([m["text"] for m in replies], ["ready, taking it"])

    def test_status_shows_who_received_and_who_claimed_a_message(self):
        message = self.path_message("Who has it")
        self.assertEqual(json.loads(self.run_tool("--json", "status", message))["state"],
                         "pending")
        self.hook("claude-0006")
        self.hook("codex-sub-0002")
        received = json.loads(self.run_tool("--json", "status", message))
        self.assertEqual((received["state"], received["received_by"]),
                         ("received", ["claude-0006", "codex-sub-0002"]))
        self.claim(message, "claude-0006")
        claimed = json.loads(self.run_tool("--json", "status", message))
        self.assertEqual((claimed["state"], claimed["claimed_by"]), ("claimed", "claude-0006"))

    def test_partner_connect_claims_the_invitation_and_reports_ready(self):
        pair = json.loads(self.run_tool("partner", "claude", str(self.worktree), "Partner brief",
                                       "--session", "codex-main-0001"))
        self.assertIn("Partner brief", self.hook("claude-0006", "SessionStart", "claude"))
        recipient = {**self.env, "CLAUDE_CODE_SESSION_ID": "claude-0006"}
        self.run_tool("partner", "connect", pair["id"], env=recipient)
        self.assertNotIn("Partner brief", self.hook("new-claude-0011", "SessionStart", "claude"))
        replies = json.loads(self.run_tool("--json", "inbox", "--session", "codex-main-0001"))
        self.assertEqual([(m["from_id"], m["text"].split(":")[0]) for m in replies],
                         [("claude-0006", "ready")])


class HookSetupTests(Machine):
    def test_configuration_preserves_settings_and_runs_both_hook_commands(self):
        settings = self.claude / "settings.json"
        original = {"permissions": {"allow": []}, "hooks": {"PostToolUse": [
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "true"}]}]}}
        settings.write_text(json.dumps(original))
        for host, path, session in (("codex", self.codex / "hooks.json", "codex-main-0001"),
                                    ("claude", settings, "claude-0006")):
            with self.subTest(host=host):
                preview = json.loads(self.run_tool("configure", host, "--dry-run"))
                self.assertIn("SessionStart", preview["hooks"])
                if host == "codex":
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(json.loads(path.read_text()), original)
                self.run_tool("configure", host)
                first = path.read_bytes()
                self.run_tool("configure", host)
                self.assertEqual(path.read_bytes(), first)
                config = json.loads(first)
                command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
                self.run_tool("send", session, "configured delivery")
                payload = json.dumps({"session_id": session, "cwd": str(self.worktree),
                                      "hook_event_name": "UserPromptSubmit"})
                delivered = subprocess.run(command, shell=True, input=payload, env=self.env,
                                           text=True, capture_output=True, timeout=30, check=False)
                self.assertEqual(delivered.returncode, 0, delivered.stderr)
                self.assertIn("configured delivery", json.loads(delivered.stdout)[
                    "hookSpecificOutput"]["additionalContext"])
                self.run_tool("configure", host, "--remove")
                remaining = json.loads(path.read_text())
                self.assertEqual(remaining, original if host == "claude" else {"hooks": {}})

    def test_invalid_configuration_is_not_overwritten(self):
        path = self.codex / "hooks.json"
        for content in ("bad json", '[]', '{"hooks": {"PostToolUse": null}}'):
            with self.subTest(content=content):
                path.write_text(content)
                result = subprocess.run([sys.executable, str(TOOL), "configure", "codex"],
                                        env=self.env, capture_output=True, text=True, timeout=30,
                                        check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
