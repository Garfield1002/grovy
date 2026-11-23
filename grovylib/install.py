"""`grovy setup`, `grovy doctor`, `grovy url`: installing and checking grovy."""
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from . import claude, client, core, sessions, tls, tmuxctl


def lan_bases(url):
    """SCHEME://HOST:PORT for each address the daemon is reachable on (all IPv4 addresses when bound to 0.0.0.0)."""
    m = re.match(r"(https?)://(.+):(\d+)/?", url or "")
    scheme, host, port = m.groups() if m else ("http", "127.0.0.1", "7333")
    hosts = [host]
    if host in ("0.0.0.0", "::"):
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
        hosts = [h for h in out if ":" not in h] or ["127.0.0.1"]
    return [f"{scheme}://{h}:{port}" for h in hosts]


def cmd_url(_):
    """Print the phone login link(s): open one once per device; it sets a cookie."""
    try:
        st = client.sock_request({"type": "status"}) or {}
    except (OSError, ValueError):
        core.die("grovyd not running (grovy serve)")
    url = st.get("url", "")
    tok = core.load_token()
    ours = st.get("tls") == tls.own_cert()
    bases = lan_bases(url)
    local_only = all(re.match(r"https?://(127\.|localhost|\[::1\])", b) for b in bases)   # where it listens
    if ours:   # the certificate is only valid for its own names: links by any other address would fail
        port = bases[0].rsplit(":", 1)[1]
        named = [f"https://{f'[{n}]' if ':' in n else n}:{port}" for n in tls.cert_names(tls.own_cert())]
        bases = named or bases
    for base in bases:
        print(base + "/" + (f"?token={tok}" if tok else ""))
    if ours and tls.cert_der():
        print(f"New device? Install {tls.public_cert()} on it first (copy the file over yourself; `grovy tls init` "
              f"says how). Its SHA-256 fingerprint is\n  {tls.fingerprint(tls.cert_der())}", file=sys.stderr)
    if local_only:
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
        paths = tls.cert_paths()
        if paths:
            tls.context(*paths)
            soon = tls.expires_soon(paths[0])
            check("https certificate", not soon, paths[0] + (" expires within 30 days: rerun `grovy tls init` (or renew "
                                                             "your own)" if soon else ""))
            if paths == (tls.own_cert(), None):
                ok = tls.matches() is not False
                check("https certificate matches grovy.crt", ok,
                      "" if ok else "devices that install grovy.crt won't trust it; rerun `grovy tls init`")
        else:
            print("[info] https: off (grovy tls init)")
    except (OSError, ValueError) as e:   # ssl.SSLError is an OSError
        check("https certificate", False, str(e))
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


def record_path():
    """What `grovy setup` changed on this computer, so `grovy cleanup` can undo exactly that."""
    return os.path.join(core.state_dir(), "setup.json")


def read_record():
    got = core.read_json(record_path(), {})
    return got if isinstance(got, dict) else {}


