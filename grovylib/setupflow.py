"""`grovy setup`: a guided install. Every question is asked first, then one review screen lists what will change,
and only a yes applies it, step by step (✓/✗). The record of what it changed (`install.update_record`) is what
`grovy cleanup` undoes.

Roles: a *trunk* is a grovy of its own (the default); a *graft* joins this computer to a trunk on another machine
(grovy's peers, `grovylib/peers.py`). Run again on a computer that is set up, it offers to change the settings (the
current ones preselected, only the differences reviewed), show the login link, or remove grovy.

Without a terminal, or with -y, every question takes its recommended answer: a trunk on this computer only, the
port it had or 7333 (or the next free one), no HTTPS.
"""
import argparse
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import time

from . import claude, client, cleanup, core, graft, install, qr, tls, tui

UNIT = os.path.expanduser("~/.config/systemd/user/grovy.service")


# --- what this computer has ---------------------------------------------------------------------------------------

def addresses():
    """[(kind, address)] for this machine's IPv4 addresses: 'vpn' (the 100.64/10 range mesh VPNs use), 'lan'
    (private), 'public'."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        out = []
    got = []
    for a in out:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        kind = "vpn" if ip in ipaddress.ip_network("100.64.0.0/10") else "lan" if ip.is_private else "public"
        got.append((kind, a))
    order = {"vpn": 0, "lan": 1, "public": 2}
    return sorted(got, key=lambda x: order[x[0]])


def daemon_status():
    try:
        return client.sock_request({"type": "status"}, timeout=1.0) or {}
    except (OSError, ValueError):
        return {}


def have_systemd():
    try:
        return bool(shutil.which("systemctl")) and subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _systemctl(*args):
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"systemctl --user {' '.join(args)}: {r.stderr.strip() or 'failed'}")
    return r.stdout


def _linger_on():
    try:
        return subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"],
                              capture_output=True, text=True, timeout=10).stdout.strip() == "Linger=yes"
    except (OSError, subprocess.SubprocessError):
        return False


def current(rec):
    """The answers to preselect: the record, or for an install older than the record (or a unit edited by hand), what
    its service file says. Without either, nothing: the defaults apply."""
    rec = dict(rec)
    try:
        with open(UNIT) as f:
            unit = f.read()
    except OSError:
        return rec
    env = dict(re.findall(r"^Environment=(GROVY_[A-Z_]+)=(.*)$", unit, re.M))
    if "GROVY_BIND" in env and not rec.get("bind"):
        rec["bind"] = env["GROVY_BIND"]
    if env.get("GROVY_PORT", "").isdigit() and not rec.get("port"):
        rec["port"] = int(env["GROVY_PORT"])
    rec.setdefault("terminal", env.get("GROVY_TERMINAL") == "on")
    if "https" not in rec:   # HTTPS is on when there is a certificate and the unit doesn't turn it off
        rec["https"] = os.path.exists(tls.own_cert()) and env.get("GROVY_TLS") != "off"
    rec.setdefault("role", "graft" if os.path.exists(graft.UNIT) else "trunk")
    return rec


# --- the questions ------------------------------------------------------------------------------------------------

def welcome():
    tui.tree(["grovy", "Claude Code sessions in tmux,", "followed and driven from a browser.",
              "Close the laptop: they keep going."])
    tui.say()
    tui.say("Setup will ask a few questions, show you everything it will change, and change nothing until you say "
            "yes.")
    tui.note("It can link the grovy command, run grovy as a service that starts at boot, and keep your own files in "
             f"{tui.short(core.config_dir())}. grovy cleanup undoes all of it.")


def checks():
    """Python, tmux and claude, before anything is asked. Exits with what to install if one is missing."""
    bad = []
    if sys.version_info >= (3, 11):
        tui.ok(f"python {sys.version.split()[0]}")
    else:
        bad.append(f"python 3.11 or newer (this is {sys.version.split()[0]})")
    if shutil.which("tmux"):
        tui.ok("tmux")
    else:
        bad.append("tmux (e.g. sudo apt install tmux, brew install tmux)")
    c = shutil.which(claude.claude_cmd())
    if c:
        tui.ok(f"claude ({c})")
    else:
        bad.append(f"claude: {claude.claude_cmd()!r} is not on PATH (install Claude Code, or set GROVY_CLAUDE)")
    for b in bad:
        tui.fail("missing: " + b)
    if bad:
        core.die("install what's missing, then run grovy setup again")


def ask_role(rec):
    return tui.select("How will you use this computer?", [
        {"label": "Trunk", "value": "trunk", "hint": "(most people)", "detail": ["A new grovy, opened from a browser."]},
        {"label": "Graft", "value": "graft", "detail": ["Joins this computer to a trunk you already set up on another "
                                                        "machine. Needs ssh to it."]},
    ], default=1 if rec.get("role") == "graft" else 0)


def ask_bind(rec):
    opts = [{"label": "This computer only", "value": "127.0.0.1", "hint": "127.0.0.1  ⚠ other devices can't open it"}]
    names = {"vpn": "Your VPN", "lan": "Local network", "public": "Public address"}
    for kind, a in addresses():
        opts.append({"label": names[kind], "value": a, "hint": a})
    opts.append({"label": "Everywhere", "value": "0.0.0.0", "hint": "0.0.0.0  every network"})
    values = [o["value"] for o in opts]
    if rec.get("bind") and rec["bind"] not in values:   # e.g. a VPN that is down right now: keep it, don't fall back
        opts.insert(1, {"label": "Current address", "value": rec["bind"], "hint": f"{rec['bind']}  (not up right now)"})
        values = [o["value"] for o in opts]
    default = values.index(rec["bind"]) if rec.get("bind") in values else 0
    return tui.select("Who can reach grovy?", opts, default=default)


def ask_port(bind, rec, own_port):
    """The port, checked: one something else already listens on is refused once (typing it again uses it anyway,
    for this run only). Our own running daemon's port is never 'taken'."""
    want = rec.get("port") or core.env_int("PORT", 7333)
    probe = bind   # 0.0.0.0 also catches a listener on any one address, as the daemon's own bind would

    def busy(p):
        return p != own_port and install._port_busy(probe, p)

    default = want if not busy(want) else install._free_port(probe, want + 1)
    if not tui.interactive():
        return default
    if default != want:
        tui.warn(f"port {want} is already in use (maybe another user's grovy): suggesting {default}")
    last = [None]

    def check(v):
        if not v.isdigit() or not 1 <= int(v) <= 65535:
            return f"{v!r} is not a port number (1-65535)"
        p = int(v)
        if busy(p) and p != last[0]:
            last[0] = p
            return f"port {p} is already in use; type it again to use it anyway, or press enter for {default}"
        return None
    return int(tui.text("Port", str(default), check))


