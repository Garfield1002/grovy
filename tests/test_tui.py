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

    def test_table_widths_and_styles(self):
        tty = mock.Mock(isatty=lambda: True)
        with mock.patch.dict(os.environ, {"TERM": "xterm"}):
            os.environ.pop("NO_COLOR", None)
            lines, w = tui.table([["日本", ("idle", "green"), "/a"], [[("pc:", "dim"), "b"], "x", "/b"]],
                                 header=["NAME", "STATE", "PATH"], stream=tty, total=80)
        self.assertEqual(w, [4, 5, 4])
        plain = [tui._ANSI.sub("", x) for x in lines]
        self.assertEqual(plain, ["NAME  STATE  PATH", "日本  idle   /a", "pc:b  x      /b"])   # 日本 is 4 columns
        self.assertIn("\x1b[32midle\x1b[0m", lines[1])
        self.assertIn("\x1b[2mNAME\x1b[0m", lines[0])
        self.assertEqual(tui.cell_width("é"), 1)   # a combining accent takes no column

    def test_table_cuts_to_the_terminal(self):
        rows = [["name", "idle", "/home/me/" + "deep/" * 20 + "end"]]
        lines, _ = tui.table(rows, header=["NAME", "STATE", "PATH"], cut_start=(2,), total=40)
        self.assertTrue(all(tui.cell_width(x) <= 40 for x in lines))
        self.assertTrue(lines[1].endswith("deep/end") and "…" in lines[1])   # a path keeps its end
        lines, _ = tui.table([["a", "x" * 100]], total=20)
        self.assertEqual(lines[0], "a  " + "x" * 16 + "…")
        lines, _ = tui.table([["a", "/" + "s" * 30, "why it failed"]], shrink=(1, 2), cut_start=(1,), total=30)
        self.assertEqual(lines[0], "a  …" + "s" * 11 + "  why it failed")   # the socket is cut first
        lines, _ = tui.table([["日本語日本語日本語"]], total=7)
        self.assertLessEqual(tui.cell_width(lines[0]), 7)

    def test_table_plain_off_a_terminal_and_with_no_color(self):
        tty = mock.Mock(isatty=lambda: True)
        rows = [[("a", "red", "bold"), [("pc:", "dim"), "b"]]]
        self.assertNotIn("\x1b", "".join(tui.table(rows, header=["X", "Y"], stream=io.StringIO())[0]))
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}):
            self.assertNotIn("\x1b", "".join(tui.table(rows, header=["X", "Y"], stream=tty)[0]))

    def test_table_cells_cannot_carry_escape_sequences(self):
        lines, w = tui.table([["evil\x1b]52;c;x\x07", "\x1b[2Jx\nfake row"]], stream=io.StringIO())
        self.assertNotIn("\x1b", lines[0])
        self.assertNotIn("\n", lines[0])
        self.assertNotIn("\x07", lines[0])
        self.assertEqual(w[0], len("evil?]52;c;x?"))

    def test_table_lines_up_a_second_block(self):
        _, w = tui.table([["a", "idle", "/a"]], header=["NAME", "STATE", "PATH"], total=80)
        lines, _ = tui.table([["pc:b", "working", "/b"]], widths=w, total=80)
        self.assertEqual(lines[0], "pc:b  working  /b")

    def test_tree_fits_and_its_text_goes_below_when_narrow(self):
        self.assertLessEqual(max(len(x) for x in tui.TREE), 42)
        side = ["grovy", "", "Claude Code sessions in tmux,"]
        for cols, beside in ((100, True), (50, False)):
            out = io.StringIO()
            with mock.patch.object(tui, "width", lambda: cols), mock.patch("sys.stdout", out):
                tui.tree(side)
            lines = out.getvalue().splitlines()
            self.assertTrue(all(len(x) <= cols for x in lines), lines)
            self.assertEqual(lines[0].endswith("grovy"), beside)
            self.assertIn("Claude Code sessions in tmux,", lines[2] if beside else lines[-1])

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
