"""`grovy cleanup`: undo what `grovy setup` did, so this computer is as it was (or ready for a fresh setup).

It removes what setup recorded in its setup record (`install.record_path()`), and, for an install made before that
record existed, what it finds in the places setup writes to. It is planned first, shown, and applied only on a yes:
  * the systemd user services (grovy, grovy-tunnel) and their unit files; a daemon started by hand is stopped too
  * the `~/.local/bin/grovy` link, only if it points to this grovy folder
  * linger, only if setup turned it on (the record says so); otherwise it's left as it is
  * the state folder (sessions list, login token, certificates, hooks files) and the files sent from the phone
  * the grovy home (custom.css/js, plugins, peers.json) only if asked: it's kept by default
Running Claude sessions are only ended on a yes (default no). Without grovy they keep running in plain tmux.
The grovy folder itself (the code) is never touched.
"""
import os
import shutil
import signal
import subprocess
import sys
import time

from . import client, core, install, tmuxctl

UNITS = ("grovy-tunnel", "grovy")   # the tunnel first: it talks to the daemon


def _unit_path(name):
    return os.path.expanduser(f"~/.config/systemd/user/{name}.service")


def _systemctl(*args, timeout=30):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)


def _have_systemd():
    try:
        return bool(shutil.which("systemctl")) and _systemctl("show-environment", timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def safe_to_remove(path):
    """A folder cleanup may delete: an existing directory, not / or your home or one of its parents, not holding
    the grovy code, and named for grovy (a GROVY_STATE_DIR=~ typo must never wipe a home)."""
    if not path or not os.path.isdir(path) or os.path.islink(path):
        return False
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    here = os.path.realpath(core.HERE)
    if real == "/" or real == home or home.startswith(real.rstrip("/") + "/"):
        return False
    if here == real or here.startswith(real.rstrip("/") + "/"):
        return False
    return "grovy" in os.path.basename(real) or "ccs" in os.path.basename(real)


def plan(record, systemd):
    """([(label, action)] in the order they run, or None with why cleanup can't go on). Nothing runs here."""
    steps = []
    # 1. the services, and a daemon running outside them (started by hand, in tmux)
    service_pid = None
    for name in UNITS:
        if os.path.exists(_unit_path(name)) or (systemd and name in record.get("services", [])):
            if systemd and name == "grovy":
                try:
                    service_pid = int(_systemctl("show", "-p", "MainPID", "--value", name, timeout=10).stdout) or None
                except (ValueError, OSError, subprocess.SubprocessError):
                    pass
            steps.append((f"stop and remove the {name} service ({_unit_path(name)})",
                          lambda n=name: _remove_service(n, systemd)))
    try:
        st = client.sock_request({"type": "status"}, timeout=1.0) or {}
    except (OSError, ValueError):
        st = {}
    pid = st.get("pid") if isinstance(st.get("pid"), int) else None
    if st and pid and pid != service_pid:
        steps.insert(0, (f"stop the grovy daemon running outside the service (pid {pid})", lambda: _stop_pid(pid)))
    elif st and not pid and not service_pid:
        return None, ("a grovy daemon is running and doesn't say which process it is (it predates cleanup): stop it "
                      "yourself (e.g. tmux kill-session -t grovyd), then run grovy cleanup again")
    if systemd and any(os.path.exists(_unit_path(n) + ext) for n in UNITS for ext in ("", ".bak")):
        steps.append(("reload systemd", lambda: _systemctl("daemon-reload")))
    # 2. the command on PATH
    link = os.path.expanduser("~/.local/bin/grovy")
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(os.path.join(core.HERE, "grovy")):
        steps.append((f"remove {link}", lambda: os.unlink(link)))
    # 3. linger: only undone when setup turned it on
    if record.get("linger"):
        steps.append(("turn linger off (setup turned it on)",
                      lambda: subprocess.run(["loginctl", "disable-linger"], capture_output=True, timeout=30)))
    # 4. what grovy keeps: state (token, sessions list, certificates) and the files sent from the phone
    sd = core.state_dir()
    if safe_to_remove(sd):
        steps.append((f"remove {sd} (sessions list, login token, certificates)", lambda: shutil.rmtree(sd)))
        old = os.path.join(os.path.dirname(sd), "ccs")   # the pre-rename name, left as a symlink to it
        if os.path.islink(old) and os.path.realpath(old) == os.path.realpath(sd):
            steps.append((f"remove {old} (link left by the rename from ccs)", lambda: os.unlink(old)))
    att = os.path.dirname(core.attachments_dir())
    if safe_to_remove(att):
        steps.append((f"remove {att} (files sent from the phone)", lambda: shutil.rmtree(att)))
    return steps, None


def _remove_service(name, systemd):
    if systemd:
        _systemctl("disable", "--now", name, timeout=60)
    for path in (_unit_path(name), _unit_path(name) + ".bak"):
        if os.path.lexists(path):
            os.unlink(path)


def _stop_pid(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise RuntimeError(f"the daemon (pid {pid}) didn't stop")


def _linger_on():
    try:
        return subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"], capture_output=True,
                              text=True, timeout=10).stdout.strip() == "Linger=yes"
    except (OSError, subprocess.SubprocessError):
        return False


def _ask_yes(prompt, default):
    try:
        got = input(f"{prompt} ({'Y/n' if default else 'y/N'}) ").strip().lower()
    except EOFError:
        got = ""
    return default if not got else got in ("y", "yes")


def cmd_cleanup(a):
    if core.env("SESSION"):
        core.die("this runs inside a grovy session, which cleanup would cut off: run it from a plain terminal")
    interactive = sys.stdin.isatty() and not a.yes
    record = install.read_record()
    systemd = _have_systemd()
    steps, why = plan(record, systemd)
    if steps is None:
        core.die(why)
    live = sorted(tmuxctl.tmux_managed())
    home = core.config_dir()
    print("This removes grovy from this computer. The grovy folder (" + core.HERE + ") is kept.")
    if not record:
        print("(no setup record: removing what is found in the usual places)")
    end_sessions = a.end_sessions
    if live and not end_sessions and interactive:
        end_sessions = _ask_yes(f"End the {len(live)} running Claude session(s) ({', '.join(live)})? Without grovy "
                                "they keep running in tmux.", False)
    if live and end_sessions:
        steps.insert(0, (f"end the Claude sessions {', '.join(live)}",
                         lambda: [tmuxctl.tmux("kill-session", "-t", "=" + tmuxctl.tmux_name(n), check=False)
                                  for n in live]))
    elif live:
        print(f"note: {len(live)} Claude session(s) keep running in tmux ({', '.join(live)}).")
    remove_home = a.remove_home
    if os.path.isdir(home) and not remove_home and interactive:
        remove_home = not _ask_yes(f"Keep your grovy home {home} (custom.css/js, plugins)?", True)
    if remove_home and os.path.isdir(home):
        if safe_to_remove(home):
            steps.append((f"remove {home} (your grovy home)", lambda: shutil.rmtree(home)))
        else:
            print(f"note: {home} doesn't look like a folder of grovy's own: remove it yourself")
    if not record.get("linger") and _linger_on():
        print("note: linger stays on (setup didn't turn it on); loginctl disable-linger turns it off")
    if not steps:
        print("Nothing to remove: grovy isn't set up here.")
        return 0
    print("\nRemove:")
    for label, _ in steps:
        print(f"  • {label}")
    if a.dry_run:
        print("(dry run: nothing changed)")
        return 0
    if interactive and not _ask_yes("Apply?", False):
        print("Nothing changed.")
        return 1
    failed = 0
    for label, action in steps:
        try:
            action()
            print(f"  ✓ {label}")
        except Exception as e:   # keep going: each step is independent, and the user sees what's left
            failed += 1
            print(f"  ✗ {label}: {e}")
    if failed:
        print(f"{failed} step(s) failed; fix that and run grovy cleanup again.")
        return 1
    print("Done. grovy setup sets it up again.")
    return 0


def add_parser(sub):
    s = sub.add_parser("cleanup", help="undo grovy setup: remove the service, the command link, grovy's state")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask (keeps sessions running and your grovy home)")
    s.add_argument("--end-sessions", action="store_true", help="also end the running Claude sessions")
    s.add_argument("--remove-home", action="store_true",
                   help="also remove your grovy home (custom.css/js, plugins, peers.json)")
    s.add_argument("--dry-run", action="store_true", help="show what would be removed, change nothing")
    s.set_defaults(fn=cmd_cleanup)
