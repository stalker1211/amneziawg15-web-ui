"""Tests for the Python dependency pins (web-ui/requirements.in -> requirements.txt).

requirements.in names what we choose; requirements.txt is `uv pip compile`'s output and
pins every package installed, Flask's own dependencies included. The image and
run_tests.sh both install requirements.txt, so these keep them on the same versions:
a package left unpinned would be resolved separately by each, at different times.
"""

import re
import unittest
from pathlib import Path

WEB_UI = Path(__file__).resolve().parent.parent / "web-ui"
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==[A-Za-z0-9.+!-]+$")


def _requirements(name):
    """The requirement lines of a requirements file: comments and blanks dropped."""
    lines = (line.split("#", 1)[0].strip() for line in (WEB_UI / name).read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line]


def _normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class RequirementsPinTests(unittest.TestCase):
    def test_every_installed_package_is_pinned(self):
        lines = _requirements("requirements.txt")
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, PIN, "requirements.txt: every line `name==version`")

    def test_generated_from_requirements_in(self):
        header = (WEB_UI / "requirements.txt").read_text(encoding="utf-8").splitlines()[:2]
        self.assertTrue(any("uv pip compile" in line and "requirements.in" in line for line in header), header)

    def test_every_chosen_package_is_in_the_lock(self):
        pinned = {_normalized(PIN.match(line).group(1)) for line in _requirements("requirements.txt")}
        for line in _requirements("requirements.in"):
            name = _normalized(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0])
            self.assertIn(name, pinned, f"{line} is in requirements.in but not compiled into requirements.txt")


if __name__ == "__main__":
    unittest.main()
