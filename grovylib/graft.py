"""The graft half of `grovy setup` (and its undo in `grovy cleanup`): join this computer to a trunk over SSH.

A graft runs its own grovy on this computer only and keeps a reverse SSH tunnel to the trunk (`grovy peer tunnel`,
grovylib/peers.py), so the trunk shows its sessions as NAME:... It opens no port.

The tunnel logs in with a key of its own, `~/.ssh/grovy_tunnel` (no passphrase: a service has no agent), which setup
installs on the trunk as a *restricted* authorized_keys entry:

    restrict,port-forwarding,permitopen="127.0.0.1:9",command="rm -f SOCK; echo SOCK" ssh-ed25519 KEY grovy-tunnel:NAME

so the key can't open a shell, run a command of its choosing, or forward to the trunk's ports (permitopen names one
nothing listens on). Its forced command is exactly the tunnel's preparation for its one socket (remove a stale one,
print the path), so the tunnel works unchanged whatever the trunk's sshd settings. It can still bind Unix sockets
the account can write (OpenSSH doesn't limit -R to a path), so the trunk must trust this computer: see DESIGN.

Setup's own logins to the trunk use your usual ssh (a password prompt is fine: it's your terminal), multiplexed over
one connection (ControlMaster), so you're asked once. One script there adds the key line (replacing an older one for
the same key), makes the socket's folder, and runs `grovy peer add NAME` with this grovy's token on stdin.
"""
import atexit
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time

from . import core, install, peers, tui

KEY = os.path.expanduser("~/.ssh/grovy_tunnel")
UNIT = os.path.expanduser("~/.config/systemd/user/grovy-tunnel.service")
SAFE_PATH = re.compile(r"/[A-Za-z0-9/._-]{1,%d}" % (peers.SOCK_MAX - 1))
PUBKEY_RE = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]{16,}")

TUNNEL_UNIT = """\
# Written by `grovy setup` (a graft); grovy cleanup removes it.
[Unit]
Description=grovy: show this computer's sessions on the trunk {trunk}
After=network-online.target grovy.service

[Service]
ExecStart={exe} peer tunnel {trunk} --name {name} --port {port} --identity {key}
Environment="PATH={path}"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def tunnel_unit(trunk, name, port, path=None):
    q = lambda v: str(v).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')   # noqa: E731
    path = path if path is not None else os.environ.get("PATH", "/usr/bin:/bin")
    return TUNNEL_UNIT.format(trunk=q(trunk), name=name, port=int(port), key=q(KEY), path=q(path),
                              exe=q(os.path.join(core.HERE, "grovy")))


# --- talking to the trunk with your own ssh ----------------------------------------------------------------------

_cm_dir = None


def _cm_opts():
    """One shared connection for all of setup's logins to the trunk: a password is asked once."""
    global _cm_dir
    if _cm_dir is None:
        _cm_dir = tempfile.mkdtemp(prefix="grovy-ssh-")   # short: a ControlPath is a Unix socket

        def close():
            for f in os.listdir(_cm_dir):
                subprocess.run(["ssh", "-o", f"ControlPath={_cm_dir}/{f}", "-O", "exit", "x"],
                               capture_output=True, timeout=10)
            shutil.rmtree(_cm_dir, ignore_errors=True)
        atexit.register(close)
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={_cm_dir}/cm", "-o", "ControlPersist=300",
            "-o", "ConnectTimeout=20"]


