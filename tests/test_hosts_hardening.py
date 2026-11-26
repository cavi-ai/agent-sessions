"""Hardening tests for host session discovery and self_id validation."""

import importlib
import json
import os
import sqlite3
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

hosts = importlib.import_module("agent_sessions.hosts")
paths = importlib.import_module("agent_sessions.paths")

HOST_ENV_VARS = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_AGENT",
    "OPENCODE_SESSION_ID",
)


class HostsHardeningTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.now = time.time()

    def _clean_env(self, **overrides):
        env = dict(os.environ)
        for var in HOST_ENV_VARS:
            env.pop(var, None)
        env.update(overrides)
        return env

    def test_codex_sessions_skips_invalid_and_non_string_thread_ids(self):
        codex_home = self.root / "codex"
        codex_home.mkdir()
        db_path = codex_home / "state_1.sqlite"
        rollout = codex_home / "valid-rollout.jsonl"
        rollout.write_text(
            json.dumps(
                {"type": "event_msg", "payload": {"type": "task_started"}}
            ) + "\n",
            encoding="utf-8",
        )

        valid_standard = "codex-valid-0001"
        valid_max_len = "a" * 200
        invalid_ids = [
            "../escape",
            "has space",
            "",
            "a" * 201,
            None,
            12345,
            "bad.dot",
            "-leading-dash",
        ]

        updated_sec = int(self.now)
        updated_ms = int(self.now * 1000)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "create table threads ("
                "id, rollout_path text, updated_at int, updated_at_ms int, "
                "source text, cwd text, title text, name text, archived int, "
                "agent_nickname text)"
            )
            rows = [
                (
                    valid_standard,
                    str(rollout),
                    updated_sec,
                    updated_ms,
                    "vscode",
                    str(self.work),
                    "Valid session",
                    None,
                    0,
                    None,
                ),
                (
                    valid_max_len,
                    str(rollout),
                    updated_sec,
                    updated_ms,
                    "vscode",
                    str(self.work),
                    "Max length session",
                    None,
                    0,
                    None,
                ),
            ]
            for idx, bad_id in enumerate(invalid_ids):
                rows.append(
                    (
                        bad_id,
                        str(rollout),
                        updated_sec,
                        updated_ms,
                        "vscode",
                        str(self.work),
                        f"Invalid {idx}",
                        None,
                        0,
                        None,
                    )
                )
            conn.executemany(
                "insert into threads values (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()

        db_path.chmod(0o444)
        self.addCleanup(lambda: db_path.chmod(0o644) if db_path.exists() else None)

        with patch.dict(os.environ, self._clean_env(CODEX_HOME=str(codex_home)), clear=True):
            with patch.object(
                hosts, "codex_rollout", wraps=hosts.codex_rollout
            ) as rollout_spy:
                found = hosts.codex_sessions(self.now - 3600)

        found_ids = {session["id"] for session in found}
        self.assertEqual(found_ids, {valid_standard, valid_max_len})
        self.assertEqual(rollout_spy.call_count, 2)
        by_id = {session["id"]: session for session in found}
        self.assertEqual(by_id[valid_standard]["status"], "running")
        self.assertEqual(by_id[valid_standard]["cwd"], str(self.work))

    def test_claude_sessions_skips_invalid_stems_without_crashing(self):
        claude_home = self.root / "claude"
        project = claude_home / "projects" / "-work"
        project.mkdir(parents=True)

        def write_session(filename, prompt):
            edited_file = self.work / "app.py"
            records = [
                {"type": "user", "cwd": str(self.work), "message": {"content": "hi"}},
                {"type": "last-prompt", "lastPrompt": prompt},
                {
                    "type": "assistant",
                    "cwd": str(self.work),
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"file_path": str(edited_file)},
                            }
                        ]
                    },
                },
            ]
            (project / filename).write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )

        valid_id = "claude-valid-0001"
        valid_max_id = "y" * 200
        write_session(f"{valid_id}.jsonl", "Valid Claude session")
        write_session(f"{valid_max_id}.jsonl", "Max length Claude session")

        invalid_filenames = [
            "bad.stem.jsonl",
            "with space.jsonl",
            ".hidden.jsonl",
            ("x" * 201) + ".jsonl",
            "-leading-dash.jsonl",
        ]
        for idx, name in enumerate(invalid_filenames):
            write_session(name, f"Invalid {idx}")

        with patch.dict(
            os.environ,
            self._clean_env(CLAUDE_CONFIG_DIR=str(claude_home)),
            clear=True,
        ):
            with patch.object(
                hosts, "tail_records", wraps=hosts.tail_records
            ) as tail_spy:
                found = hosts.claude_sessions(self.now - 3600)

        found_ids = {session["id"] for session in found}
        self.assertEqual(found_ids, {valid_id, valid_max_id})
        self.assertEqual(tail_spy.call_count, 2)
        by_id = {session["id"]: session for session in found}
        self.assertEqual(by_id[valid_id]["title"], "Valid Claude session")
        self.assertEqual(by_id[valid_id]["edited"], [str(self.work / "app.py")])

    def test_self_id_calls_environment_session_id_once_and_validates(self):
        with patch.dict(os.environ, self._clean_env(CODEX_THREAD_ID="codex-env-1234"), clear=True):
            with patch.object(
                paths, "environment_session_id", wraps=paths.environment_session_id
            ) as env_spy:
                resolved_id = hosts.self_id(self.work, [])
        self.assertEqual(resolved_id, "codex-env-1234")
        self.assertEqual(env_spy.call_count, 1)

        for bad_env_id in ("../escape", "has space", "bad.dot", "a" * 201):
            with self.subTest(bad_env_id=bad_env_id):
                with patch.dict(
                    os.environ,
                    self._clean_env(CODEX_THREAD_ID=bad_env_id),
                    clear=True,
                ):
                    with patch.object(
                        paths,
                        "environment_session_id",
                        wraps=paths.environment_session_id,
                    ) as env_spy:
                        with self.assertRaises(ValueError):
                            hosts.self_id(self.work, [])
                self.assertEqual(env_spy.call_count, 1)

        with patch.dict(os.environ, self._clean_env(), clear=True):
            with patch.object(
                paths, "environment_session_id", wraps=paths.environment_session_id
            ) as env_spy:
                fallback_id = hosts.self_id(
                    self.work, [{"id": "only-local-session", "cwd": str(self.work)}]
                )
        self.assertEqual(fallback_id, "only-local-session")
        self.assertEqual(env_spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