def ask_https(bind, rec):
    if bind == "127.0.0.1":
        return False
    return tui.select("Use HTTPS?", [
        {"label": "No", "value": False, "hint": "plain HTTP"},
        {"label": "Yes", "value": True, "hint": "needed for notifications; you install a certificate on each device"},
    ], default=1 if rec.get("https") else 0)


def cert_names(bind):
    """What the certificate is for: the address grovy listens on (every address for 0.0.0.0) and the host name."""
    ips = [a for _, a in addresses()] if bind == "0.0.0.0" else [bind]
    return ips + tls.default_names()


# --- the changes --------------------------------------------------------------------------------------------------

def plan_trunk(bind, port, https, systemd, rec, role="trunk", terminal=False):
    """[(label, action)] for what has to change; empty if everything is already so. A graft's own grovy is planned
    the same way (on this computer only, without HTTPS), with role="graft"."""
    steps = []
    bindir = os.path.expanduser("~/.local/bin")
    link, target = os.path.join(bindir, "grovy"), os.path.join(core.HERE, "grovy")
    if os.path.realpath(link) == os.path.realpath(target):
        pass
    elif os.path.lexists(link):
        tui.warn(f"{link} points to another grovy ({os.path.realpath(link)}): left alone; remove it to use this one")
    else:
        def do_link():
            os.makedirs(bindir, exist_ok=True)
            os.symlink(target, link)
            install.update_record(link=link)
        steps.append((f"link {tui.short(link)}", do_link))
    missing = [n for n in ("custom.css", "custom.js") if not os.path.exists(os.path.join(core.config_dir(), n))]
    if missing or not os.path.exists(core.token_path()):
        def do_files():
            core.load_token(create=True)
            os.makedirs(core.config_dir(), exist_ok=True)
            for n in missing:
                shutil.copyfile(os.path.join(core.HERE, "examples", n), os.path.join(core.config_dir(), n))
        steps.append((f"your grovy home {tui.short(core.config_dir())} (custom.css, custom.js) and a login token", do_files))
    if https:
        names = cert_names(bind)
        if not names:
            core.die("HTTPS needs an address or host name for the certificate, and this computer has neither: pick a "
                     "specific address, or set it up with grovy tls init --name NAME")
        have = set(tls.cert_names(tls.own_cert())) if os.path.exists(tls.own_cert()) else set()
        if not set(names) <= have:
            steps.append((f"an HTTPS certificate for {', '.join(names)}", lambda: tls.init(names)))
    old = {k: rec.get(k) for k in ("bind", "port", "https")}
    was = f" (was {old['bind']}:{old['port']})" if old["bind"] and (old["bind"], old["port"]) != (bind, port) else ""
    if systemd:
        unit = install.service_unit(bind, port, tls_off=not https, terminal=terminal)
        current = open(UNIT).read() if os.path.exists(UNIT) else None
        if current != unit:
            def do_unit():
                os.makedirs(os.path.dirname(UNIT), exist_ok=True)
                if current:
                    shutil.copyfile(UNIT, UNIT + ".bak")
                with open(UNIT, "w") as f:
                    f.write(unit)
                install.update_record(services=["grovy"])
            label = f"grovy service, listening on {bind}:{port}" + (" with HTTPS" if https else "") + was
            steps.append((label + ", starts at boot", do_unit))
        st = daemon_status()
        active = subprocess.run(["systemctl", "--user", "is-active", "grovy"], capture_output=True, text=True,
                                timeout=10).stdout.strip() == "active"
        if st and not active:
            steps.append(("enable the service for the next boot (a grovy daemon runs outside it now: stop it, then "
                          "systemctl --user start grovy)", lambda: (_systemctl("daemon-reload"),
                                                                     _systemctl("enable", "grovy"))))
        elif current != unit or not active:
            steps.append(("start grovy (running Claude sessions are not touched)",
                          lambda: (_systemctl("daemon-reload"), _systemctl("enable", "grovy"),
                                   _systemctl("restart", "grovy"))))
        if not _linger_on():
            def do_linger():
                r = subprocess.run(["loginctl", "enable-linger"], capture_output=True, text=True, timeout=30)
                if r.returncode:   # not worth stopping for: grovy then starts when you log in
                    tui.warn(f"linger: {r.stderr.strip() or 'loginctl enable-linger failed'} (grovy starts when you "
                             "log in instead)")
                    return
                install.update_record(linger=True)
            steps.append(("keep running after you log out (linger)", do_linger))
    g = rec.get("graft") if isinstance(rec.get("graft"), dict) and rec["graft"].get("trunk") else graft.from_unit()
    if role == "trunk" and (g.get("trunk") or os.path.exists(graft.UNIT)):
        def do_leave():
            graft.leave(g, "now a trunk")
            install.update_record(graft=None)
        steps.append((f"stop the tunnel to {g.get('trunk') or 'the trunk'} and take this computer off it", do_leave))
    if (old["bind"], old["port"], bool(old["https"]), bool(rec.get("terminal"))) != (bind, port, https, terminal) or \
            rec.get("role") != role:
        steps.append((None,   # not shown: grovy's own note of the answers, for cleanup and the next setup
                      lambda: install.update_record(role=role, bind=bind, port=port, https=https, terminal=terminal,
                                                    **({"home": core.home_env()} if core.home_env() else {}))))
    return steps