def update_record(**changes):
    """Merge into the setup record. A flag that says setup did something (linger) stays set across reruns: a later
    setup that finds linger already on mustn't make cleanup forget who turned it on."""
    rec = read_record()
    for k, v in changes.items():
        if k == "linger":
            v = bool(v or rec.get("linger"))
        elif k == "services":
            v = sorted(set(rec.get("services", [])) | set(v))
        rec[k] = v
    rec["version"] = 1
    core.atomic_write_json(record_path(), rec)


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
    vpn = [a for a in addrs if ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10")]   # mesh-VPN range
    choices = [("127.0.0.1", "this computer only (no phone access)"),
               ("0.0.0.0", "every network this computer is on")] + \
              [(a, "VPN address" if a in vpn else "this address only") for a in addrs]
    default = vpn[0] if vpn else "0.0.0.0"
    if assume_yes or not sys.stdin.isatty():
        return default
    print("Where should the phone reach grovy? Only expose it on networks you trust (it is plain HTTP unless you "
          "run `grovy tls init`).")
    for i, (a, what) in enumerate(choices, 1):
        print(f"  {i}) {a:<16} {what}")
    got = _ask("Address or number", default)
    if got.isdigit() and 1 <= int(got) <= len(choices):
        return choices[int(got) - 1][0]
    ipaddress.ip_address(got)   # ValueError on garbage
    return got


def _port_busy(bind, port):
    """True if something already holds (bind, port). http.server sets SO_REUSEADDR, so we do too: a socket in
    TIME_WAIT doesn't count as busy, only a live listener does — the same condition the daemon would hit."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((bind, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _free_port(bind, start):
    """The first free port at or after `start` (wrapping is pointless; ports above it are plenty)."""
    for p in range(start, 65536):
        if not _port_busy(bind, p):
            return p
    return start


def _pick_port(bind, assume_yes, own_port):
    """Ask which TCP port to listen on, warning when it's already taken by something other than our own daemon
    (another user's grovy bound to 0.0.0.0 is the common one) and suggesting a free one. `own_port` is the port
    our running daemon already uses, so re-running setup doesn't flag it. Typing the taken port again uses it
    anyway: that consent is in-session only, never stored. Non-interactive: the env/default port, no prompt."""
    default, last = core.env_int("PORT", 7333), None
    if assume_yes or not sys.stdin.isatty():
        return default
    while True:
        got = _ask("TCP port for the phone to reach grovy", default)
        try:
            port = int(got)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            print(f"  {got!r} is not a port number (1-65535).")
            continue
        if port == own_port or port == last or not _port_busy(bind, port):
            return port
        last, default = port, _free_port(bind, port + 1)
        print(f"  port {port} on {bind} is already in use (maybe another user's grovy, bound to 0.0.0.0).\n"
              f"  press enter for {default}, or type {port} again to use it anyway.")


INSTALL_HELP = """
Copy that file to each device yourself (cable, AirDrop, USB stick; not over the network you are protecting
against), install it, and check that the device shows the same SHA-256 fingerprint. A device that had the previous
certificate installs this one again.
  Firefox:  Settings > Privacy & Security > Certificates > View Certificates > Authorities > Import > the file,
            and tick "Trust this CA to identify websites".
  Linux, whole system (Fedora; Chrome, curl, and Fedora's Firefox):  sudo trust anchor --store {file}
            (double-clicking the file in GNOME can't import it)
  iPhone:   open the file (Files or AirDrop), then Settings > General > VPN & Device Management > the grovy
            profile > Install; then Settings > General > About > Certificate Trust Settings > turn it on.
  Android:  Settings > Security > Encryption & credentials > Install a certificate > CA certificate > the file.
            (Firefox for Android ignores it unless "Use third party CA certificates" is on in its secret settings.)
It can vouch for nothing but this server: its own key was destroyed once it had signed the server's certificate."""


def cmd_tls(a):
    """`grovy tls init`: a certificate for the names grovy is opened by, so the daemon serves HTTPS."""
    names = a.name or tls.default_names()
    if not names:
        core.die("this machine has no full host name: give the name or address the phone uses, with --name")
    try:
        ips, names = tls.init(names)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
        core.die(str(e))
    print(f"HTTPS certificate for: {', '.join([str(i) for i in sorted(ips, key=str)] + sorted(names))}")
    print("  (valid only for these, and subdomains of the names)")
    host = socket.gethostname().lower()
    known = {host, host.split(".")[0] + ".local", socket.getfqdn().lower(), "localhost"} | {
        h.strip().lower() for h in core.env("HOSTS", "").split(",")}
    extra = sorted(n for n in names if n not in known)
    if extra:
        print(f"To open grovy by {', '.join(extra)}, also add it to GROVY_HOSTS in the daemon's environment.")
    print(f"\nThe certificate to install on each device: {tls.public_cert()}\n  SHA-256 {tls.fingerprint(tls.cert_der())}")
    try:
        st = client.sock_request({"type": "status"}, timeout=1.0) or {}
    except (OSError, ValueError):
        st = None
    restart = "systemctl --user restart grovy, or stop and start `grovy serve`; sessions keep running"
    if st is None:
        print("Start the daemon (grovy serve, or systemctl --user start grovy): it will use HTTPS.")
    elif st.get("tls") == tls.own_cert():
        print("The daemon picks up the new certificate within 10 seconds.")
    elif st.get("tls"):
        print(f"The daemon serves another certificate ({st['tls']}, GROVY_TLS_CERT): it ignores this one.")
    else:
        print(f"Restart the daemon to switch it to HTTPS ({restart}).")
    print(INSTALL_HELP.format(file=tls.public_cert()))
    return 0


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
            update_record(link=link)
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
    running, own_port = False, None
    try:
        st = client.sock_request({"type": "status"}, timeout=1.0) or {}
        running = bool(st)
        m = re.match(r"http://.+:(\d+)", st.get("url", ""))
        own_port = int(m.group(1)) if m else None
    except (OSError, ValueError):
        pass
    port = a.port if a.port is not None else _pick_port(bind, a.yes, own_port)
    if not dry:
        update_record(role="trunk", bind=bind, port=port, **({"home": core.home_env()} if core.home_env() else {}))
    unit = service_unit(bind, port)
    systemd = not a.no_service and shutil.which("systemctl") and subprocess.run(
        ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    if a.no_service or not systemd:
        if not a.no_service:
            print("no systemd user session here: start the daemon yourself, e.g. in tmux:")
        print(f"  tmux new -d -s grovyd 'GROVY_BIND={bind} GROVY_PORT={port} {target} serve'")
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
                update_record(services=["grovy"])
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
            if not dry:
                if subprocess.run(["loginctl", "enable-linger"], capture_output=True, timeout=30).returncode:
                    print("note: `loginctl enable-linger` failed; without it the daemon starts when you log in")
                else:
                    update_record(linger=True)
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
          "`grovy doctor`.\nTo undo all of this: grovy cleanup.")
    return 0