def clean(text):
    """Remote text made safe to print: no terminal escape sequences or other control characters."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", (text or "").strip())


def ssh(host, script, input=None, timeout=180, run=None):
    """Run a sh script on the trunk with your usual ssh login (it may ask for a password: ssh asks on the terminal
    itself). Returns the CompletedProcess."""
    run = run or subprocess.run
    argv = ["ssh", *_cm_opts(), "--", host, "sh -c " + shlex.quote(script)]
    if input is None:
        return run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    return run(argv, input=input, capture_output=True, text=True, timeout=timeout)


FIND_GROVY = ('g="$HOME/.local/bin/grovy"; [ -x "$g" ] || g="$(command -v grovy)" || g=""; '
              'if [ -z "$g" ]; then echo NOGROVY; exit 0; fi; ')


def probe(host, run=None, timeout=120):
    """(peer names already on the trunk, None) or (None, why it won't do). Checks ssh works and that the trunk has a
    grovy with peers."""
    if not peers.HOST_RE.fullmatch(host) or host.startswith("-"):
        return None, f"{host!r} isn't an ssh destination (user@host, or a name from ~/.ssh/config)"
    try:
        r = ssh(host, FIND_GROVY + '"$g" peer -h >/dev/null 2>&1 || { echo OLDGROVY; exit 0; }; echo OK; '
                '"$g" peer ls 2>/dev/null; true', run=run, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"ssh {host}: {e}"
    out = r.stdout or ""
    if r.returncode or not out.strip():
        return None, f"ssh {host} failed: {clean(r.stderr)[-300:] or f'exit {r.returncode}'}"
    if "NOGROVY" in out:
        return None, f"grovy isn't installed on {host}: set the trunk up first (grovy setup there)"
    if "OLDGROVY" in out:
        return None, f"the grovy on {host} is too old to accept grafts: update it (git pull there) and restart it"
    names = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and peers.valid_peer(parts[0]):
            names[parts[0]] = parts[2]
    return names, None


def ssh_hosts(path=None):
    """The Host names in ~/.ssh/config (no patterns)."""
    path = path or os.path.expanduser("~/.ssh/config")
    out = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].lower() == "host":
                    out += [h for h in parts[1:] if not re.search(r"[*?!]", h) and h not in out]
    except OSError:
        pass
    return out


def default_name():
    n = re.sub(r"[^a-z0-9_-]", "-", socket.gethostname().split(".")[0].lower()).strip("-_")[:16]
    return n if peers.valid_peer(n) else "pc"


# --- questions ----------------------------------------------------------------------------------------------------

def ask_trunk(rec, given=None):
    """The trunk's ssh destination, checked (ssh works, grovy with peers there). Returns (host, peer names)."""
    old = (rec.get("graft") or {}).get("trunk")
    while True:
        host = given
        if not host:
            hosts = ssh_hosts()
            opts = [{"label": h, "value": h} for h in hosts] + [{"label": "Type an address", "value": None,
                                                                 "hint": "user@host"}]
            default = hosts.index(old) if old in hosts else 0
            host = tui.select("Which machine is the trunk?", opts, default=default) if hosts else None
            if not host:
                host = tui.text("The trunk's ssh address (user@host)", old or "")
        if not host:
            core.die("no trunk given (--trunk HOST)")
        tui.note(f"  connecting to {host} (ssh may ask for your password there, once)…")
        names, why = probe(host)
        if names is not None:
            tui.ok(f"{host} has grovy" + (f" (grafts there: {', '.join(sorted(names))})" if names else ""))
            return host, names
        tui.fail(why)
        if given or not tui.interactive():
            core.die("can't use that trunk")
        given = None


def ask_name(rec, taken, given=None):
    old = (rec.get("graft") or {}).get("name")

    def check(v):
        if not peers.valid_peer(v):
            return "lowercase letters, digits, - and _ (at most 16), e.g. pc"
        return None
    name = given or tui.text("This computer's name on the trunk", old or default_name(), check)
    if check(name):
        core.die(f"name: {check(name)}")
    if name in taken and name != old:
        tui.warn(f"the trunk already has a graft named {name}: this computer replaces it (its tunnel key line is "
                 "removed there too)")
    return name


# --- the changes --------------------------------------------------------------------------------------------------

# Rewrites ~/.ssh/authorized_keys without the lines grovy owns for this key or this name, plus an optional new line.
# Careful because a mistake locks the user out: fields are compared exactly (awk on bytes, LC_ALL=C, so no line is
# dropped for not being valid UTF-8), the new file is made in ~/.ssh and only put in place if every other line is
# still there (line counts), and any failure leaves the file untouched. A symlinked file is written through.
EDIT_KEYS = r"""
edit_keys() {
  a="$HOME/.ssh/authorized_keys"; body="$1"; line="$2"; tag="$3"   # tag: also drop lines ending in it
  [ -n "$body" ] || { echo "no key received" >&2; return 5; }
  [ -f "$a" ] || { [ -n "$line" ] || return 0; : > "$a"; }
  t=$(mktemp "$HOME/.ssh/.authorized_keys.grovy.XXXXXX") || { echo "can't make a temporary file in ~/.ssh" >&2; return 6; }
  n=$(LC_ALL=C awk -v body="$body" -v tag="$tag" '
    { drop = (tag != "" && $NF == tag); for (i = 1; i <= NF; i++) if ($i == body) drop = 1 }
    drop { n++; next } { print } END { print n + 0 > "/dev/stderr" }' "$a" 2>&1 >"$t") || { rm -f "$t"; echo "awk failed" >&2; return 7; }
  if [ -n "$line" ]; then printf '%s\n' "$line" >> "$t" || { rm -f "$t"; return 8; }; fi
  total=$(LC_ALL=C awk 'END { print NR }' "$a"); kept=$(LC_ALL=C awk 'END { print NR }' "$t")
  want=$((total - n)); if [ -n "$line" ]; then want=$((want + 1)); fi
  [ "$kept" = "$want" ] || { rm -f "$t"; echo "authorized_keys not changed: lines don't add up ($kept, expected $want)" >&2; return 9; }
  if [ -L "$a" ]; then cat "$t" > "$a" && rm -f "$t"; else chmod 600 "$t" && mv -f "$t" "$a"; fi
}
"""

# Runs on the trunk. stdin: this grovy's token, then the tunnel's public key. Prints SOCKET=path.
ADD_SCRIPT = FIND_GROVY + EDIT_KEYS + r"""
set -e
read -r tok; read -r key
d="${XDG_STATE_HOME:-$HOME/.local/state}/grovy/peers"
mkdir -p "$d"; chmod 700 "$d"
s="$d/@NAME@.sock"
case "$s" in *[!A-Za-z0-9/._-]*) echo "the socket path $s has characters grovy won't put in authorized_keys" >&2; exit 4;; esac
umask 077
mkdir -p "$HOME/.ssh"
body=$(printf '%s\n' "$key" | cut -d' ' -f2)
edit_keys "$body" "$(printf 'restrict,port-forwarding,permitopen="127.0.0.1:9",permitlisten="127.0.0.1:9",command="rm -f %s; echo %s" %s grovy-tunnel:@NAME@' "$s" "$s" "$key")" "grovy-tunnel:@NAME@"
@RMOLD@
printf '%s\n' "$tok" | "$g" peer add @NAME@ --socket "$s" >/dev/null
printf 'SOCKET=%s\n' "$s"
"""

REMOVE_SCRIPT = FIND_GROVY + EDIT_KEYS + r"""
read -r key
body=$(printf '%s\n' "$key" | cut -d' ' -f2)
# Only this computer's key: another computer may hold the name now (it replaced this one). Without the key (deleted
# here), the name's line is taken instead.
if [ -n "$body" ]; then edit_keys "$body" "" "" || exit $?; else edit_keys grovy-no-key "" "grovy-tunnel:@NAME@" || exit $?; fi
"$g" peer rm @NAME@ >/dev/null 2>&1 || true
echo REMOVED
"""


def public_key():
    with open(KEY + ".pub") as f:
        pub = f.read().strip()
    m = PUBKEY_RE.match(pub)
    if not m:
        raise RuntimeError(f"{KEY}.pub doesn't look like an ssh public key")
    return m.group(0)


def plan(trunk, name, port, systemd, rec):
    """The graft's own steps, after its local grovy's (setupflow.plan_trunk with role=graft)."""
    steps = []
    old = rec.get("graft") or {}
    new_key = not os.path.exists(KEY)
    if new_key:
        def do_key():
            os.makedirs(os.path.dirname(KEY), mode=0o700, exist_ok=True)
            r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"grovy-tunnel:{name}", "-f", KEY],
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            if r.returncode:
                raise RuntimeError(r.stderr.strip() or "ssh-keygen failed")
            install.update_record(graft={**(install.read_record().get("graft") or {}), "key": KEY})
        steps.append((f"a key for the tunnel only, {tui.short(KEY)} (no passphrase)", do_key))
    if old.get("trunk") and old["trunk"] != trunk and old.get("name"):
        steps.append((f"on {old['trunk']}: remove this computer ({old['name']}), now grafted onto {trunk}",
                      lambda: leave(old, f"moved to {trunk}")))
    if new_key or (old.get("trunk"), old.get("name")) != (trunk, name):
        def do_trunk():
            rm_old = (f'"$g" peer rm {old["name"]} >/dev/null 2>&1 || true'
                      if old.get("trunk") == trunk and old.get("name") not in (None, name) else ":")
            script = ADD_SCRIPT.replace("@NAME@", name).replace("@RMOLD@", rm_old)
            tok = core.load_token()
            if not tok:
                raise RuntimeError("no login token here yet")
            r = ssh(trunk, script, input=f"{tok}\n{public_key()}\n")
            sock = next((ln[7:] for ln in (r.stdout or "").splitlines() if ln.startswith("SOCKET=")), None)
            if r.returncode or not sock:
                raise RuntimeError(clean(r.stderr)[-300:] or f"ssh exit {r.returncode}")
            if not SAFE_PATH.fullmatch(sock):
                raise RuntimeError(f"the trunk gave an unexpected socket path {clean(sock)!r}")
            install.update_record(graft={"trunk": trunk, "name": name, "socket": sock, "key": KEY})
        steps.append((f"on {trunk}: add this computer as {name}, with the tunnel key (restricted to the tunnel)",
                      do_trunk))
    if systemd:
        unit = tunnel_unit(trunk, name, port)
        current = open(UNIT).read() if os.path.exists(UNIT) else None
        if current != unit or steps:
            def do_unit():
                os.makedirs(os.path.dirname(UNIT), exist_ok=True)
                with open(UNIT, "w") as f:
                    f.write(unit)
                install.update_record(services=["grovy-tunnel"])
                for args in (["daemon-reload"], ["enable", "grovy-tunnel"], ["restart", "grovy-tunnel"]):
                    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
                    if r.returncode:
                        raise RuntimeError(f"systemctl --user {' '.join(args)}: {r.stderr.strip()}")
            steps.append((f"the tunnel to {trunk} as a service (grovy-tunnel), reconnecting by itself", do_unit))
    return steps