def review_and_apply(title, steps, dry):
    tui.say()
    if not any(label for label, _ in steps):
        if not dry:
            for _, action in steps:   # only grovy's own record: nothing on the computer changes
                action()
        tui.ok("Nothing to change: it's already set up like that.")
        return not dry
    tui.say(tui.paint(title, "bold"))
    for label, _ in steps:
        if label:
            tui.say(f"  • {label}")
    if dry:
        tui.note("(dry run: nothing changed)")
        return False
    if not tui.AUTO and not tui.interactive():
        tui.note("(no terminal to ask: nothing changed; run it in a terminal, or with -y to apply this)")
        return False
    if not tui.confirm("Apply?", True):
        tui.say("Nothing changed.")
        return False
    for label, action in steps:
        try:
            action()
        except Exception as e:   # stop at the first failure: later steps build on earlier ones
            tui.fail(f"{label or 'saving the answers'}: {e}")
            tui.say("Stopped there. Fix that and run grovy setup again, or undo what was done with grovy cleanup.")
            sys.exit(1)
        if label:
            tui.ok(label)
    return True


# --- the end ------------------------------------------------------------------------------------------------------

def finish_trunk(bind, port, systemd):
    if not systemd:
        tui.say()
        tui.warn("no systemd user session here: start grovy yourself, e.g. in tmux:")
        tui.say(f"    tmux new -d -s grovyd 'GROVY_BIND={bind} GROVY_PORT={port} {os.path.join(core.HERE, 'grovy')} serve'")
    for _ in range(50):
        if daemon_status():
            break
        time.sleep(0.2)
    else:
        tui.say()
        tui.say("grovy isn't answering yet; once it is, grovy url prints the login link.")
        return
    try:
        links, bases, tok, ours = install.login_links()
    except (OSError, ValueError):
        tui.say("grovy isn't answering yet; once it is, grovy url prints the login link.")
        return
    tui.say()
    live = daemon_status().get("url", "")
    if live.rstrip("/") not in (f"http://{bind}:{port}", f"https://{bind}:{port}"):   # e.g. a daemon outside the service
        tui.warn(f"the grovy running now listens on {live or 'something else'}, not {bind}:{port}: stop it, then "
                 "systemctl --user start grovy (sessions keep running)")
        next_steps()
        return
    if all(install._is_local(b) for b in bases):
        tui.say(tui.paint("grovy is running on this computer only.", "bold"))
        tui.say(f"  {links[0]}")
        opener = shutil.which("xdg-open") or (shutil.which("open") if sys.platform == "darwin" else None)
        graphical = sys.platform == "darwin" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        if opener and graphical and tui.interactive() and tui.confirm("Open it in your browser now?", True):
            subprocess.Popen([opener, links[0]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tui.note("To open it from other devices too, run grovy setup again and pick another address.")
    else:
        link = next((lk for lk, b in zip(links, bases) if not install._is_local(b)), links[0])
        tui.say(tui.paint("Scan it, or open the link, once on each device: it logs that device in.", "bold"))
        if ours:
            tui.note("With HTTPS: install the certificate on that device first (grovy url says which file).")
        try:
            tui.say(qr.render(link, quiet=2))
        except ValueError:
            pass   # too long for the small encoder: the link below is enough
        tui.say(f"  {link}")
        if len(links) > 1:
            tui.note("  (grovy url lists every address)")
        install._login_warnings(bases, tok, ours)
    next_steps()


def next_steps():
    tui.say()
    tui.say(tui.paint("Next", "bold"))
    tui.say("  • start a session: ＋ in the browser, or grovy new NAME FOLDER")
    tui.say("  • check everything: grovy doctor")
    tui.say("  • undo all of it: grovy cleanup")


# --- the command --------------------------------------------------------------------------------------------------

def cmd_setup(a):
    tui.AUTO = bool(a.yes)
    try:
        return _setup(a)
    except KeyboardInterrupt:
        tui.say()
        tui.say("Stopped. What was already applied stays (grovy setup again finishes it, grovy cleanup undoes it).")
        return 130
    finally:
        tui.AUTO = False


def _setup(a):
    rec = current(install.read_record())
    set_up = bool(rec.get("role")) or os.path.exists(UNIT)
    if a.port is not None and not 1 <= a.port <= 65535:
        core.die("--port: 1-65535")
    flags = a.bind or a.port or a.role or a.trunk or a.name or a.https is not None or a.no_service
    welcome()
    if set_up and not flags and tui.interactive():
        tui.say()
        where = f"{rec.get('bind')}:{rec.get('port')}" if rec.get("bind") else "settings unknown"
        if rec.get("role") == "graft" and (rec.get("graft") or {}).get("trunk"):
            where = f"on {rec['graft']['trunk']} as {rec['graft'].get('name')}"
        what = tui.select(f"grovy is set up here ({rec.get('role', 'trunk')}, {where}"
                          f"{', HTTPS' if rec.get('https') else ''}). What now?", [
            {"label": "Change settings", "value": "change", "hint": "your current answers are preselected"},
            {"label": "Show the login link", "value": "link"},
            {"label": "Remove grovy", "value": "remove", "hint": "same as grovy cleanup"},
        ])
        if what == "link":
            finish_trunk(rec.get("bind", "127.0.0.1"), rec.get("port", 7333), True)
            return 0
        if what == "remove":
            a.end_sessions = a.remove_home = False
            return cleanup.cmd_cleanup(a)
    total = 4
    tui.step(1, total, "Checking what grovy needs")
    checks()
    tui.step(2, total, "Role")
    role = a.role or ask_role(rec)
    if role == "graft":
        return _setup_graft(a, rec, total)
    tui.step(3, total, "Network")
    bind = a.bind or ask_bind(rec)
    try:
        if ipaddress.ip_address(bind).version != 4:
            core.die("grovy listens on IPv4 addresses only")
    except ValueError:
        core.die(f"{bind!r} is not an IP address")
    port = a.port if a.port is not None else ask_port(bind, rec, _own_port())
    https = a.https if a.https is not None else ask_https(bind, rec)
    systemd = not a.no_service and have_systemd()
    tui.step(4, total, "Review")
    steps = plan_trunk(bind, port, https, systemd, rec)
    if not review_and_apply("Ready to set up a trunk:", steps, a.dry_run):
        return 0 if a.dry_run or tui.interactive() else 2
    if os.path.expanduser("~/.local/bin") not in os.environ.get("PATH", "").split(os.pathsep):
        tui.warn("~/.local/bin is not on your PATH; add it in your shell profile: export PATH=\"$HOME/.local/bin:$PATH\"")
    finish_trunk(bind, port, systemd)
    return 0


def _own_port():
    m = re.match(r"https?://.+:(\d+)", daemon_status().get("url", ""))
    return int(m.group(1)) if m else None


def _setup_graft(a, rec, total):
    if not shutil.which("ssh") or not shutil.which("ssh-keygen"):
        core.die("a graft needs ssh and ssh-keygen (OpenSSH client): install them first")
    tui.step(3, total, "Trunk")
    trunk, taken = graft.ask_trunk(rec, a.trunk)
    name = graft.ask_name(rec, taken, a.name)
    port = a.port if a.port is not None else ask_port("127.0.0.1", rec, _own_port())
    term = tui.select(f"Let {trunk} open a terminal on this computer's sessions (grovy attach {name}:SESSION)?", [
        {"label": "Yes", "value": True, "hint": f"a full terminal: whoever controls {trunk} can then run commands here"},
        {"label": "No", "value": False, "hint": "the trunk can still list, start, stop and message them"},
    ], default=0 if rec.get("terminal", True) else 1)
    systemd = not a.no_service and have_systemd()
    tui.step(4, total, "Review")
    steps = plan_trunk("127.0.0.1", port, False, systemd, rec, role="graft", terminal=term) + \
        graft.plan(trunk, name, port, systemd, rec)
    if not review_and_apply(f"Ready to graft this computer onto {trunk} as {name}:", steps, a.dry_run):
        return 0 if a.dry_run or tui.interactive() else 2
    tui.say()
    if not systemd:
        tui.warn("no systemd user session here: start grovy and the tunnel yourself, e.g. in tmux:")
        tui.say(f"    tmux new -d -s grovyd '{'GROVY_TERMINAL=on ' if term else ''}GROVY_PORT={port} "
                f"{os.path.join(core.HERE, 'grovy')} serve'")
        tui.say(f"    {graft.tunnel_command(trunk, name, port)}")
    tui.note(f"  waiting for {trunk} to see this computer…")
    if graft.wait_online(trunk, name):
        tui.ok(f"Connected to {trunk}. This computer's sessions now appear there as {name}:…")
    else:
        tui.warn(f"{trunk} doesn't see this computer yet. Check the tunnel: journalctl --user -u grovy-tunnel "
                 f"(and on {trunk}: grovy peer ls)")
    next_steps()
    return 0


def add_parser(sub):
    s = sub.add_parser("setup", help="guided install: the grovy command, the service, the login link (grovy cleanup undoes it)")
    s.add_argument("--role", choices=("trunk", "graft"), help="trunk: a grovy of its own (default); graft: join a trunk")
    s.add_argument("--trunk", help="graft: the trunk's ssh address (user@host, or a name from ~/.ssh/config)")
    s.add_argument("--name", help="graft: this computer's name on the trunk (default: its host name)")
    s.add_argument("--bind", help="address to listen on (asked if omitted)")
    s.add_argument("--port", type=int, help="TCP port to listen on (asked if omitted)")
    s.add_argument("--https", action=argparse.BooleanOptionalAction, default=None, help="serve HTTPS (--no-https: plain HTTP)")
    s.add_argument("--no-service", action="store_true", help="don't install the systemd service")
    s.add_argument("-y", "--yes", action="store_true", help="don't ask: take the recommended answers")
    s.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    s.set_defaults(fn=cmd_setup)

