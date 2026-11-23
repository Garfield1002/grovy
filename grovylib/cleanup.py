"""`grovy cleanup`: undo what `grovy setup` did, so this computer is as it was (or ready for a fresh setup).

It removes what setup recorded in its setup record (`install.record_path()`), and, for an install made before that
record existed, what it finds in the places setup writes to. It is planned first, shown, and applied only on a yes:
  * the systemd user services (grovy, grovy-tunnel) and their unit files; a daemon started by hand is stopped too
  * the `~/.local/bin/grovy` link, only if it points to this grovy folder
  * linger, only if setup turned it on (the record says so); otherwise it's left as it is
  * the state folder (sessions list, login token, certificates, hooks files) and the files sent from the phone
  * the grovy home (custom.css/js, plugins, peers.json) only if asked: it's kept by default
Running Claude sessions are only ended on a yes (default no). Without grovy they keep running in plain tmux (until
you log out, once its service and linger are gone).
The grovy folder itself (the code) is never touched.
"""
import os
import shutil
import signal
import subprocess
import time

from . import client, core, graft, install, tmuxctl, tui

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
    """A folder cleanup may delete: an existing directory (not a symlink, trailing slash or not), not / or your home
    or one of its parents, not holding the grovy code, and named for grovy (a GROVY_STATE_DIR=~ typo must never wipe
    a home)."""
    if not path:
        return False
    path = os.path.normpath(path)   # "link/" would make islink() look through the link
    if not os.path.isdir(path) or os.path.islink(path):
        return False
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    here = os.path.realpath(core.HERE)
    if real == "/" or real == home or home.startswith(real.rstrip("/") + "/"):
        return False
    if here == real or here.startswith(real.rstrip("/") + "/"):
        return False
    return "grovy" in os.path.basename(real) or "ccs" in os.path.basename(real)


def must(fn):
    """A step whose failure stops cleanup: what follows (removing state under a daemon still running) isn't safe."""
    fn.must = True
    return fn


def soft(fn):
    """A step whose failure is only a warning: removing this computer from a trunk that is gone for good must not
    keep it from being cleaned up here (the key is deleted below, so the trunk's line for it is dead anyway)."""
    fn.soft = True
    return fn


def final(fn):
    """A step skipped if anything before it failed: it removes what a rerun needs (the record, the tunnel key)."""
    fn.final = True
    return fn


def unit_owner(path):
    """The grovy folder a unit file runs (its ExecStart), or None if it can't tell."""
    try:
        with open(path) as f:
            for line in f:
                key, _, value = line.partition("=")
                if key.strip() == "ExecStart" and value.strip():
                    words = [w.lstrip("-@+!:").strip('"').replace("%h", os.path.expanduser("~")).replace("%%", "%")
                             for w in value.split()]
                    if os.path.basename(words[0]).startswith("python") and len(words) > 1:
                        words = words[1:]   # python3 .../grovy.py serve
                    return os.path.dirname(os.path.realpath(words[0]))
    except (OSError, IndexError):
        pass
    return None


def _ours(path):
    return unit_owner(path) == os.path.realpath(core.HERE)


