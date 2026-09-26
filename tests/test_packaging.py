# tests/test_packaging.py — skills package layout (spec §2)
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


def frontmatter(path: Path) -> dict:
    """The `key: value` lines between the leading `---` fences of a SKILL.md."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{path}: no frontmatter")
    head = text[4:text.index("\n---", 4)]
    out = {}
    for line in head.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


class TestPackaging(unittest.TestCase):
    def test_no_root_skill_md(self):
        # A root SKILL.md would shadow skills/*/SKILL.md for `npx skills`.
        self.assertFalse((REPO / "SKILL.md").exists())

    def test_every_skill_dir_names_itself(self):
        dirs = sorted(p for p in SKILLS.iterdir() if p.is_dir())
        self.assertIn(SKILLS / "rip-swarm", dirs)
        for d in dirs:
            fm = frontmatter(d / "SKILL.md")
            self.assertEqual(fm["name"], d.name)
            self.assertTrue(fm["description"])

    def test_version(self):
        from rip_swarm import __version__
        self.assertEqual(__version__, "0.3.0")

    def test_scripts_run_from_a_copy_outside_the_repo(self):
        from rip_swarm import __version__
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "installed" / "rip-swarm"
            shutil.copytree(
                SKILLS / "rip-swarm", copy, ignore=shutil.ignore_patterns("__pycache__")
            )
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            run = subprocess.run(
                [sys.executable, str(copy / "scripts" / "version.py")],
                cwd=tmp, env=env, capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), f"rip-swarm {__version__}")


if __name__ == "__main__":
    unittest.main()
