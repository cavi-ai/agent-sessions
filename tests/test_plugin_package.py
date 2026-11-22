"""Keep the native plugin skill and package metadata aligned."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_sessions
import managed_runs

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


class PluginPackageTests(unittest.TestCase):
    def test_skill_copy_matches_canonical_source(self):
        canonical = ROOT / "SKILL.md"
        installed = ROOT / "skills/agent-sessions/SKILL.md"
        self.assertFalse(installed.is_symlink())
        self.assertEqual(installed.read_bytes(), canonical.read_bytes())
        text = installed.read_text()
        self.assertIn("agent-sessions", text)
        self.assertNotIn("<this folder>", text)
        self.assertNotIn("](docs/", text)

    def test_host_manifests_reference_same_skill_and_version(self):
        with (ROOT / "pyproject.toml").open("rb") as source:
            version = tomllib.load(source)["project"]["version"]
        for host in ("claude", "codex"):
            manifest = json.loads((ROOT / f".{host}-plugin/plugin.json").read_text())
            self.assertEqual(manifest["name"], "agent-sessions")
            self.assertEqual(manifest["version"], version)
            self.assertNotIn("hooks", manifest)
        self.assertEqual(manifest["skills"], "./skills/")

    def test_runtime_cli_and_native_client_version_match_package(self):
        with (ROOT / 'pyproject.toml').open('rb') as source:
            version = tomllib.load(source)['project']['version']
        self.assertEqual(agent_sessions.VERSION, version)
        env = {key: os.environ[key] for key in ('HOME', 'PATH', 'LANG') if key in os.environ}
        result = subprocess.run([sys.executable, str(ROOT / 'agent_sessions.py'), '--version'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'agent-sessions '+version)
        with patch.object(managed_runs, 'Rpc') as rpc, patch.object(managed_runs, 'capabilities', return_value={}):
            client = rpc.return_value
            client.request.return_value = {'config': {}}
            managed_runs.configured_client({'cwd': str(ROOT)}, [], {}, 1)
            initialize = client.request.call_args_list[0]
            self.assertEqual(initialize.args[1]['clientInfo']['version'], version)


if __name__ == "__main__":
    unittest.main()
