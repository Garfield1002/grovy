"""`grovy doctor`, `grovy url`, `grovy tls init`, and what setup (setupflow.py) and cleanup share: the service unit,
the setup record, the login links, port checks."""
import os
import re
import shutil
import socket
import subprocess
import sys

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


def login_links():
    """(full login URLs, the bases they came from, token, whether grovy's own certificate is served); raises
    OSError/ValueError if grovyd is silent."""
    st = client.sock_request({"type": "status"}) or {}
    tok = core.load_token()
    ours = st.get("tls") == tls.own_cert()
    bases = lan_bases(st.get("url", ""))
    if ours:   # the certificate is only valid for its own names: links by any other address would fail
        port = bases[0].rsplit(":", 1)[1]
        named = [f"https://{f'[{n}]' if ':' in n else n}:{port}" for n in tls.cert_names(tls.own_cert())]
        bases = named or bases
    return [base + "/" + (f"?token={tok}" if tok else "") for base in bases], bases, tok, ours


def _is_local(base):
    return bool(re.match(r"https?://(127\.|localhost|\[::1\])", base))


def _login_warnings(bases, tok, ours=False):
    """The notes cmd_url and setup both print: the certificate to install first, a warning if the daemon is
    local-only or has no token."""
    if ours and tls.cert_der():
        print(f"New device? Install {tls.public_cert()} on it first (copy the file over yourself; `grovy tls init` "
              f"says how). Its SHA-256 fingerprint is\n  {tls.fingerprint(tls.cert_der())}", file=sys.stderr)
    if all(_is_local(b) for b in bases):
        print("note: the daemon only listens on this computer, so these links won't open on a phone.\n"
              "Restart it with --bind 0.0.0.0 (or your VPN address), or set GROVY_BIND.", file=sys.stderr)
    if not tok:
        print("(no token: anyone who can reach these addresses can use grovy)", file=sys.stderr)


def cmd_url(_):
    """Print the phone login link(s): open one once per device; it sets a cookie."""
    try:
        links, bases, tok, ours = login_links()
    except (OSError, ValueError):
        core.die("grovyd not running (grovy serve)")
    for link in links:
        print(link)
    _login_warnings(bases, tok, ours)
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


def state_path():
    """The state folder, without making it (core.state_dir() does): cleanup and dry runs only look."""
    return core.env("STATE_DIR") or os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                                                 "grovy")


def record_path():
    """What `grovy setup` changed on this computer, so `grovy cleanup` can undo exactly that."""
    return os.path.join(state_path(), "setup.json")


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
    os.makedirs(state_path(), mode=0o700, exist_ok=True)
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
{tls}{dirs}# The PATH of the shell that ran setup: claude (and the Claude sessions' Bash tool) need it.
Environment="PATH={path}"
Restart=on-failure
RestartSec=2
# Essential: if this daemon starts the tmux server (first session from the phone), tmux is in this
# unit's cgroup, and the default KillMode would kill tmux and every Claude session on restart or crash.
KillMode=process

[Install]
WantedBy=default.target
"""


def service_unit(bind, port, path=None, tls_off=False):
    path = path if path is not None else os.environ.get("PATH", "/usr/bin:/bin")
    path = ":".join(dict.fromkeys(p for p in path.split(":") if p))   # de-duplicated, order kept
    q = lambda v: str(v).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')   # noqa: E731  systemd: % specifiers, quotes
    dirs = "".join(f'Environment="{k}={q(v)}"\n' for k, v in (("GROVY_HOME", core.home_env()),
                                                               ("GROVY_STATE_DIR", core.env("STATE_DIR"))) if v)
    return SERVICE_UNIT.format(exe=q(os.path.join(core.HERE, "grovy")), bind=q(bind), port=int(port), path=q(path),
                               tls="Environment=GROVY_TLS=off\n" if tls_off else "", dirs=dirs)


def _port_busy(bind, port):
    """True if something already holds (bind, port). http.server sets SO_REUSEADDR, so we do too: a socket in
    TIME_WAIT doesn't count as busy, only a live listener does — the same condition the daemon would hit."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((bind, port))
        return False
    except OSError as e:
        import errno
        return e.errno != errno.EADDRNOTAVAIL   # the address isn't up right now (a VPN that is down): can't tell
    finally:
        s.close()


def _free_port(bind, start):
    """The first free port at or after `start` (wrapping is pointless; ports above it are plenty)."""
    for p in range(start, 65536):
        if not _port_busy(bind, p):
            return p
    return start


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
