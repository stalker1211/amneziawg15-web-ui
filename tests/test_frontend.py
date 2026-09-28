"""Static checks on the frontend: it is self-contained, and dark mode cannot be forgotten.

The page loads nothing from other hosts (Tailwind is compiled by build_css.sh, the two
libraries live in static/vendor/ with pinned hashes), and every colour utility in the
markup either carries a `dark:` partner in the same class string or is on the short
list of colours that deliberately look the same in both themes.
"""

import base64
import hashlib
import re
import unittest
from pathlib import Path

from tests.support import WEB_UI_DIR

WEB_UI = Path(WEB_UI_DIR)
INDEX = WEB_UI / "templates" / "index.html"
MARKUP = [INDEX, *sorted((WEB_UI / "static" / "js").glob("*.js"))]

# Unmodified release files; the hashes are the ones cdnjs publishes (socket.io.js is
# also byte-identical to dist/socket.io.js in the socket.io-client npm tarball).
VENDORED = {
    "socket.io-4.8.4.js": "sha512-dDCXSKyPaEikucluA6BrNqHngOGByBEhxwwlP+GvVt90XOe0BpmZEdnIJi849R2q9rG23Yzxhiy4uKCdnrJySw==",
    "qrcode-1.0.0.min.js": "sha512-CNgIRecGo7nphbeZ04Sc13ka07paqdeTu0WR1IM4kNcpmBAUSHSQX0FslNhTDadL4O5SAGapGt4FodqL8My0mA==",
}

PALETTE = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|"
    "violet|purple|fuchsia|pink|rose"
)
COLOUR = re.compile(rf"(?P<kind>bg|text|border)-(?:white|black|(?:{PALETTE})-\d{{2,3}})(?:/\d+)?")

# A new colour class must get a dark: partner or be added to one of these on purpose.
#
# Look right on both backgrounds: solid buttons, toggle tracks, status dots, dividers,
# white/mid-grey text, the log pane (dark in both themes), the modal backdrop.
THEME_NEUTRAL = {
    "bg-amber-300", "bg-blue-500", "bg-blue-600", "bg-gray-200", "bg-gray-300", "bg-gray-400", "bg-gray-500",
    "bg-gray-600", "bg-green-500", "bg-purple-500", "bg-red-500", "bg-red-600", "text-emerald-200",
    "text-gray-400", "text-white",
}  # fmt: skip
# Given their dark look by a style.css rule instead: the two refresh buttons
# (#refreshIpBtn, .egress-refresh-btn) and the h3/h4 element colour.
DARK_VIA_STYLE_CSS = {"bg-white/70", "bg-white/80", "border-blue-200/70", "text-blue-700"}
# Known gaps, kept as-is so the build change stayed pixel-identical: the error boxes'
# dark-red text and light border on the dark red panel, and the QR card's light frame.
# Fix these in the GUI redesign (DEVELOPMENT.md §10 #15).
LIGHT_ONLY_TODAY = {"border-gray-100", "border-red-200", "text-red-700"}
ALLOWED_UNPAIRED = THEME_NEUTRAL | DARK_VIA_STYLE_CSS | LIGHT_ONLY_TODAY


def class_units():
    """Every class list in the markup: class="..." attributes and quoted JS strings."""
    units = []
    for path in MARKUP:
        text = path.read_text(encoding="utf-8")
        units += re.findall(r'class="([^"]*)"', text)
        units += re.findall(r"className\s*=\s*[`'\"]([^`'\"]*)", text)
        units += re.findall(r"'([^'\n]*)'", text)
    return units


class SelfContainedTests(unittest.TestCase):
    def test_page_loads_nothing_from_another_host(self):
        html = INDEX.read_text(encoding="utf-8")
        refs = re.findall(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', html)
        self.assertTrue(refs)
        self.assertEqual([r for r in refs if re.match(r"(https?:)?//", r)], [])

    def test_vendored_libraries_match_their_release_hashes(self):
        vendor = WEB_UI / "static" / "vendor"
        self.assertEqual({p.name for p in vendor.iterdir()}, set(VENDORED))
        for name, expected in VENDORED.items():
            digest = base64.b64encode(hashlib.sha512((vendor / name).read_bytes()).digest()).decode()
            self.assertEqual(f"sha512-{digest}", expected, name)

    def test_index_references_the_vendored_files_and_built_css(self):
        html = INDEX.read_text(encoding="utf-8")
        for name in (*VENDORED, "css/tailwind.css"):
            self.assertIn(name, html)
        # style.css first: the compiled utilities must win ties with it, as before.
        self.assertLess(html.index("css/style.css"), html.index("css/tailwind.css"))


class NativeDialogTests(unittest.TestCase):
    def test_no_alert_confirm_or_prompt(self):
        # They block the page and cannot follow the theme; ui.js has toasts, an in-app
        # confirm, askText and inline rename instead.
        call = re.compile(r"(?<![.\w])(?<!function )(?:window\.)?(alert|confirm|prompt)\(")
        found = []
        for path in sorted((WEB_UI / "static" / "js").glob("*.js")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("//", 1)[0]
                if call.search(code):
                    found.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(found, [])


class DarkModeTests(unittest.TestCase):
    def test_every_colour_class_has_a_dark_partner(self):
        unpaired = set()
        for unit in class_units():
            tokens = unit.split()
            dark_kinds = {t.split(":", 1)[1].split("-", 1)[0] for t in tokens if t.startswith("dark:")}
            for token in tokens:
                m = COLOUR.fullmatch(token)
                if m and m["kind"] not in dark_kinds and token not in ALLOWED_UNPAIRED:
                    unpaired.add(token)
        self.assertEqual(sorted(unpaired), [], "add a dark: class next to these, or list them on purpose above")

    def test_the_allow_lists_have_no_stale_entries(self):
        # Keeps the lists honest: an entry whose class is gone from the markup goes too.
        used = {t for unit in class_units() for t in unit.split() if COLOUR.fullmatch(t)}
        self.assertEqual(sorted(ALLOWED_UNPAIRED - used), [])

    def test_the_old_override_style_is_gone(self):
        css = (WEB_UI / "static" / "css" / "style.css").read_text(encoding="utf-8")
        # body.dark .<utility> { ... !important } rules are what the dark: classes replaced.
        self.assertEqual(re.findall(r"body\.dark \.(?:bg|text|border|shadow)-[\w\\/-]+", css), [])


if __name__ == "__main__":
    unittest.main()
