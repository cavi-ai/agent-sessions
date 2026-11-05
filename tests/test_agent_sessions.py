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


class MessageTests(Machine):
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


if __name__ == "__main__":
    unittest.main()