def plan(record, systemd, state_existed=True):
    """([(label, action)] in the order they run, or None with why cleanup can't go on). Nothing runs here."""
    steps = []
    # 1. the services, and a daemon running outside them (started by hand, in tmux)
    for name in UNITS:
        path = _unit_path(name)
        if os.path.exists(path) and not _ours(path):
            return None, (f"{tui.short(path)} runs another grovy ({unit_owner(path) or 'unknown'}), which uses the same "
                          f"state: run grovy cleanup from that folder, or remove the service yourself")
    service_pid = None
    for name in UNITS:
        if os.path.exists(_unit_path(name)) or (systemd and name in record.get("services", [])):
            if systemd and name == "grovy":
                try:
                    service_pid = int(_systemctl("show", "-p", "MainPID", "--value", name, timeout=10).stdout) or None
                except (ValueError, OSError, subprocess.SubprocessError):
                    pass
            steps.append((f"stop and remove the {name} service ({tui.short(_unit_path(name))})",
                          must(lambda n=name: _remove_service(n, systemd))))
    try:
        st = client.sock_request({"type": "status"}, timeout=1.0) or {}
    except (OSError, ValueError):
        st = {}
    pid = st.get("pid") if isinstance(st.get("pid"), int) and st.get("pid") > 1 else None
    if st and pid and pid != service_pid:
        steps.insert(0, (f"stop the grovy daemon running outside the service (pid {pid})", must(lambda: _stop_pid(pid))))
    elif st and not pid and not service_pid:
        return None, ("a grovy daemon is running and doesn't say which process it is (it predates cleanup): stop it "
                      "yourself (e.g. tmux kill-session -t grovyd), then run grovy cleanup again")
    if systemd and any(os.path.exists(_unit_path(n) + ext) for n in UNITS for ext in ("", ".bak")):
        steps.append(("reload systemd", lambda: _systemctl("daemon-reload")))
    # the graft's entry on its trunk (over your usual ssh), and the tunnel key setup made
    g = record.get("graft") if isinstance(record.get("graft"), dict) else {}
    olds = [o for o in record.get("old_grafts", []) if isinstance(o, dict)]   # trunks setup couldn't reach to leave
    for o in [g, *olds]:
        if o.get("trunk") and o.get("name"):
            steps.append((f"on {o['trunk']}: remove this computer ({o['name']}) and its tunnel key line",
                          soft(lambda o=o: graft.remove_on_trunk(o))))
    if not g.get("key") and any(o.get("key") for o in olds):
        g = {"key": next(o["key"] for o in olds if o.get("key"))}
    key = g.get("key")
    if isinstance(key, str) and os.path.realpath(key) == os.path.realpath(graft.KEY) and os.path.exists(key):
        steps.append((f"remove the tunnel key {tui.short(key)}",
                      final(lambda: [os.unlink(p) for p in (key, key + ".pub") if os.path.exists(p)])))
    # 2. the command on PATH
    link = os.path.expanduser("~/.local/bin/grovy")
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(os.path.join(core.HERE, "grovy")):
        steps.append((f"remove {tui.short(link)}", lambda: os.unlink(link)))
    # 3. linger: only undone when setup turned it on
    if record.get("linger"):
        steps.append(("turn linger off (setup turned it on)", _linger_off))
    # 4. what grovy keeps: state (token, sessions list, certificates, the setup record) and the files sent from the
    # phone. Last, and only if nothing failed: a rerun needs the record.
    sd = os.path.normpath(install.state_path())   # removed exactly as checked
    if state_existed and safe_to_remove(sd):
        steps.append((f"remove {tui.short(sd)} (sessions list, login token, certificates)",
                      final(lambda: shutil.rmtree(sd))))
        old = os.path.join(os.path.dirname(os.path.normpath(sd)), "ccs")   # the pre-rename name, left as a symlink
        if os.path.islink(old) and os.path.realpath(old) == os.path.realpath(sd):
            steps.append((f"remove {old} (link left by the rename from ccs)", final(lambda: os.unlink(old))))
    elif state_existed and os.path.isdir(sd):
        tui.warn(f"{sd} is kept: it doesn't look like a folder of grovy's own (or it is a link): remove it yourself")
    att = os.path.normpath(os.path.dirname(core.attachments_dir()))
    if safe_to_remove(att):
        steps.append((f"remove {att} (files sent from the phone)", final(lambda: shutil.rmtree(att))))
    return steps, None


def _remove_service(name, systemd):
    if systemd:
        r = _systemctl("disable", "--now", name, timeout=60)
        if r.returncode and "not loaded" not in r.stderr and "does not exist" not in r.stderr:
            raise RuntimeError(f"systemctl --user disable --now {name}: {r.stderr.strip() or 'failed'}")
    wants = os.path.expanduser(f"~/.config/systemd/user/default.target.wants/{name}.service")
    for path in (_unit_path(name), _unit_path(name) + ".bak", wants):
        if os.path.lexists(path):
            os.unlink(path)


def _stop_pid(pid):
    """Stop the hand-started daemon, checking first that it is still the one that said it was `pid` (not a process
    that got the number since)."""
    try:
        st = client.sock_request({"type": "status"}, timeout=5.0) or {}
    except (ConnectionRefusedError, FileNotFoundError):
        return   # already gone
    except (OSError, ValueError) as e:   # busy or confused: not proof it's gone
        raise RuntimeError(f"the daemon didn't answer ({e}): stop it yourself, then run grovy cleanup again")
    if st.get("pid") != pid:
        raise RuntimeError(f"the daemon isn't pid {pid} any more: run grovy cleanup again")
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
        return subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"], capture_output=True,
                              text=True, timeout=10).stdout.strip() == "Linger=yes"
    except (OSError, subprocess.SubprocessError):
        return False


