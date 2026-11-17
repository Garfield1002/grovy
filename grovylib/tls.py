"""Optional HTTPS. Off unless there is a certificate: plain HTTP stays the default.

The daemon serves GROVY_TLS_CERT + GROVY_TLS_KEY (your own files), else `$STATE/tls/server.pem` if `grovy tls init`
made one, for exactly the names and addresses grovy is opened by, made with the openssl command. Each device installs
`$STATE/tls/grovy.crt`, copied over by hand: grovy never serves it. See docs/DESIGN.md, "HTTPS (optional)".
"""
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile

from . import core

CERT_DAYS = 800   # iOS refuses server certificates valid for more than 825 days, even ones you installed
DNS_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
PEM_CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def tls_dir():
    return os.path.join(core.state_dir(), "tls")


def own_cert():
    return os.path.join(tls_dir(), "server.pem")


def public_cert():
    """The certificate alone (no key): the file to copy to each device."""
    return os.path.join(tls_dir(), "grovy.crt")


def cert_paths():
    """(certfile, keyfile or None) the daemon serves, or None for plain HTTP. ValueError if half-configured."""
    if core.env("TLS", "") == "off":
        return None
    cert, key = core.env("TLS_CERT"), core.env("TLS_KEY")
    if cert or key:
        if not (cert and key):
            raise ValueError("set both GROVY_TLS_CERT and GROVY_TLS_KEY")
        return cert, key
    own = own_cert()   # certificate and key in one file, so replacing it is atomic
    return (own, None) if os.path.exists(own) else None


def context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key, password=_no_passphrase)   # never a prompt on the daemon's terminal
    return ctx


def _no_passphrase():
    raise ValueError("the key is protected by a passphrase; grovy needs one without")


def stamp(cert, key):
    """Changes whenever either file does; None if one is missing."""
    try:
        return tuple((s.st_mtime_ns, s.st_size, s.st_ino) for s in (os.stat(p) for p in (cert, key) if p))
    except OSError:
        return None


def expires_soon(cert, days=30):
    """True if the certificate expires within `days` (None if openssl can't tell)."""
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-checkend", str(days * 86400), "-in", cert],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.lower()
    return False if "will not expire" in out else True if "will expire" in out else None


def cert_der():
    """The certificate devices install (`grovy.crt`), DER-encoded; None if `grovy tls init` never ran."""
    try:
        with open(public_cert()) as f:
            m = PEM_CERT_RE.search(f.read())
        return ssl.PEM_cert_to_DER_cert(m.group(0)) if m else None
    except (OSError, ValueError):
        return None


