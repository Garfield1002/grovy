"""`grovy setup`, `grovy doctor`, `grovy url`: installing and checking grovy."""
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time

from . import claude, client, core, sessions, tmuxctl


def lan_bases(url):
    """http://HOST:PORT for each address the daemon is reachable on (all IPv4 addresses when bound to 0.0.0.0)."""
    m = re.match(r"http://(.+):(\d+)/?", url or "")
    host, port = (m.group(1), m.group(2)) if m else ("127.0.0.1", "7333")
    hosts = [host]
    if host in ("0.0.0.0", "::"):
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
        hosts = [h for h in out if ":" not in h] or ["127.0.0.1"]
    return [f"http://{h}:{port}" for h in hosts]


def cmd_url(_):
    """Print the phone login link(s): open one once per device; it sets a cookie."""
    try:
        url = (client.sock_request({"type": "status"}) or {}).get("url", "")
    except (OSError, ValueError):
        core.die("grovyd not running (grovy serve)")
    tok = core.load_token()
    bases = lan_bases(url)
    for base in bases:
        print(base + "/" + (f"?token={tok}" if tok else ""))
    if all(re.match(r"http://(127\.|localhost|\[::1\])", b) for b in bases):
        print("note: the daemon only listens on this computer, so these links won't open on a phone.\n"
              "Restart it with --bind 0.0.0.0 (or your VPN address), or set GROVY_BIND.", file=sys.stderr)
    if not tok:
        print("(no token: anyone who can reach these addresses can use grovy)", file=sys.stderr)
    return 0


def cmd_doctor(_):
    ok = True

    def check(label, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'ok' if good else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))

    check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    t = shutil.which("tmux")
    check("tmux", t, tmuxctl.tmux("-V").stdout.strip() if t else "not found")
    c = shutil.which(claude.claude_cmd())
    ver = subprocess.run([c, "--version"], capture_output=True, text=True).stdout.strip() if c else "not found"
    check("claude", c, ver)
    sd = core.state_dir()
    check("state dir private", (os.stat(sd).st_mode & 0o077) == 0, sd)
    try:
        st = client.sock_request({"type": "status"})
        check("daemon socket", True, f"{len(st.get('sessions', {}))} session(s) known")
        check("daemon http", True, st.get("url", ""))
        if "claude" in st:   # the daemon's PATH can differ from this shell's (systemd)
            check("daemon finds claude", st["claude"], st["claude"] or f"{claude.claude_cmd()!r} not on the daemon's PATH "
                  "(set PATH or GROVY_CLAUDE in its environment)")
    except (OSError, ValueError) as e:
        print(f"[info] daemon not running ({e}): start it with `grovy serve`")
    live, reg = tmuxctl.tmux_managed(), sessions.registry()
    for n in sorted(live):
        hs = os.path.join(sd, "hooks", f"{n}.json")
        check(f"session {n}: hooks file", os.path.exists(hs), hs)
    for n in sorted(set(reg) - set(live)):
        print(f"[info] session {n}: exited (grovy resume {n} / grovy forget {n})")
    return 0 if ok else 1


SERVICE_UNIT = """\
# Written by `grovy setup`; rerun it to change the address or port (or edit here, then
# systemctl --user daemon-reload && systemctl --user restart grovy).
[Unit]
Description=grovy - Claude Code session manager daemon

[Service]
ExecStart={exe} serve
Environment=GROVY_BIND={bind}
Environment=GROVY_PORT={port}
# The PATH of the shell that ran setup: claude (and the Claude sessions' Bash tool) need it.
Environment=PATH={path}
Restart=on-failure
RestartSec=2
# Essential: if this daemon starts the tmux server (first session from the phone), tmux is in this
# unit's cgroup, and the default KillMode would kill tmux and every Claude session on restart or crash.
KillMode=process

[Install]
WantedBy=default.target
"""


def service_unit(bind, port, path=None):
    path = path if path is not None else os.environ.get("PATH", "/usr/bin:/bin")
    path = ":".join(dict.fromkeys(p for p in path.split(":") if p))   # de-duplicated, order kept
    q = lambda v: str(v).replace("%", "%%")   # noqa: E731  systemd expands %-specifiers
    return SERVICE_UNIT.format(exe=q(os.path.join(core.HERE, "grovy")), bind=q(bind), port=int(port), path=q(path))


def _ask(prompt, default):
    try:
        got = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        got = ""
    return got or default


