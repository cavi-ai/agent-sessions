import hashlib
import io
import json
import posixpath
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.docs.build import build_documentation
from scripts.docs.lib import (
    DOCUMENTED_VERSION,
    PRODUCT_ID,
    REPO_ROOT,
    SOURCE_REL,
    list_files_recursive,
    navigation_paths,
)
from scripts.docs.release_artifact import create_product_docs_release_artifact
from scripts.docs.verify import verify_documentation

IDENTITY = {
    "version": DOCUMENTED_VERSION,
    "tag": f"v{DOCUMENTED_VERSION}",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "source_date_epoch": 1700000000,
}
PAGES = Path(REPO_ROOT) / SOURCE_REL / "pages"
REPOSITORY = "cavi-ai/agent-sessions"


def digest(root, exclude_manifest=False):
    hash_value = hashlib.sha256()
    for relative in list_files_recursive(root):
        if exclude_manifest and relative == "manifest.json":
            continue
        hash_value.update(relative.encode())
        hash_value.update(b"\0")
        hash_value.update((Path(root) / relative).read_bytes())
        hash_value.update(b"\0")
    return hash_value.hexdigest()




class DocumentationTests(unittest.TestCase):
    def test_build_is_complete_stamped_and_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "first", Path(temp) / "second"
            build_documentation({**IDENTITY, "output_root": first})
            build_documentation({**IDENTITY, "output_root": second})
            self.assertEqual(digest(first), digest(second))
            manifest = json.loads((first / "manifest.json").read_text())
            self.assertEqual(
                manifest,
                {
                    "schemaVersion": 2,
                    "package": PRODUCT_ID,
                    "product": PRODUCT_ID,
                    "version": DOCUMENTED_VERSION,
                    "contentSha256": digest(first, True),
                    "publicBasePath": f"/docs/{PRODUCT_ID}/v{DOCUMENTED_VERSION}",
                    "stableAlias": f"/docs/{PRODUCT_ID}",
                    "release": {"tag": IDENTITY["tag"], "commit": IDENTITY["commit"]},
                    "generatedAt": "2023-11-14T22:13:20.000Z",
                },
            )
            for relative in list_files_recursive(first):
                self.assertNotRegex((first / relative).read_text(), r"\{\{[A-Z][A-Z0-9_]*\}\}")
            verify_documentation({**IDENTITY, "output_root": first})

    def test_build_rejects_mismatched_versions_and_dirty_output(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "release version must be"):
                build_documentation(
                    {**IDENTITY, "version": "0.0.900", "output_root": Path(temp) / "bad"}
                )
            output = Path(temp) / "built"
            build_documentation({**IDENTITY, "output_root": output})
            original = (output / "navigation.json").read_bytes()
            (output / "navigation.json").write_text("stale\n")
            build_documentation({**IDENTITY, "output_root": output})
            self.assertEqual((output / "navigation.json").read_bytes(), original)
            (output / "stray.md").write_text("stray\n")
            with self.assertRaisesRegex(ValueError, "dirty output"):
                build_documentation({**IDENTITY, "output_root": output})

    def test_rebuild_for_new_commit_refreshes_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "built"
            build_documentation({**IDENTITY, "output_root": output})
            build_documentation({**IDENTITY, "commit": "f" * 40, "output_root": output})
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["release"]["commit"], "f" * 40)
            verify_documentation({**IDENTITY, "commit": "f" * 40, "output_root": output})

    def test_navigation_lists_every_page_once(self):
        navigation = json.loads((Path(REPO_ROOT) / SOURCE_REL / "navigation.json").read_text())
        self.assertEqual(
            sorted(navigation_paths(navigation)), sorted(list_files_recursive(PAGES))
        )

    def test_relative_page_links_resolve(self):
        pages = list_files_recursive(PAGES)
        broken = []
        for page in pages:
            text = (PAGES / page).read_text()
            for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", text):
                if re.match(r"^[a-z]+:", target):
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(page), target))
                if resolved not in pages:
                    broken.append(f"{page} -> {target}")
        self.assertEqual(broken, [])

    def test_readme_links_resolve(self):
        readme = (Path(REPO_ROOT) / "README.md").read_text()
        broken = []
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", readme):
            if re.match(r"^[a-z]+:", target):
                continue
            if not (Path(REPO_ROOT) / target).exists():
                broken.append(target)
        self.assertEqual(broken, [])

    def test_release_artifact_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            docs = Path(temp) / "docs"
            build_documentation({**IDENTITY, "output_root": docs})
            results = [
                create_product_docs_release_artifact(
                    {
                        **IDENTITY,
                        "docs_root": docs,
                        "output_directory": Path(temp) / name,
                        "repository": REPOSITORY,
                    }
                )
                for name in ("one", "two")
            ]
            first = results[0]["artifact_path"].read_bytes()
            self.assertEqual(first, results[1]["artifact_path"].read_bytes())
            sha = hashlib.sha256(first).hexdigest()
            asset = f"{PRODUCT_ID}-docs-{IDENTITY['tag']}.tar.gz"
            self.assertEqual(
                results[0]["envelope"],
                {
                    "schemaVersion": 1,
                    "slug": PRODUCT_ID,
                    "kind": "product-docs",
                    "version": DOCUMENTED_VERSION,
                    "tag": IDENTITY["tag"],
                    "repository": REPOSITORY,
                    "commit": IDENTITY["commit"],
                    "artifact": {
                        "url": (
                            f"https://github.com/{REPOSITORY}/releases/download/"
                            f"{IDENTITY['tag']}/{asset}"
                        ),
                        "sha256": sha,
                        "format": "tar.gz",
                    },
                },
            )
            self.assertEqual(results[0]["checksum_path"].read_text(), f"{sha}  {asset}\n")
            with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
                names = archive.getnames()
            self.assertIn("cavi-release.json", names)
            root = f"docs/{PRODUCT_ID}/v{DOCUMENTED_VERSION}/"
            self.assertTrue(
                all(name == "cavi-release.json" or name.startswith(root) for name in names)
            )

    def test_artifact_rejects_docs_built_for_another_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            docs = Path(temp) / "docs"
            build_documentation({**IDENTITY, "output_root": docs})
            with self.assertRaisesRegex(ValueError, "manifest is inconsistent"):
                create_product_docs_release_artifact(
                    {
                        **IDENTITY,
                        "commit": "a" * 40,
                        "docs_root": docs,
                        "output_directory": Path(temp) / "release",
                        "repository": REPOSITORY,
                    }
                )


if __name__ == "__main__":
    unittest.main()
