"""Real Node.js TypeScript syntax and execution smoke test for the OpenCode plugin."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _node_ts_command() -> tuple[list[str] | None, str | None]:
    node = shutil.which("node")
    if node is None:
        return None, "node executable not found on PATH"
    probe = subprocess.run(
        [node, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0:
        detail = probe.stderr.strip() or f"exit code {probe.returncode}"
        return None, f"node --version failed: {detail}"
    version_text = probe.stdout.strip()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version_text)
    if match is None:
        return None, f"unrecognized node --version output: {version_text!r}"
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < (22, 6):
        return (
            None,
            f"Node.js >= 22.6 required for TypeScript execution; found {version_text}",
        )
    flags = ["--experimental-strip-types"] if (major, minor) < (22, 18) else []
    return [node, *flags], None


def _clean_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for var in (
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CLAUDE_PID",
        "ANTIGRAVITY_CONVERSATION_ID",
        "ANTIGRAVITY_AGENT",
        "ANTIGRAVITY_APP_DATA_DIR",
        "OPENCODE_SESSION_ID",
        "OPENCODE_DB",
        "AGENT_SESSIONS_PROFILE_ID",
    ):
        env.pop(var, None)
    pythonpath = str(ROOT / "src")
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env.update(
        {
            "CODEX_HOME": str(root / "codex"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "AGY_HOME": str(root / "agy"),
            "AGY_CONFIG_DIR": str(root / "agy-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "OPENCODE_CONFIG_DIR": str(root / "opencode-config"),
            "AGENT_SESSIONS_DATA": str(root / "agent-sessions-data"),
            "PYTHONPATH": pythonpath,
        }
    )
    return env


def _seed_opencode_db(db_path: Path, session_id: str, cwd: Path, now: float) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now_ms = int(now * 1000)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(
            "create table if not exists session_v2 ("
            "id text primary key, project_id text not null, parent_id text, "
            "slug text not null, directory text not null, title text, "
            "version text not null, time_created integer not null, "
            "time_updated integer not null, time_archived integer, "
            "time_busy integer, time_idle integer)"
        )
        db.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                "project",
                None,
                "smoke",
                str(cwd),
                "OpenCode plugin smoke test",
                "2.3.2",
                now_ms,
                now_ms,
                None,
                None,
                None,
            ),
        )


RUNNER_JS = r"""
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const [pluginPath, sessionID, pythonExe, senderID, promptMsg, afterMsg] = process.argv.slice(1);
const mod = await import(pathToFileURL(pluginPath).href);
const plugin = mod.default;

assert.equal(plugin?.id, "agent-sessions");
assert.equal(typeof plugin?.setup, "function");

const sessionHooks = new Map();
const toolHooks = new Map();
const synthetics = [];

const ctx = {
  session: {
    async hook(name, fn) {
      sessionHooks.set(name, fn);
    },
    async synthetic(payload) {
      synthetics.push(payload);
    },
  },
  tool: {
    async hook(name, fn) {
      toolHooks.set(name, fn);
    },
  },
};

await plugin.setup(ctx);

const promptHook = sessionHooks.get("prompt");
const beforeHook = toolHooks.get("execute.before");
const afterHook = toolHooks.get("execute.after");
assert.equal(typeof promptHook, "function");
assert.equal(typeof beforeHook, "function");
assert.equal(typeof afterHook, "function");

const promptEvent = { sessionID, prompt: { text: "original prompt" } };
await promptHook(promptEvent);
assert.ok(
  promptEvent.prompt.text.startsWith("original prompt\n\n<agent-sessions>\n"),
  `expected <agent-sessions> prefix, got: ${promptEvent.prompt.text}`,
);
assert.ok(promptEvent.prompt.text.endsWith("\n</agent-sessions>"));
assert.ok(
  promptEvent.prompt.text.includes(promptMsg),
  `expected queued prompt message, got: ${promptEvent.prompt.text}`,
);

await beforeHook({ sessionID });

execFileSync(
  pythonExe,
  ["-m", "agent_sessions", "send", sessionID, afterMsg, "--session", senderID],
  { encoding: "utf8", timeout: 15_000 },
);

await afterHook({ sessionID });
assert.equal(synthetics.length, 1);
assert.equal(synthetics[0].sessionID, sessionID);
assert.ok(
  synthetics[0].text.includes(afterMsg),
  `expected synthetic message text, got: ${synthetics[0].text}`,
);

const missingProfileId = "0".repeat(64);
process.env.AGENT_SESSIONS_PROFILE_ID = missingProfileId;
await assert.rejects(
  () => promptHook({ sessionID, prompt: { text: "blocked" } }),
  (err) => {
    assert.ok(err instanceof Error);
    assert.match(err.message, /agent-sessions profile:/);
    assert.match(err.message, new RegExp(missingProfileId));
    return true;
  },
);

console.log(
  JSON.stringify({
    pluginId: plugin.id,
    promptText: promptEvent.prompt.text,
    synthetics,
  }),
);
""".lstrip()


class OpenCodePluginSmokeTests(unittest.TestCase):
    def test_generated_opencode_plugin_loads_and_dispatches_hooks_in_node(self):
        node_cmd, skip_reason = _node_ts_command()
        if skip_reason is not None:
            self.skipTest(skip_reason)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workdir = root / "workspace"
            workdir.mkdir()
            env = _clean_env(root)
            session_id = "ses_smoke0001"
            sender_id = "ses_sender0001"
            prompt_msg = "queued prompt smoke message"
            after_msg = "queued post-tool smoke message"

            _seed_opencode_db(
                root / "xdg-data" / "opencode" / "opencode.db",
                session_id,
                workdir,
                time.time(),
            )

            configured = subprocess.run(
                [sys.executable, "-m", "agent_sessions", "configure", "opencode"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)

            plugin_path = (root / "opencode-config" / "plugins" / "agent-sessions.ts").resolve()
            self.assertTrue(plugin_path.is_file(), f"missing generated plugin: {plugin_path}")
            (plugin_path.parent / "package.json").write_text(
                '{"type": "module"}\n',
                encoding="utf-8",
            )

            queued = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_sessions",
                    "send",
                    session_id,
                    prompt_msg,
                    "--session",
                    sender_id,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(queued.returncode, 0, queued.stderr)

            proc = subprocess.run(
                [
                    *node_cmd,
                    "--input-type=module",
                    "-e",
                    RUNNER_JS,
                    str(plugin_path),
                    session_id,
                    sys.executable,
                    sender_id,
                    prompt_msg,
                    after_msg,
                ],
                env=env,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

            payload = json.loads(proc.stdout)
            self.assertEqual(payload["pluginId"], "agent-sessions")
            self.assertIn(prompt_msg, payload["promptText"])
            self.assertEqual(len(payload["synthetics"]), 1)
            self.assertEqual(payload["synthetics"][0]["sessionID"], session_id)
            self.assertIn(after_msg, payload["synthetics"][0]["text"])


if __name__ == "__main__":
    unittest.main()