def matches(cert=None, root=None):
    """Whether the served certificate was signed by the one devices install (an interrupted or concurrent
    `grovy tls init` could leave them mismatched); None if openssl can't tell."""
    try:
        r = subprocess.run(["openssl", "verify", "-CAfile", root or public_cert(), "-purpose", "sslserver",
                            cert or own_cert()], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def cert_names(cert):
    """The DNS names and IP addresses a certificate is for (its subjectAltName), as text; [] if openssl can't tell."""
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", cert],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for kind, value in re.findall(r"(DNS|IP Address):([^,\s]+)", r.stdout):
        try:
            out.append(str(ipaddress.ip_address(value)) if kind != "DNS" else value)
        except ValueError:
            pass
    return out


def fingerprint(der):
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


# -- grovy tls init ------------------------------------------------------------------------------------------------

def parse_names(names):
    """(IP addresses, DNS names) from what the user gave, or ValueError. A DNS name needs a dot: the certificate is
    also valid for its subdomains (X.509 can't say otherwise), and a single label like "dev" is a whole top-level
    domain."""
    ips, dns = set(), set()
    for x in names:
        x = x.strip().lower().rstrip(".")
        if "%" in x:
            raise ValueError(f"{x}: leave out the %zone")
        try:
            a = ipaddress.ip_address(x)
        except ValueError:
            if not DNS_RE.match(x) and x != "localhost":   # *.localhost never leaves the machine
                raise ValueError(f"{x!r}: give a full host name (with a dot, e.g. box.example.org) or an IP address") \
                    from None
            dns.add(x)
            continue
        if a.is_unspecified:
            raise ValueError(f"{x}: give the address the phone opens grovy by, not {x}")
        ips.add(a)
    if not (ips or dns):
        raise ValueError("no name or address given")
    return ips, dns


def default_names():
    """This machine's full host name, if it has one."""
    fqdn = socket.getfqdn().lower()
    return [fqdn] if DNS_RE.match(fqdn) else []


def _config(ips, names):
    ips = sorted(ips, key=str)
    names = sorted(names)
    alt = [f"IP.{i} = {a}" for i, a in enumerate(ips)] + [f"DNS.{i} = {n}" for i, n in enumerate(names)]
    # What protects is that the installed certificate's key is destroyed once it has signed the server's. The name
    # constraints (exactly these names and addresses, and subdomains of the names) are a second guard only where
    # they're enforced: Android, for one, ignores them on a certificate you installed.
    nc = ([f"permitted;IP.{i} = {a}/{ipaddress.ip_network(a).netmask}" for i, a in enumerate(ips)] +
          [f"permitted;DNS.{i} = {n}" for i, n in enumerate(names)])
    return "\n".join([
        "[req]", "distinguished_name = dn", "[dn]",
        "[root]",
        "basicConstraints = critical, CA:TRUE, pathlen:0",
        "keyUsage = critical, keyCertSign, cRLSign",
        "extendedKeyUsage = serverAuth",
        "subjectKeyIdentifier = hash",
        # Not marked critical: a client that doesn't know name constraints still accepts the certificate.
        "nameConstraints = @nc",
        "[server]",
        "basicConstraints = critical, CA:FALSE",
        "keyUsage = critical, digitalSignature, keyEncipherment",
        "extendedKeyUsage = serverAuth",
        "subjectKeyIdentifier = hash",
        "authorityKeyIdentifier = keyid",
        "subjectAltName = @alt",
        "[nc]", *nc,
        "[alt]", *alt, ""])


def _openssl(*args, cwd):
    r = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"openssl {args[0]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def init(names):
    """A new certificate for `names` (IP addresses and host names): `$STATE/tls/server.pem` (the server's certificate
    and key) and `$STATE/tls/grovy.crt`, the certificate devices install. That one is a CA (phones only trust those,
    and Firefox refuses a server certificate that is a CA itself) limited to `names`, whose key signs the server's
    certificate and is then destroyed: it can never vouch for anything else. Returns (ips, dns names)."""
    if not shutil.which("openssl"):
        raise RuntimeError("the openssl command is needed (e.g. dnf install openssl)")
    ips, dns = parse_names(names)
    d = tls_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    for x in os.listdir(d):   # a run killed midway (no Python cleanup) can leave its temporary folder, root key included
        if x.startswith("tmp") and os.path.isdir(os.path.join(d, x)):
            shutil.rmtree(os.path.join(d, x), ignore_errors=True)
    first = (sorted(dns) or [str(min(ips, key=str))])[0]
    with tempfile.TemporaryDirectory(dir=d) as tmp:   # mode 0700, deleted at the end with the root key in it
        with open(os.path.join(tmp, "x.cnf"), "w") as f:
            f.write(_config(ips, dns))
        _openssl("req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", str(CERT_DAYS),
                 "-subj", f"/CN=grovy {first}", "-config", "x.cnf", "-extensions", "root",
                 "-set_serial", "0x" + secrets.token_hex(16), "-keyout", "root.key", "-out", "root.pem", cwd=tmp)
        _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", f"/CN={first}",
                 "-config", "x.cnf", "-keyout", "k.pem", "-out", "r.csr", cwd=tmp)
        _openssl("x509", "-req", "-in", "r.csr", "-sha256", "-days", str(CERT_DAYS), "-CA", "root.pem",
                 "-CAkey", "root.key", "-set_serial", "0x" + secrets.token_hex(16), "-extfile", "x.cnf",
                 "-extensions", "server", "-out", "c.pem", cwd=tmp)
        os.unlink(os.path.join(tmp, "root.key"))
        _openssl("verify", "-CAfile", "root.pem", "-purpose", "sslserver", "c.pem", cwd=tmp)
        with open(os.path.join(tmp, "c.pem")) as c, open(os.path.join(tmp, "k.pem")) as k:
            combined = c.read() + k.read()
        fd = os.open(os.path.join(tmp, "server.pem"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(combined)
        os.chmod(os.path.join(tmp, "root.pem"), 0o644)
        os.replace(os.path.join(tmp, "root.pem"), public_cert())
        os.replace(os.path.join(tmp, "server.pem"), own_cert())
    return ips, dns
