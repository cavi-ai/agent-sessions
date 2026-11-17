"""Keep the native plugin skill and package metadata aligned."""

import json
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