def _linger_off():
    r = subprocess.run(["loginctl", "disable-linger"], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "loginctl disable-linger failed")


def cmd_cleanup(a):
    if core.env("SESSION"):
        core.die("this runs inside a grovy session, which cleanup would cut off: run it from a plain terminal")
    interactive = tui.interactive() and not a.yes
    sd = install.state_path()
    existed = os.path.isdir(sd)   # before anything below (the daemon socket, tmux) makes it
    record = install.read_record()
    systemd = _have_systemd()
    try:
        steps, why = plan(record, systemd, existed)
        if steps is None:
            core.die(why)
        return _run(a, interactive, record, steps)
    finally:
        if not existed and os.path.isdir(sd) and not os.listdir(sd):
            os.rmdir(sd)   # made only by looking: leave the computer as it was


def _run(a, interactive, record, steps):
    live = sorted(tmuxctl.tmux_managed())
    home = os.path.normpath(core.home_env() or record.get("home") or core.config_dir())
    tui.say(tui.paint("Remove grovy from this computer", "bold"))
    tui.note(f"The grovy folder ({tui.short(core.HERE)}) is kept: grovy setup sets it up again.")
    if not record:
        tui.note("(no setup record: removing what is found in the usual places)")
    end_sessions = a.end_sessions
    if live and not end_sessions and interactive:
        end_sessions = tui.confirm(f"End the {len(live)} running Claude session(s) ({', '.join(live)})? Without grovy "
                                   "they keep running in tmux", False)
    if live and end_sessions:
        steps.insert(0, (f"end the Claude sessions {', '.join(live)}",
                         lambda: [tmuxctl.tmux("kill-session", "-t", "=" + tmuxctl.tmux_name(n), check=False)
                                  for n in live]))
    elif live:
        tui.note(f"{len(live)} Claude session(s) keep running in tmux ({', '.join(live)}); without grovy's service "
                 "and linger they may stop when you log out.")
    remove_home = a.remove_home
    if os.path.isdir(home) and not remove_home and interactive:
        remove_home = not tui.confirm(f"Keep your grovy home {tui.short(home)} (custom.css/js, plugins)?", True)
    if remove_home and os.path.isdir(home):
        if safe_to_remove(home):
            steps.append((f"remove {tui.short(home)} (your grovy home)", lambda: shutil.rmtree(home)))
        else:
            tui.warn(f"{home} doesn't look like a folder of grovy's own: remove it yourself")
    if not record.get("linger") and _linger_on():
        tui.note("Linger stays on (setup didn't turn it on); loginctl disable-linger turns it off.")
    steps = [x for x in steps if not getattr(x[1], "final", False)] + \
        [x for x in steps if getattr(x[1], "final", False)]   # what a rerun needs goes last, after everything else
    tui.say()
    if not steps:
        tui.ok("Nothing to remove: grovy isn't set up here.")
        return 0
    tui.say(tui.paint("Remove:", "bold"))
    for label, _ in steps:
        tui.say(f"  • {label}")
    if a.dry_run:
        tui.note("(dry run: nothing changed)")
        return 0
    if interactive and not tui.confirm("Apply?", False):
        tui.say("Nothing changed.")
        return 1
    failed = 0
    for label, action in steps:
        if failed and getattr(action, "final", False):
            tui.note(f"  – skipped: {label} (kept so that grovy cleanup can run again)")
            continue
        try:
            action()
            tui.ok(label)
        except Exception as e:   # keep going, except after a step whose failure makes the rest unsafe
            if getattr(action, "soft", False):
                tui.warn(f"{label}: {e}")
                continue
            failed += 1
            tui.fail(f"{label}: {e}")
            if getattr(action, "must", False):
                tui.say("Stopped there: nothing else was changed.")
                break
    if failed:
        tui.say(f"{failed} step(s) failed; fix that and run grovy cleanup again.")
        return 1
    tui.say("Done. grovy setup sets it up again.")
    return 0


def add_parser(sub):
    s = sub.add_parser("cleanup", help="undo grovy setup: remove the service, the command link, grovy's state")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask (keeps sessions running and your grovy home)")
    s.add_argument("--end-sessions", action="store_true", help="also end the running Claude sessions")
    s.add_argument("--remove-home", action="store_true",
                   help="also remove your grovy home (custom.css/js, plugins, peers.json)")
    s.add_argument("--dry-run", action="store_true", help="show what would be removed, change nothing")
    s.set_defaults(fn=cmd_cleanup)
