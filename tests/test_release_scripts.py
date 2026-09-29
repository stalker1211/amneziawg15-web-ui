"""Tests for the versioning rules in version.sh and publish_dockerhub.sh.

Each test builds a throwaway git repository holding copies of the two scripts and
runs `publish_dockerhub.sh` without `--publish` (a dry run), which prints the version, label and Docker
tags without touching Docker. The rules pinned here: every publish updates :latest,
except a rebuild of an older release (so :latest never goes backwards); an image also
gets a version tag only when HEAD is exactly on a clean release tag vX.Y[.Z]; and
--publish runs only on master or on a clean release commit.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ("version.sh", "publish_dockerhub.sh")
IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash (not in the image)")
class PublishRulesTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="awg-release-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # HOME points at the temp dir so the user's git config (signing, hooks) stays out.
        self.env = {**os.environ, **IDENTITY, "HOME": str(self.dir)}
        for name in SCRIPTS:
            shutil.copy2(REPO / name, self.dir / name)
        (self.dir / ".gitignore").write_text(".cache/\n", encoding="utf-8")
        self.git("init", "-q", "-b", "master")
        self.commit("initial")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, env=self.env, check=True, capture_output=True, text=True)

    def commit(self, message):
        (self.dir / "change.txt").write_text(message, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def tag(self, name):
        self.git("tag", "-a", name, "-m", name)

    def publish(self, *args):
        result = subprocess.run(
            ["bash", "publish_dockerhub.sh", *args],
            cwd=self.dir, env=self.env, capture_output=True, text=True, check=False,  # exit code is asserted
        )  # fmt: skip
        fields = dict(re.findall(r"^(Version|Label|Push):\s+(.*)$", result.stdout, re.MULTILINE))
        return result.returncode, fields, result.stdout + result.stderr

    def pushed(self, *args):
        code, fields, output = self.publish(*args)
        self.assertEqual(code, 0, output)
        return sorted(image.split(":", 1)[1] for image in fields["Push"].split())

    def test_clean_release_commit_publishes_its_version_and_latest(self):
        self.tag("v2.2")
        self.assertEqual(self.pushed(), ["2.2", "latest"])
        _, fields, _ = self.publish()
        self.assertEqual(fields["Version"], "v2.2 (release v2.2)")
        self.assertRegex(fields["Label"], r"^v2\.2 build \d{8}\.\d+$")

    def test_commits_after_a_release_go_to_latest_with_their_commit_in_the_label(self):
        self.tag("v2.2")
        self.commit("after the release")
        self.assertEqual(self.pushed(), ["latest"])
        _, fields, _ = self.publish()
        self.assertRegex(fields["Label"], r"^v2\.2-1-g[0-9a-f]{7,} build \d{8}\.\d+$")

    def test_uncommitted_changes_never_get_a_version(self):
        self.tag("v2.2")
        (self.dir / "change.txt").write_text("edited", encoding="utf-8")
        self.assertEqual(self.pushed(), ["latest"])
        self.assertIn("-dirty", self.publish()[1]["Version"])

    def test_untracked_files_count_as_dirty(self):
        # They would be in the Docker build context.
        self.tag("v2.2")
        (self.dir / "new.js").write_text("x", encoding="utf-8")
        self.assertEqual(self.pushed(), ["latest"])

    def test_rebuilding_an_older_release_does_not_move_latest(self):
        self.tag("v2.1")
        self.commit("next")
        self.tag("v2.2")
        self.git("checkout", "-q", "v2.1")
        self.assertEqual(self.pushed(), ["2.1"])

    def test_newest_uses_version_order(self):
        self.tag("v2.9")
        self.commit("next")
        self.tag("v2.10")
        self.assertEqual(self.pushed(), ["2.10", "latest"])

    def test_the_version_argument_must_match_the_release(self):
        self.tag("v2.2")
        self.assertEqual(self.pushed("2.2"), ["2.2", "latest"])
        self.assertEqual(self.pushed("v2.2"), ["2.2", "latest"])
        code, _, output = self.publish("2.3")
        self.assertEqual(code, 1)
        self.assertIn("git tag -a v2.3", output)

    def test_mismatch_hint_matches_the_situation(self):
        self.tag("v2.2")
        (self.dir / "change.txt").write_text("edited", encoding="utf-8")
        code, _, output = self.publish("2.2")
        self.assertEqual(code, 1)
        self.assertIn("uncommitted or untracked changes", output)

        self.commit("after the release")
        code, _, output = self.publish("2.2")
        self.assertEqual(code, 1)
        self.assertIn("v2.2 already exists on another commit", output)
        self.assertIn("run without a version", output)
        # Re-creating an existing tag would fail, so it must not be suggested.
        self.assertNotIn("git tag -a v2.2", output)

    def test_only_v_tags_are_releases(self):
        self.tag("1.5.1")
        self.assertEqual(self.pushed(), ["latest"])

    def test_no_release_tag_at_all(self):
        self.assertEqual(self.pushed(), ["latest"])
        self.assertRegex(self.publish()[1]["Version"], r"^[0-9a-f]{7,}$")

    def real_publish(self, *args, flag="--publish"):
        # A stub docker whose daemon is unreachable: getting that error means the
        # script passed its own checks, and nothing can ever be built. It lives
        # outside the repository, where it would count as an untracked change.
        stub = Path(tempfile.mkdtemp(prefix="awg-stub-"))
        self.addCleanup(shutil.rmtree, stub, ignore_errors=True)
        (stub / "docker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (stub / "docker").chmod(0o755)
        env = {**self.env, "PATH": f"{stub}{os.pathsep}{self.env['PATH']}"}
        result = subprocess.run(
            ["bash", "publish_dockerhub.sh", flag, *args],
            cwd=self.dir, env=env, capture_output=True, text=True, check=False,  # exit code is asserted
        )  # fmt: skip
        return result.returncode, result.stdout + result.stderr

    def test_scan_builds_on_any_branch_and_keeps_the_counter(self):
        self.git("switch", "-q", "-c", "feature")
        self.commit("work in progress")
        code, output = self.real_publish(flag="--scan")
        self.assertEqual(code, 1)
        self.assertIn("docker daemon not reachable", output)  # got past the branch rule
        self.assertNotIn("asked for", output)  # --scan is not read as a version
        self.assertFalse((self.dir / ".cache" / "build_counter").exists())

    def test_publish_refuses_a_branch_other_than_master(self):
        self.git("switch", "-q", "-c", "feature")
        self.commit("work in progress")
        _, _, dry = self.publish()
        self.assertIn("--publish would refuse: HEAD is feature, not master", dry)
        code, output = self.real_publish()
        self.assertEqual(code, 1)
        self.assertIn("HEAD is feature, not master", output)
        self.assertNotIn("docker", output)

    def test_publish_allows_master_and_a_detached_release(self):
        self.tag("v2.1")
        self.commit("next")
        self.assertNotIn("would refuse", self.publish()[2])
        self.assertIn("docker daemon not reachable", self.real_publish()[1])

        self.git("switch", "-q", "--detach", "v2.1")  # rebuilding an older release
        self.assertNotIn("would refuse", self.publish()[2])
        self.assertIn("docker daemon not reachable", self.real_publish()[1])

        self.git("switch", "-q", "--detach", "master")  # detached, but no release
        self.assertIn("HEAD is detached, not master", self.real_publish()[1])

    def test_dry_run_leaves_the_build_counter_alone(self):
        self.tag("v2.2")
        counter = self.dir / ".cache" / "build_counter"
        counter.parent.mkdir()
        counter.write_text(f"{time.strftime('%Y%m%d')} 4\n", encoding="utf-8")
        self.assertTrue(self.publish()[1]["Label"].endswith(".5"))
        self.assertEqual(counter.read_text(encoding="utf-8").split()[1], "4")


if __name__ == "__main__":
    unittest.main()
