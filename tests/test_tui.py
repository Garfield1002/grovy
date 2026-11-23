"""grovylib/tui.py: menus, questions, colours; and a real arrow-key menu driven through a pseudo-terminal."""
import io
import os
import pty
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from grovylib import tui  # noqa: E402

OPTS = [{"label": "Trunk", "value": "trunk", "hint": "(most people)", "detail": ["A new grovy."]},
        {"label": "Graft", "value": "graft"}]


class TestTui(unittest.TestCase):
    def test_select_moves_wraps_and_collapses(self):
        out = io.StringIO()
        self.assertEqual(tui.select("Role?", OPTS, keys=["down", "enter"], out=out), "graft")
        self.assertIn("✓ Role? Graft", out.getvalue().rsplit("\x1b[J", 1)[1])   # the menu collapses to the answer
        self.assertEqual(tui.select("Role?", OPTS, keys=["up", "enter"], out=io.StringIO()), "graft")   # wraps
        self.assertEqual(tui.select("Role?", OPTS, default=1, keys=["1", "enter"], out=io.StringIO()), "trunk")
        self.assertEqual(tui.select("Role?", OPTS, keys=["x", "esc", "enter"], out=io.StringIO()), "trunk")
        with self.assertRaises(KeyboardInterrupt):
            tui.select("Role?", OPTS, keys=["ctrl-c"], out=io.StringIO())

    def test_without_a_terminal_every_question_takes_its_default(self):
        with mock.patch.object(tui, "interactive", lambda: False):
            self.assertEqual(tui.select("Role?", OPTS, default=1), "graft")
            self.assertEqual(tui.text("Name?", "pc"), "pc")
            self.assertFalse(tui.confirm("Sure?", False))

    def test_text_rechecks_until_valid(self):
        answers = iter(["nope", " 7340 "])
        check = lambda v: None if v.isdigit() else "not a number"  # noqa: E731
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(tui.text("Port?", "7333", check, ask=lambda p: next(answers)), "7340")
        self.assertIn("not a number", out.getvalue())
        self.assertEqual(tui.text("Port?", "7333", ask=lambda p: ""), "7333")
        self.assertTrue(tui.confirm("Sure?", True, ask=lambda p: ""))
        self.assertFalse(tui.confirm("Sure?", True, ask=lambda p: "n"))

    def test_long_lines_are_cut_so_redraws_count_right(self):
        s = tui._cut("\x1b[1m" + "x" * 100 + "\x1b[0m", 20)
        self.assertEqual(tui.visible_len(s), 20)
        self.assertTrue(s.endswith("\x1b[0m"))
        with mock.patch.object(tui, "width", lambda: 40):
            out = io.StringIO()
            tui.select("Q" * 200, OPTS, keys=["enter"], out=out)
        for line in out.getvalue().replace("\x1b[J", "\n").split("\n"):
            self.assertLessEqual(tui.visible_len(re.sub(r"\x1b\[\d+A", "", line)), 39)

    def test_colour_only_on_a_terminal(self):
        self.assertEqual(tui.paint("x", "bold", stream=io.StringIO()), "x")
        tty = mock.Mock(isatty=lambda: True)
        self.assertIn("\x1b[", tui.paint("x", "bold", stream=tty))
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(tui.paint("x", "bold", stream=tty), "x")

    def test_tree_has_one_width(self):
        widths = {sum(len(t) for t, _ in runs) for runs in tui.TREE}
        self.assertLessEqual(max(widths), 42)

    def test_arrow_keys_in_a_real_terminal(self):
        """The termios path: cbreak mode, an escape sequence for ↓, enter; and the terminal restored after."""
        code = ("import sys, termios; sys.path.insert(0, %r); from grovylib import tui\n"
                "before = termios.tcgetattr(0)\n"
                "v = tui.select('Role?', [{'label': 'Trunk', 'value': 't'}, {'label': 'Graft', 'value': 'g'}])\n"
                "print('GOT', v, 'RESTORED', termios.tcgetattr(0) == before)\n") % ROOT
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-c", code])
        out = b""
        try:
            deadline = time.time() + 10
            while b"to move" not in out and time.time() < deadline:
                if select.select([fd], [], [], 0.2)[0]:
                    out += os.read(fd, 4096)
            os.write(fd, b"\x1b[B")
            time.sleep(0.2)
            os.write(fd, b"\r")
            while b"RESTORED" not in out and time.time() < deadline:
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        out += os.read(fd, 4096)
                    except OSError:
                        break
        finally:
            os.waitpid(pid, 0)
            os.close(fd)
        self.assertIn(b"GOT g RESTORED True", out)


import re  # noqa: E402

if __name__ == "__main__":
    unittest.main()