def leave(g, why):
    """Take this computer off the trunk it was grafted to (`g`, the record's graft entry): stop and remove the tunnel
    service, remove its line and peer entry there, delete the key. If the trunk can't be reached, say so and keep
    the record's entry, so that grovy cleanup can try again; the key stays then, as the trunk still accepts it."""
    if shutil.which("systemctl"):
        try:
            subprocess.run(["systemctl", "--user", "disable", "--now", "grovy-tunnel"], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        tui.warn("stop the tunnel you started by hand, if it runs: tmux kill-session -t grovy-tunnel")
    for p in (UNIT, os.path.expanduser("~/.config/systemd/user/default.target.wants/grovy-tunnel.service")):
        if os.path.lexists(p):
            os.unlink(p)
    olds = [o for o in install.read_record().get("old_grafts", []) if isinstance(o, dict)
            and (o.get("trunk"), o.get("name")) != (g.get("trunk"), g.get("name"))]
    if not g.get("trunk"):
        tui.warn("which trunk this computer was grafted onto isn't known: on that trunk, run grovy peer rm NAME and "
                 "delete the line ending in grovy-tunnel:NAME from ~/.ssh/authorized_keys")
        return
    try:
        remove_on_trunk(g)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        tui.warn(f"{e} ({why}; grovy cleanup tries again)")
        install.update_record(old_grafts=[*olds, g])
        return
    install.update_record(old_grafts=olds)
    if why == "now a trunk":
        for p in (KEY, KEY + ".pub"):
            if os.path.exists(p):
                os.unlink(p)


def from_unit():
    """{trunk, name} read back from the tunnel's unit file (a graft set up before the record knew it), or {}."""
    try:
        with open(UNIT) as f:
            m = re.search(r"^ExecStart=\S+ peer tunnel (\S+) --name (\S+)", f.read(), re.M)
    except OSError:
        return {}
    if not m or not peers.HOST_RE.fullmatch(m.group(1).replace("%%", "%")) or not peers.valid_peer(m.group(2)):
        return {}
    return {"trunk": m.group(1).replace("%%", "%"), "name": m.group(2)}


def tunnel_command(trunk, name, port):
    return (f"tmux new -d -s grovy-tunnel '{os.path.join(core.HERE, 'grovy')} peer tunnel {trunk} --name {name} "
            f"--port {port} --identity {KEY}'")


def wait_online(trunk, name, tries=15, sleep=None):
    """Whether the trunk sees this graft online, asked over setup's shared ssh connection."""
    sleep = sleep or time.sleep
    for _ in range(tries):
        names, _why = probe(trunk, timeout=20)
        if names and names.get(name, "").startswith("online"):
            return True
        sleep(2)
    return False


def remove_on_trunk(graft):
    """cleanup: take this graft off its trunk (best effort). Raises with what to run there by hand."""
    trunk, name = graft.get("trunk"), graft.get("name")
    if not trunk or not peers.valid_peer(name or ""):
        return
    try:
        key = public_key()
    except (OSError, RuntimeError):
        key = ""
    r = ssh(trunk, REMOVE_SCRIPT.replace("@NAME@", name), input=key + "\n")
    if r.returncode or "REMOVED" not in (r.stdout or ""):
        raise RuntimeError(f"couldn't reach {trunk} ({clean(r.stderr)[-200:] or r.returncode}); there, run: "
                           f"grovy peer rm {name}, and delete the line ending in grovy-tunnel:{name} from "
                           "~/.ssh/authorized_keys")
