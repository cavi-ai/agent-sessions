"""Hardening tests for cross-host exact session ID collisions in partners.py."""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

partners = importlib.import_module("agent_sessions.partners")
paths = importlib.import_module("agent_sessions.paths")
queue = importlib.import_module("agent_sessions.queue")

TOOL_CMD = [sys.executable, "-m", "agent_sessions"]
HOST_ENV_VARS = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_AGENT",
    "ANTIGRAVITY_APP_DATA_DIR",
    "OPENCODE_SESSION_ID",
    "OPENCODE_DB",
)


def _session(host, session_id, cwd):
    return {"host": host, "id": session_id, "cwd": str(cwd)}


class PartnersHardeningTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.data_dir = self.root / "data"
        self.caller_dir = self.root / "caller-worktree"
        self.partner_dir = self.root / "partner-worktree"
        self.other_dir = self.root / "other-worktree"
        self.caller_dir.mkdir()
        self.partner_dir.mkdir()
        self.other_dir.mkdir()
        env_patch = patch.dict(os.environ, {"AGENT_SESSIONS_DATA": str(self.data_dir)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _pair_files(self):
        pairs_dir = self.data_dir / "pairs"
        if not pairs_dir.exists():
            return []
        return sorted(pairs_dir.glob("*.json"))

    def _clean_cli_env(self, codex_home, claude_home):
        env = dict(os.environ)
        for var in HOST_ENV_VARS:
            env.pop(var, None)
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "AGY_HOME": str(self.root / "agy"),
                "AGY_CONFIG_DIR": str(self.root / "agy-config"),
                "XDG_DATA_HOME": str(self.root / "xdg-data"),
                "OPENCODE_CONFIG_DIR": str(self.root / "opencode-config"),
                "AGENT_SESSIONS_DATA": str(self.data_dir),
                "PYTHONPATH": str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        return env

    def test_connect_partner_selects_expected_host_when_wrong_host_appears_first(self):
        initial_found = [_session("codex", "codex-caller-01", self.caller_dir)]
        pair = partners.create_partner(
            "claude",
            str(self.partner_dir),
            "Review parser changes",
            "codex-caller-01",
            initial_found,
        )
        self.assertEqual(pair["status"], "waiting")

        invite_dir = paths.queue_dir(paths.path_key(str(self.partner_dir)))
        self.assertEqual(len(queue.listing(invite_dir)), 1)

        # Wrong host (codex) with the exact same partner session ID appears first in found.
        found = [
            _session("codex", "shared-partner-01", self.other_dir),
            _session("claude", "shared-partner-01", self.partner_dir),
            _session("codex", "codex-caller-01", self.caller_dir),
        ]
        connected = partners.connect_partner(
            pair["id"], "shared-partner-01", "codex-caller-01", found
        )
        self.assertEqual(connected["status"], "connected")
        self.assertEqual(connected["partner"]["host"], "claude")
        self.assertEqual(connected["partner"]["id"], "shared-partner-01")
        self.assertEqual(connected["partner"]["cwd"], str(self.partner_dir))

        persisted = partners.read_pair(pair["id"])
        self.assertEqual(persisted["status"], "connected")
        self.assertEqual(persisted["partner"]["host"], "claude")
        self.assertEqual(persisted["partner"]["id"], "shared-partner-01")

        # Invitation was claimed from the directory queue and caller notified.
        self.assertEqual(queue.listing(invite_dir), [])
        caller_queue = paths.queue_dir(paths.session_key("codex-caller-01"))
        notices = [
            queue.read_message(caller_queue / name) for name in queue.listing(caller_queue)
        ]
        self.assertTrue(
            any(
                msg and f"connected to pair {pair['id']}" in msg["text"]
                for msg in notices
            )
        )

    def test_connect_partner_rejects_ambiguous_within_expected_host_without_mutating_pair(self):
        initial_found = [_session("claude", "claude-caller-01", self.caller_dir)]
        pair = partners.create_partner(
            "codex",
            str(self.partner_dir),
            "Fix parser",
            "claude-caller-01",
            initial_found,
            project_id="proj-001",
        )
        self.assertEqual(pair["status"], "needs_creation")

        found = [
            _session("codex", "dup-codex-01", self.partner_dir),
            _session("codex", "dup-codex-01", self.partner_dir),
            _session("claude", "claude-caller-01", self.caller_dir),
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            partners.connect_partner(pair["id"], "dup-codex-01", "claude-caller-01", found)

        persisted = partners.read_pair(pair["id"])
        self.assertEqual(persisted["status"], "needs_creation")
        self.assertIsNone(persisted["partner"]["id"])

    def test_connect_partner_preserves_missing_wrong_host_and_wrong_directory_errors(self):
        initial_found = [_session("claude", "claude-caller-01", self.caller_dir)]
        pair = partners.create_partner(
            "codex",
            str(self.partner_dir),
            "Audit module",
            "claude-caller-01",
            initial_found,
            project_id="proj-001",
        )

        cases = [
            (
                "missing",
                [_session("claude", "claude-caller-01", self.caller_dir)],
                "partner must be a discovered session of the requested host",
            ),
            (
                "wrong_host_only",
                [
                    _session("claude", "peer-01", self.partner_dir),
                    _session("claude", "claude-caller-01", self.caller_dir),
                ],
                "partner must be a discovered session of the requested host",
            ),
            (
                "wrong_directory",
                [
                    _session("codex", "peer-01", self.other_dir),
                    _session("claude", "claude-caller-01", self.caller_dir),
                ],
                "partner session is working in a different directory",
            ),
        ]
        for label, found, expected_msg in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, expected_msg):
                    partners.connect_partner(pair["id"], "peer-01", "claude-caller-01", found)
                persisted = partners.read_pair(pair["id"])
                self.assertEqual(persisted["status"], "needs_creation")
                self.assertIsNone(persisted["partner"]["id"])

    def test_connect_partner_cli_checks_self_connect_host_on_cross_host_id_collision(self):
        codex_home = self.root / "codex-home"
        claude_home = self.root / "claude-home"
        codex_home.mkdir()
        claude_project = claude_home / "projects" / "-partner"
        claude_project.mkdir(parents=True)

        now = time.time()
        shared_id = "shared-cli-0001"
        caller_id = "codex-caller-0001"

        # Real Codex SQLite DB with a more-recent colliding session so Codex sorts first.
        rollout = codex_home / "rollout.jsonl"
        rollout.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
            conn.execute(
                "create table threads (id text primary key, rollout_path text, created_at int,"
                " updated_at int, updated_at_ms int, source text, cwd text, title text,"
                " name text, archived int, agent_nickname text)"
            )
            for tid, cwd, ts in (
                (shared_id, str(self.other_dir), now),
                (caller_id, str(self.caller_dir), now - 5),
            ):
                conn.execute(
                    "insert into threads values (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tid,
                        str(rollout),
                        int(ts - 60),
                        int(ts),
                        int(ts * 1000),
                        "vscode",
                        cwd,
                        "work",
                        None,
                        0,
                        None,
                    ),
                )
            conn.commit()

        # Real Claude session JSONL with the exact same ID, slightly older mtime.
        claude_log = claude_project / f"{shared_id}.jsonl"
        claude_log.write_text(
            json.dumps(
                {
                    "type": "user",
                    "cwd": str(self.partner_dir),
                    "message": {"content": "hello"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(claude_log, (now - 30, now - 30))

        env = self._clean_cli_env(codex_home, claude_home)
        created = subprocess.run(
            [
                *TOOL_CMD,
                "--json",
                "partner",
                "claude",
                str(self.partner_dir),
                "Review collision fix",
                "--session",
                caller_id,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        pair = json.loads(created.stdout)
        self.assertEqual(pair["status"], "waiting")
        invite_dir = paths.queue_dir(paths.path_key(str(self.partner_dir)))
        self.assertEqual(len(queue.listing(invite_dir)), 1)

        command = [*TOOL_CMD, "--json", "partner", "connect", pair["id"]]
        for label, options, caller_env, cwd, error in (
            ("wrong host", [], {"CODEX_THREAD_ID": shared_id}, self.other_dir, "host"),
            ("unqualified ID", ["--session", shared_id], {}, self.root, "ambiguous"),
        ):
            with self.subTest(caller=label):
                denied = subprocess.run(
                    [*command, *options],
                    env={**env, **caller_env},
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                self.assertNotEqual(denied.returncode, 0, denied.stdout)
                self.assertIn(error, denied.stderr)
                self.assertEqual(partners.read_pair(pair["id"]), pair)
                self.assertEqual(len(queue.listing(invite_dir)), 1)

        connected_proc = subprocess.run(
            command,
            env={**env, "CLAUDE_CODE_SESSION_ID": shared_id},
            cwd=self.partner_dir,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(connected_proc.returncode, 0, connected_proc.stderr)
        connected = json.loads(connected_proc.stdout)
        self.assertEqual(connected["status"], "connected")
        self.assertEqual(connected["partner"]["host"], "claude")
        self.assertEqual(connected["partner"]["id"], shared_id)
        self.assertEqual(queue.listing(invite_dir), [])

    def test_create_partner_rejects_ambiguous_caller_without_writing_pair_file(self):
        found = [
            _session("codex", "shared-caller-01", self.caller_dir),
            _session("claude", "shared-caller-01", self.other_dir),
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous.*claude.*codex"):
            partners.create_partner(
                "claude",
                str(self.partner_dir),
                "Inspect build",
                "shared-caller-01",
                found,
            )

        self.assertEqual(self._pair_files(), [])
        invite_dir = paths.queue_dir(paths.path_key(str(self.partner_dir)))
        self.assertEqual(queue.listing(invite_dir), [])

    def test_create_partner_preserves_missing_caller_error_without_writing_pair_file(self):
        found = [_session("codex", "other-caller-01", self.caller_dir)]
        with self.assertRaisesRegex(
            ValueError, "caller is not a discovered session; check --session and --minutes"
        ):
            partners.create_partner(
                "claude",
                str(self.partner_dir),
                "Inspect build",
                "missing-caller-01",
                found,
            )
        self.assertEqual(self._pair_files(), [])

    def test_attach_partner_rejects_cross_host_collisions_and_self_attach_without_writes(self):
        cases = [
            (
                "ambiguous_caller",
                "peer-01",
                "shared-caller-01",
                [
                    _session("codex", "shared-caller-01", self.caller_dir),
                    _session("claude", "shared-caller-01", self.other_dir),
                    _session("claude", "peer-01", self.partner_dir),
                ],
                "ambiguous.*claude.*codex",
            ),
            (
                "ambiguous_partner",
                "shared-peer-01",
                "caller-01",
                [
                    _session("codex", "caller-01", self.caller_dir),
                    _session("codex", "shared-peer-01", self.other_dir),
                    _session("claude", "shared-peer-01", self.partner_dir),
                ],
                "ambiguous.*claude.*codex",
            ),
            (
                "self_attach_preserves_error_even_when_id_collides",
                "same-id-01",
                "same-id-01",
                [
                    _session("codex", "same-id-01", self.caller_dir),
                    _session("claude", "same-id-01", self.partner_dir),
                ],
                "attach requires two different discovered sessions",
            ),
            (
                "missing_peer",
                "missing-peer-01",
                "caller-01",
                [_session("codex", "caller-01", self.caller_dir)],
                "attach requires two different discovered sessions",
            ),
        ]
        for label, session_id, caller_id, found, pattern in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, pattern):
                    partners.attach_partner(session_id, caller_id, found)
                self.assertEqual(self._pair_files(), [])

    def test_attach_partner_host_qualified_idempotency_and_distinct_hosts(self):
        found_claude_peer = [
            _session("codex", "caller-01", self.caller_dir),
            _session("claude", "peer-01", self.partner_dir),
        ]
        first = partners.attach_partner("peer-01", "caller-01", found_claude_peer)
        self.assertEqual(first["status"], "connected")
        self.assertEqual(first["caller"]["host"], "codex")
        self.assertEqual(first["partner"]["host"], "claude")
        self.assertEqual(len(self._pair_files()), 1)

        # Unambiguous repeat with same caller/partner hosts returns the existing pair.
        second = partners.attach_partner("peer-01", "caller-01", found_claude_peer)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self._pair_files()), 1)

        # Same exact session IDs on a different partner host must not collide with the claude pair.
        found_opencode_peer = [
            _session("codex", "caller-01", self.caller_dir),
            _session("opencode", "peer-01", self.other_dir),
        ]
        third = partners.attach_partner("peer-01", "caller-01", found_opencode_peer)
        self.assertNotEqual(third["id"], first["id"])
        self.assertEqual(third["caller"]["host"], "codex")
        self.assertEqual(third["partner"]["host"], "opencode")
        self.assertEqual(third["partner"]["cwd"], str(self.other_dir))
        self.assertEqual(len(self._pair_files()), 2)

        # Same exact session IDs on a different caller host must not collide either.
        found_opencode_caller = [
            _session("opencode", "caller-01", self.other_dir),
            _session("claude", "peer-01", self.partner_dir),
        ]
        fourth = partners.attach_partner("peer-01", "caller-01", found_opencode_caller)
        self.assertNotIn(fourth["id"], {first["id"], third["id"]})
        self.assertEqual(fourth["caller"]["host"], "opencode")
        self.assertEqual(fourth["partner"]["host"], "claude")
        self.assertEqual(len(self._pair_files()), 3)


if __name__ == "__main__":
    unittest.main()