def _pick_bind(assume_yes):
    out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
    addrs = [a for a in out if ":" not in a]
    vpn = [a for a in addrs if ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10")]   # Tailscale
    choices = [("127.0.0.1", "this computer only (no phone access)"),
               ("0.0.0.0", "every network this computer is on")] + \
              [(a, "Tailscale/VPN address" if a in vpn else "this address only") for a in addrs]
    default = vpn[0] if vpn else "0.0.0.0"
    if assume_yes or not sys.stdin.isatty():
        return default
    print("Where should the phone reach grovy? Only expose it on networks you trust (it is plain HTTP).")
    for i, (a, what) in enumerate(choices, 1):
        print(f"  {i}) {a:<16} {what}")
    got = _ask("Address or number", default)
    if got.isdigit() and 1 <= int(got) <= len(choices):
        return choices[int(got) - 1][0]
    ipaddress.ip_address(got)   # ValueError on garbage
    return got


def cmd_setup(a):
    """One-time install: link grovy onto PATH, write and start the systemd user service, print the phone link."""
    dry = a.dry_run
    step = lambda msg: print(("(dry run) " if dry else "") + msg)  # noqa: E731
    for tool in ("tmux", claude.claude_cmd()):
        if not shutil.which(tool):
            core.die(f"{tool} not found on PATH: install it first" + (" (or set GROVY_CLAUDE)" if tool != "tmux" else ""))
    # 1. grovy on PATH
    bindir = os.path.expanduser("~/.local/bin")
    link, target = os.path.join(bindir, "grovy"), os.path.join(core.HERE, "grovy")
    if os.path.realpath(link) == os.path.realpath(target):
        step(f"ok: {link} already points here")
    elif os.path.lexists(link):
        step(f"skipped: {link} exists and points elsewhere ({os.path.realpath(link)}); remove it to use this copy")
    else:
        step(f"linking {link} -> {target}")
        if not dry:
            os.makedirs(bindir, exist_ok=True)
            os.symlink(target, link)
    if bindir not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: {bindir} is not on your PATH; add it in your shell profile: export PATH=\"$HOME/.local/bin:$PATH\"")
    # 2. token, and the templates for your own styles and script (everything in them starts commented out)
    if not dry:
        core.load_token(create=True)
    for name, what in (("custom.css", "your own styles"), ("custom.js", "your own additions to the page")):
        dest = os.path.join(core.config_dir(), name)
        if not os.path.exists(dest):
            step(f"creating {dest} ({what}; see the comments in it)")
            if not dry:
                os.makedirs(core.config_dir(), exist_ok=True)
                shutil.copyfile(os.path.join(core.HERE, "examples", name), dest)
    # 3. the daemon as a service
    bind = a.bind or _pick_bind(a.yes)
    ipaddress.ip_address(bind)
    unit = service_unit(bind, a.port)
    running = False
    try:
        running = bool(client.sock_request({"type": "status"}, timeout=1.0))
    except (OSError, ValueError):
        pass
    systemd = not a.no_service and shutil.which("systemctl") and subprocess.run(
        ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    if a.no_service or not systemd:
        if not a.no_service:
            print("no systemd user session here: start the daemon yourself, e.g. in tmux:")
        print(f"  tmux new -d -s grovyd 'GROVY_BIND={bind} GROVY_PORT={a.port} {target} serve'")
    else:
        path = os.path.expanduser("~/.config/systemd/user/grovy.service")
        old = open(path).read() if os.path.exists(path) else None
        if old == unit:
            step(f"ok: {path} is up to date")
        else:
            step(f"writing {path}" + (" (previous version kept as grovy.service.bak)" if old else ""))
            if dry:
                print(unit)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if old:
                    shutil.copyfile(path, path + ".bak")
                with open(path, "w") as f:
                    f.write(unit)
        active = subprocess.run(["systemctl", "--user", "is-active", "grovy"], capture_output=True,
                                text=True, timeout=10).stdout.strip() == "active"
        if running and not active:
            step("enabling the service for the next boot; a grovy daemon is already running outside it, so it "
                 "isn't started now.\n  Stop that one (sessions are unaffected), then: systemctl --user start grovy")
            cmds = [["daemon-reload"], ["enable", "grovy"]]
        else:
            step("starting the service and enabling it at boot")
            cmds = [["daemon-reload"], ["enable", "grovy"], ["restart", "grovy"]]
        for c in cmds if not dry else []:
            r = subprocess.run(["systemctl", "--user", *c], capture_output=True, text=True, timeout=30)
            if r.returncode:
                core.die(f"systemctl --user {' '.join(c)} failed: {r.stderr.strip()}")
        linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if linger != "Linger=yes":
            step("enabling linger, so the daemon runs after a reboot without you logging in")
            if not dry and subprocess.run(["loginctl", "enable-linger"], capture_output=True, timeout=30).returncode:
                print("note: `loginctl enable-linger` failed; without it the daemon starts when you log in")
    if dry:
        return 0
    # 4. the phone link
    for _ in range(50):
        try:
            if client.sock_request({"type": "status"}, timeout=1.0):
                break
        except (OSError, ValueError):
            time.sleep(0.2)
    else:
        print("the daemon isn't answering yet; when it is, run `grovy url` for the phone link")
        return 0
    print("\nOpen this once on your phone (it logs the phone in):")
    cmd_url(None)
    print("\nThen tap + to start a session. New projects: run `claude` once in the folder first, to accept its "
          "trust prompt.\nMake it yours in " + core.config_dir() + " (custom.css, custom.js); check everything with "
          "`grovy doctor`.")
    return 0
