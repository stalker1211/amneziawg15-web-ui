"""Tests for the versioning rules in version.sh and publish_dockerhub.sh.

Each test builds a throwaway git repository holding copies of the two scripts and
runs `publish_dockerhub.sh` without `--publish` (a dry run), which prints the version, label and Docker
tags without touching Docker. The rules pinned here: every publish updates :latest,
except a rebuild of an older release (so :latest never goes backwards); an image also
gets a version tag only when HEAD is exactly on a clean release tag vX.Y[.Z]; and
--publish runs only on master or on a clean release commit. The --scan-remote tests
stub grype (and a docker that answers only the registry's index read): the flag scans
the published :latest, or a tag given to it, on every platform its index lists, on any
branch, builds nothing, and exits 0 clean, 2 on a fixable finding on any platform,
1 on a grype error with no finding.
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

    def stub_scan_remote(
        self, grype_exit, flags=(), index: str | None = "linux/amd64 linux/arm64 unknown/unknown", env=None, missing=None
    ):
        # A stub grype that records each call's arguments and exits with grype_exit
        # (or, given a dict, that platform's code), plus a docker stub that answers only
        # `buildx imagetools inspect` with `index` (None: the registry read fails) and
        # reports any other use: --scan-remote must never build or touch the counter.
        # `missing` leaves one of the two out, on a bare PATH like cron's.
        stub = Path(tempfile.mkdtemp(prefix="awg-stub-"))
        self.addCleanup(shutil.rmtree, stub, ignore_errors=True)
        codes = grype_exit if isinstance(grype_exit, dict) else {"*": grype_exit}
        cases = "".join(f'*"{platform}"*) exit {code} ;;\n' for platform, code in codes.items() if platform != "*")
        (stub / "grype").write_text(
            f'#!/bin/sh\necho "$@" >> "{stub / "grype.log"}"\ncase "$*" in\n{cases}esac\nexit {codes.get("*", 0)}\n',
            encoding="utf-8",
        )
        inspect = f'echo "{index}"; exit 0' if index is not None else 'echo "ERROR: not found" >&2; exit 1'
        (stub / "docker").write_text(
            f'#!/bin/sh\nif [ "$1 $2 $3" = "buildx imagetools inspect" ]; then {inspect}; fi\n'
            'echo "stub docker ran" >&2\nexit 99\n',
            encoding="utf-8",
        )
        for name in ("grype", "docker"):
            (stub / name).chmod(0o755)
        if missing:
            (stub / missing).unlink()
        path = f"{stub}{os.pathsep}" + ("/usr/bin:/bin" if missing else self.env["PATH"])
        extra = env or {}
        env = {key: value for key, value in self.env.items() if key != "TAG"}  # the caller's shell may set TAG
        env.update(PATH=path, **extra)
        result = subprocess.run(
            ["bash", "publish_dockerhub.sh", "--scan-remote", *flags],
            cwd=self.dir, env=env, capture_output=True, text=True, check=False,  # exit code is asserted
        )  # fmt: skip
        log = stub / "grype.log"
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result.returncode, result.stdout + result.stderr, calls

    def test_scan_remote_reads_the_published_latest_on_any_branch(self):
        self.git("switch", "-q", "-c", "feature")  # no release tag, not master: still scans
        self.commit("work in progress")
        code, output, calls = self.stub_scan_remote(0)
        self.assertEqual(code, 0, output)
        self.assertIn("Scan clean", output)
        self.assertNotIn("Version:", output)  # scan-remote does not derive a version
        self.assertNotIn("stub docker ran", output)  # the registry read needs no daemon
        self.assertFalse((self.dir / ".cache" / "build_counter").exists())
        self.assertEqual(len(calls), 2)  # one per platform
        for call, platform in zip(calls, ("linux/amd64", "linux/arm64")):
            argv = call.split()
            self.assertIn("registry:stalker1211/amneziawg15-web-ui:latest", argv)
            self.assertIn(platform, argv)  # grype selects the platform from the index
            self.assertIn("--fail-on high", call)  # the gate: these three decide what fails
            self.assertIn("--ignore-states not-fixed,wont-fix,unknown", call)
            self.assertIn("--show-suppressed", call)
        _, _, calls = self.stub_scan_remote(0, env={"IMAGE_REPO": "someone/else"})
        self.assertIn("registry:someone/else:latest", calls[0].split())

    def test_scan_remote_names_a_missing_tool_and_the_path(self):
        for tool in ("grype", "docker"):
            code, output, calls = self.stub_scan_remote(0, missing=tool)
            self.assertEqual(code, 1, output)
            self.assertIn(f"{tool}", output)
            self.assertIn("/usr/bin:/bin", output)  # the PATH it searched
            self.assertIn("put /opt/homebrew/bin and /usr/local/bin on PATH", output)
            self.assertEqual(calls, [])

    def test_scan_remote_scans_the_published_platforms_not_platforms(self):
        _, output, calls = self.stub_scan_remote(0, env={"PLATFORMS": "linux/arm64"})
        self.assertEqual(len(calls), 2, output)  # a build's PLATFORMS does not halve the watch
        _, output, calls = self.stub_scan_remote(0, index="linux/amd64 linux/arm64 linux/arm/v7 unknown/unknown")
        self.assertIn("Published platforms: linux/amd64 linux/arm64 linux/arm/v7", output)
        self.assertEqual([call.split()[2] for call in calls], ["linux/amd64", "linux/arm64", "linux/arm/v7"])

    def test_scan_remote_is_inconclusive_without_the_index(self):
        for index in (None, "unknown/unknown", ""):  # unreadable, or no platform in it
            code, output, calls = self.stub_scan_remote(0, index=index)
            self.assertEqual(code, 1, output)
            self.assertIn("INCONCLUSIVE", output)
            self.assertEqual(calls, [])  # nothing is scanned, so nothing reads as clean

    def test_scan_remote_exit_codes_are_grypes(self):
        code, output, calls = self.stub_scan_remote(2)  # a fixable HIGH/CRITICAL sits on :latest
        self.assertEqual(code, 2, output)
        self.assertIn("Scan FAILED on linux/amd64 linux/arm64", output)
        self.assertIn("republish from master", output)
        self.assertEqual(len(calls), 2)  # both platforms are scanned even when the first fails

        code, output, _ = self.stub_scan_remote(1)  # a grype error is never read as clean
        self.assertEqual(code, 1, output)
        self.assertIn("INCONCLUSIVE", output)

        code, output, _ = self.stub_scan_remote(127)  # any other grype failure either
        self.assertEqual(code, 1, output)
        self.assertIn("INCONCLUSIVE", output)

    def test_scan_remote_a_finding_outweighs_another_platforms_error(self):
        code, output, calls = self.stub_scan_remote({"linux/amd64": 2, "linux/arm64": 1})
        self.assertEqual(code, 2, output)  # the finding is the actionable part
        self.assertIn("Scan FAILED on linux/amd64", output)
        self.assertIn("grype errored on linux/arm64", output)  # the error is still said
        self.assertNotIn("INCONCLUSIVE", output)
        self.assertEqual(len(calls), 2)

        code, output, _ = self.stub_scan_remote({"linux/amd64": 0, "linux/arm64": 1})
        self.assertEqual(code, 1, output)  # a clean platform does not make the scan clean
        self.assertIn("INCONCLUSIVE", output)

    def test_scan_remote_tells_how_to_replace_a_stale_version_tag(self):
        code, output, _ = self.stub_scan_remote(2, flags=("v2.8.1",))
        self.assertEqual(code, 2, output)
        self.assertIn("git checkout v2.8.1 && ./publish_dockerhub.sh --publish", output)
        self.assertNotIn("republish from master", output)  # that would only move :latest

    def test_scan_remote_says_when_the_tag_comes_from_the_environment(self):
        code, output, calls = self.stub_scan_remote(0, env={"TAG": "2.7"})
        self.assertEqual(code, 0, output)
        self.assertIn("registry:stalker1211/amneziawg15-web-ui:2.7", calls[0].split())
        self.assertIn("TAG=2.7 in the environment", output)
        self.assertNotIn("in the environment", self.stub_scan_remote(0, flags=("2.7",))[1])

    def test_scan_remote_can_pick_a_published_version_tag(self):
        for asked in ("2.8.2", "v2.8.2"):  # the same forms the version argument takes
            code, output, calls = self.stub_scan_remote(0, flags=(asked,))
            self.assertEqual(code, 0, output)
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertIn("registry:stalker1211/amneziawg15-web-ui:2.8.2", call.split())
        self.assertIn(":latest", self.stub_scan_remote(0, flags=())[2][0])  # without one it is latest

    def test_scan_remote_is_not_combined_with_a_build(self):
        for flag in ("--scan", "--publish"):
            code, output, calls = self.stub_scan_remote(0, flags=(flag,))
            self.assertEqual(code, 1, output)
            self.assertIn("not combined", output)
            self.assertEqual(calls, [])  # it refuses before scanning anything


class ProvenanceTests(unittest.TestCase):
    def test_the_build_keeps_git_out_of_the_provenance(self):
        # Without it every pushed image names the private NAS remote and commit
        # (verified on Docker Hub, 2.3 to 2.6).
        script = (REPO / "publish_dockerhub.sh").read_text(encoding="utf-8")
        export = script.find("\nexport BUILDX_GIT_INFO=0\n")
        self.assertGreater(export, 0, "publish_dockerhub.sh must export BUILDX_GIT_INFO=0")
        self.assertLess(export, script.find("docker buildx build"), "the export must come before the build")


if __name__ == "__main__":
    unittest.main()
