# grovy: design and internals

For people changing grovy (and Claude sessions doing it: see `CLAUDE.md`). Users: see `README.md` and the guides it links to.

```
claude ──hooks──► grovy hook NAME ──unix socket──► grovyd ──HTTP+SSE──► phone
   ▲                                               │
   └───────────── tmux paste-buffer ◄──────────────┘  (prompts typed only when known safe)
```

| file | what |
|---|---|
| `grovy` / `grovy.py` | the `grovy` command, and the library scripts get with `import grovy` |
| `grovyd.py` | the daemon: HTTP API + SSE for the browser, Unix socket for hooks and the CLI |
| `extensions/` | built-in extensions of the page, written against `window.grovy` like a user's `custom.js` (see "Making it yours") |
| `grovylib/` | the rest, one file per topic (CLI, hook adapter, tmux, registry, and the daemon's `Store`, assembled from one class per feature); `grovylib/__init__.py` maps it |
| `index.html` | the whole UI (vanilla JS, no innerHTML); a map of its parts opens its script |
| `sw.js` | the service worker, served at `/sw.js`: shows push notifications and opens their session; caches nothing |
| `plugins/` | built-in daemon plugins (`git.py`, `push.py`), written against the plugin `Api` like a user's own |
| `themes/` | built-in themes for ⋯ → Theme (`nature.css`), CSS like a user's own (see "Making it yours") |
| `tests/test_grovy.py`, `tests/test_peers.py` | unit + tmux integration tests (`python3 -m unittest discover -s tests`) |
| `examples/` | systemd unit, Telegram notify script, the generated hook config |
| `assets/` | icon sizes and the home-screen backdrop, from [grove](https://github.com/Garfield1002/grove), served without the token from an allowlist (`ASSETS` in `grovyd.py`); `logo.png` is only for the README, not served |

> **Renamed from `ccs`.** On first run, `~/.local/state/ccs` moves to `~/.local/state/grovy`, and a symlink is
> left behind for sessions started before the rename. `CCS_*` environment variables are still accepted.
> Phones logged in with the old cookie stay logged in, and records written before the rename still load. (The
> `ccs`/`ccs.py` command aliases were removed once no session started before the rename was left.)

## Session settings and context

`grovy new` (and the phone) writes a per-session settings file (`$STATE/hooks/NAME.json`, see
`examples/claude-hooks.json`) and starts `claude --settings THAT_FILE`. Hook arrays from `--settings` are *merged*
with the user's settings, so existing hooks keep working. Claude processes not started by grovy are unaffected.
The SessionStart hook adds the widget/page guide (`WIDGET_GUIDE`, about 450 tokens) as `additionalContext`, followed
by the user's own `~/.config/grovy/instructions.md` if there is one (see "Making it yours").

## Network access and the token

The daemon binds to **127.0.0.1 by default**. To reach it from the phone, bind it to the
machine's VPN address, never `0.0.0.0` on a machine with a public interface:

```sh
GROVY_BIND=100.101.102.103 grovy serve        # or 0.0.0.0 for every interface
grovy url                                   # prints http://ADDR:7333/?token=... : open it once on the phone
```

`grovy setup` ends by printing that same link as a **QR code** to scan with the phone's camera, rather than the
long token URL (`grovylib/qr.py`, a small stdlib-only byte-mode encoder, drawn with half-block characters and
explicit black/white colours so it scans in any terminal theme); `grovy url` still prints the link as text to copy.

**A token is always required, loopback included**, because other local users and DNS-rebinding web pages can
reach 127.0.0.1 too. It's generated once into `$STATE/token` (mode 0600). `GROVY_TOKEN` overrides it. Opening the `grovy url` link sets an HttpOnly, SameSite=Lax
cookie for a year. (Lax, not Strict: a standalone PWA launched from the phone's home screen is a top-level
navigation the browser counts as cross-site, and Strict withholds the cookie there, so every cold launch landed
back on the login page. CSRF is covered without Strict — actions are POST, guarded by the forced CORS preflight and
the Origin check, and GET API routes need the `X-Grovy` header a navigation can't set.) Without it the page shows "login needed" and the API returns 401. That page also has a field to
paste the login link (or the token) into, a plain GET form to `/?token=`, since grovy added to an iPhone's home screen
keeps cookies of its own and has no address bar to open the link in. A token in a URL is never written to the log. To rotate the token, delete the
file and restart the daemon. ⋯ → Login link (on the session list only, not inside a session) shows the same link for the address the page was opened
on: the cookie is HttpOnly, so the page asks `POST /api/login-link`, which returns the token only to a logged-in,
same-origin JSON request (a cross-site page can't send one). A script injected into the page could read the token
that way; the strict script CSP is what prevents that. `GROVY_AUTH=off` temporarily accepts requests without a token while still setting the
cookie from the link. That's useful for enrolling a phone whose only way to see the link is the grovy page itself.
Without HTTPS (below) the token protects against other peers on the network, not an on-path observer; the VPN
provides the encryption. Cookies aren't isolated by port, so another web service on the same host name/IP that your phone visits
would also receive the cookie. Only browse services there that you trust.

**Host check:** requests are accepted only when the `Host` header is an IP address, `localhost`, or this machine's
host name. That defeats DNS rebinding, where a malicious page reaches the daemon through its own domain name. To use
another name, such as a MagicDNS name, add it to `GROVY_HOSTS` (comma-separated). Hook ingestion is **never** on the network: it uses a Unix socket with mode 0600 in the state directory.

"Add to Home Screen" on the phone gives it an app-like icon.

## HTTPS (optional)

Off by default; plain HTTP is unchanged. The daemon serves HTTPS on the same port when it has a certificate:
`GROVY_TLS_CERT` + `GROVY_TLS_KEY` (your own, e.g. Let's Encrypt), else `$STATE/tls/server.pem` (certificate and key
in one file, so replacing it is atomic) if `grovy tls init` made one. `GROVY_TLS=off` ignores both. A certificate
that is there but doesn't load stops the daemon at start (it never falls back to HTTP silently); a key with a
passphrase is refused, never prompted for. The reconcile loop (10s) reloads it when its files change
(`HttpServer.reload_tls`, a new `SSLContext` swapped in whole); a broken new file keeps the previous one. With HTTPS
the login cookie is `__Secure-grovy_token` (`Secure`), and the plain `grovy_token` is deleted, also when the page
loads for a phone that logged in over plain HTTP (it would otherwise keep sending it in the clear to the old
`http://` address). A different name because a browser won't let `http://` overwrite a Secure cookie of the same
name: turning HTTPS off again would lock phones out until they cleared the site's data. Links (`grovy url`, `status`) say
`https`, and `status` reports which certificate is served.

`grovy tls init [--name NAME ...]` (`grovylib/tls.py`, using the `openssl` command; default name: this machine's full
host name) makes, each time, two certificates for exactly those names and IP addresses, valid 800 days (iOS refuses
more than 825; `grovy doctor` warns 30 days before):
* `grovy.crt`, what devices install: a CA, because phones only offer to trust a CA (`serverAuth` only). Its key
  signs the next certificate and is then deleted (it only ever existed in a 0700 temporary folder, which a later run
  removes if a killed run left it): it can never vouch for anything else. That is the protection. Its **name
  constraints**, pinned to those names and addresses, are a second guard only where enforced: Android ignores them
  on an installed certificate (and they aren't marked critical, so a client that doesn't understand them accepts
  it). `grovy doctor` checks that the served certificate is signed by `grovy.crt` (an interrupted run could leave
  them mismatched).
* `server.pem`, what the daemon serves (certificate and key in one file, 0600): an ordinary server certificate
  (`CA:FALSE`), signed by the above. Firefox refuses a server certificate that is a CA itself, so it can't be one file.

X.509 can't exclude subdomains, so a name also covers its subdomains; a name must therefore have a dot (`dev` would
be a whole top-level domain), except `localhost`. Every run makes a new pair, which each device installs again (with
no lasting CA, nothing else could stay the same). A name other than the machine's own also needs `GROVY_HOSTS`;
`grovy tls init` says so.

**Getting the certificate onto a device.** `grovy tls init` also writes the certificate alone (no key) to
`$STATE/tls/grovy.crt` and prints how to install it (Firefox, Linux system-wide with `trust anchor`, iPhone,
Android), with its SHA-256 fingerprint. You copy that file over yourself (cable, AirDrop, USB stick): grovy **never serves it**. Over
plain HTTP anyone on the path could swap it, and with it any fingerprint shown beside it; over HTTPS it's useless
(a device that can open the page already trusts it). The fingerprint to check is the one the computer prints, against
the one the device shows for what it installed.

**Plain HTTP to the HTTPS port.** The daemon looks at the first byte of each connection, in the connection's thread
(the TLS handshake also runs there, never in the accept loop), under one deadline (`HEADER_DEADLINE`) for handshake
and headers together, and under idle eviction. A TLS handshake gets TLS; anything else gets `HTTPS_PAGE` for every
request: it links to `https://<same Host>/` (with `?token=` only if the request carried the right one, so an old login
link still works in one tap; the Host check requires a numeric port and no IPv6 zone id, since the Host is echoed)
and, with grovy's own certificate, says to install `grovy.crt` first. It never sets a cookie or shows anything
private. TLS reads that fail (a preconnect closed unused, a connection the deadline cut) end the connection quietly.

## Setup and cleanup

`grovy setup` (`grovylib/setupflow.py`, drawn with `grovylib/tui.py`) asks every question first, then shows one
review listing only what has to change, and applies it on a yes, step by step (✓/✗; the first failure stops it and
points to `grovy cleanup`). The questions: role (trunk, the default, or graft: joining a trunk, see "Other
machines"), the address (a menu of this machine's addresses: this computer only (the default, with a warning that
other devices can't open it), VPN (100.64/10), local network, public, everywhere), the port (the recorded one or
`GROVY_PORT`/7333; one that something else listens on is refused once and suggested a free one; typing it again uses
it, for that run only), HTTPS (only asked when other devices can reach it; default no; no writes `GROVY_TLS=off` into
the unit so an old certificate isn't served). The changes: the `~/.local/bin/grovy` link, bash tab completion (`completion.SCRIPT` in bash-completion's
per-user folder, loaded on the first tab; only written over if it is grovy's own, by its first line), the token and the
`custom.*` templates, a certificate (`tls.init`, for the address, or every address for 0.0.0.0, and the host name,
unless the current one already covers them), the systemd user service (previous version kept as `.bak`), starting
it, linger. Each change is recorded in `$STATE/setup.json` (role, address, port, https, the link it made, services it
wrote, `linger: true` if it turned linger on; that flag survives reruns). On a computer already set up, it first
offers: change settings (the recorded answers preselected), show the login link, or remove (= `grovy cleanup`).
It ends with the login link: a QR code and the link when other devices can reach grovy, else the link and an offer
to open it in the browser (only when asked in person); if the daemon answering still runs other settings (one
outside the service), it says so instead. A failing `enable-linger` is a warning (grovy then starts at login), not
a failure. Answers are preselected from the record, or for an install older than it from the unit itself
(`GROVY_BIND`, `GROVY_PORT`, `GROVY_TLS`; HTTPS on when `server.pem` exists and isn't turned off); a recorded address
that isn't up right now stays offered and the default. With `-y`, every question takes its default (`tui.AUTO`)
and it applies; without a terminal and without `-y` it shows the plan, changes nothing and exits 2. An address that
isn't up right now can't be probed for its port: that port counts as free. HTTPS with no address or
host name to put in the certificate is refused. The unit carries `GROVY_HOME`/`GROVY_STATE_DIR` when they are set,
and `PATH` quoted. Ctrl-C stops it with a line saying what stays; flags
(`--role`, `--bind`, `--port`, `--https/--no-https`, `--no-service`, `--dry-run`) answer questions up front.

`grovy cleanup` (`grovylib/cleanup.py`) plans first, shows the list, and applies it on a yes (`-y` without asking,
`--dry-run` only shows; looking makes no folder: one created by the look itself is removed). It refuses to start when
a `grovy`/`grovy-tunnel` unit runs another grovy folder (its `ExecStart`, `%h` expanded): that grovy shares the
state. Steps that stop something (a service, a daemon) stop cleanup if they fail; steps that remove what a rerun
needs (the state with its record, the tunnel key) run last and are skipped once anything failed, so `grovy cleanup`
again retries exactly what's left. A hand-started daemon's pid is checked again just before it is stopped (a daemon
that doesn't answer is a failure, not "gone"). It stops and removes the `grovy` and `grovy-tunnel` services and unit files (and `.bak`),
stops a daemon running outside the service (by the `pid` the daemon reports in `status`; a daemon too old to report
one makes cleanup stop before changing anything), removes the link only if it points to this grovy folder (the completion file only if it is grovy's), turns
linger off only if the record says setup turned it on, and removes the state folder, the ccs symlink and the
attachments folder. The grovy home is kept unless asked (`--remove-home`), and running sessions are ended only on a yes
(`--end-sessions`); they keep running in plain tmux otherwise (until the user manager exits at the last logout, once the
service and linger are gone; their hooks recreate an empty state folder). Without a record (installs older than it), it removes
what it finds in those places. A folder is only ever deleted when it is named for grovy (or ccs), is not the home
folder, `/` or one of the home's parents, and doesn't hold the grovy code: a mistyped `GROVY_STATE_DIR=~` can't
wipe a home. It refuses to run inside a grovy session, which it would cut off. The grovy folder is never touched.

**`grovy update`** (`grovylib/update.py`) updates the grovy folder from its upstream branch: it refuses without one,
with uncommitted changes to tracked files, and when the branch has commits of its own (it only fast-forwards: no
merge, no rebase), fetches, lists the incoming commits (cleaned of control characters) and asks (`-y`; without a
terminal and without `-y` it changes nothing and exits 2; `--check` only shows). After `git merge --ff-only` it
checks that `grovy`, `grovylib.hook` and `grovyd` import in a fresh interpreter; if not, `git reset --keep` back to
the commit it came from, and nothing is restarted. Then it restarts what runs the code: the `grovy` service, else a
daemon started by hand in a tmux pane (the pane whose process is the daemon's pid, respawned with its own start
command), and the `grovy-tunnel` service; then waits for the daemon to answer. Claude sessions are never touched;
those started before keep their `--settings` until ⋯ → Update session. `--no-restart` updates the code only.

## Configuration (environment variables)

| var | default | |
|---|---|---|
| `GROVY_BIND`, `GROVY_PORT` | `127.0.0.1`, `7333` | HTTP listener (`grovy serve --bind/--port` too) |
| `GROVY_TOKEN` | `$STATE/token` | web UI token (auto-generated) |
| `GROVY_HOSTS` | unset | extra accepted `Host` names, comma-separated |
| `GROVY_TLS_CERT`, `GROVY_TLS_KEY` | unset | serve HTTPS with this certificate (else `$STATE/tls/server.pem` if `grovy tls init` made one; see "HTTPS") |
| `GROVY_TLS` | on | `off` = plain HTTP even if there is a certificate |
| `GROVY_AUTH` | `on` | `off` = don't require the token (enrollment only) |
| `GROVY_STATE_DIR` | `$XDG_STATE_HOME/grovy` → `~/.local/state/grovy` | all state, mode 0700 |
| `GROVY_TERMINAL` | `off` | `on` = another machine (this one's trunk) may open a terminal on this machine's sessions (`grovy attach pc:NAME`, see "Other machines") |
| `GROVY_HOME` | `$XDG_CONFIG_HOME/grovy` → `~/.config/grovy` | your own files: `custom.css`/`custom.js`, `themes/`, `instructions.md`, `plugins/`, `peers.json` (`GROVY_CONFIG_DIR` is the older name; `GROVY_HOME` wins). Passed on to sessions started from it |
| `GROVY_PERMISSION_TIMEOUT` | `600` | seconds a permission waits for the phone (fixed per session at `grovy new`) |
| `GROVY_CLAUDE` | `claude` | claude executable |
| `GROVY_PROJECTS_DIR` | `~/dev` | where ⋯ → New project makes projects |
| `GROVY_NOTIFY_CMD` | unset | command run with a short message on stdin (see below) |
| `GROVY_PUBLIC_URL` | unset | appended to notifications as a link |
| `GROVY_DEBUG` | unset | log HTTP requests |
| `GROVY_WIDGETS` | `1` | `0` = don't tell Claude sessions about widgets and pages (applies to sessions started afterwards) |
| `GROVY_OOM_AVOID` | `1` | `0` = don't move sessions out of systemd-oomd's way (see "Out of memory") |
| `GROVY_PLUGINS` | `1` | `0` = load no plugins: neither `~/.config/grovy/plugins/` nor the built-in ones (see "Making it yours") |
| `GROVY_SUBAGENT_HOOKS` | `0` | `1` = forward every hook of subagents (normally only permission requests, notifications, start/stop and tool calls, which feed the background list) |

## Notifications (Telegram or anything else)

`GROVY_NOTIFY_CMD` runs, without a shell, for: *permission needed*, *needs input in terminal*,
*turn finished*, and *error*. Each message is one line, `[name] reason` (+ link), and never contains
command output or the full prompt. It is rate-limited to one per session per kind per 60s, and runs
in a background thread with a 15s timeout, so a failing bot never blocks hooks or Claude.
`examples/notify-telegram.sh` posts to an existing bot (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

**Push notifications on your devices (Web Push).** Needs HTTPS (see "HTTPS"): browsers allow service workers and
push only on a secure page. ⋯ → Notifications (on the session list; `extensions/push.js`) turns them on per device: it asks for permission
right inside the tap (iPhone shows the prompt nowhere else), subscribes with grovy's VAPID key and registers the
subscription's endpoint with the built-in plugin `plugins/push.py`, which sends a test. On iPhone it only works in
grovy opened from the home screen. The plugin listens to the same moments as `GROVY_NOTIFY_CMD` (its `notify` event,
not rate-limited) and sends each subscribed
device an **empty** push through its browser's push service (Apple, Google, Mozilla: the only way to wake a phone),
which so learns that something happened and when, never what. The service worker (`sw.js`) then fetches the text
from grovy (`POST /api/x/push/pending`, with the login cookie, over HTTPS) and shows it; tapping it opens the session.
Hence no payload encryption, and no crypto code in grovy: the VAPID key (P-256, in the plugin's `data_dir()`, 0600) is
made and used with the `openssl` command (ES256 tokens, one per push service, reused for 11h).
* All of grovy is **one notification, a stack**: a line per session (its latest news, newest first, at most 6), kept in
  the notification's own `data`, so dismissing or tapping it starts a new stack. It alerts when it appears, and again
  only for news that needs you (input, error); a finished turn updates it silently. Tapping it opens the session if
  it holds one, else the list. The badge (`assets/badge-96.png`) is white on transparent: Android draws it as a mask.
* Every push shows a notification (browsers count silent ones against the site, and Safari drops the subscription):
  one that finds no new message (an earlier one took it) redraws the stack quietly; one that can't reach grovy says
  "open grovy to see". While a grovy window is in front, news is shown and closed at once (the page shows it). Pushes carry a `Topic` per session and kind, so a phone that was off gets one of each, not a burst.
* On iPhone, `subscribe()` must run inside the tap itself: the extension fetches the key and the service worker
  beforehand, and calls it first thing (it asks for permission itself). It never unsubscribes a device on its own.
* Messages wait per device (at most 20, in memory: lost on a daemon restart) until its service worker takes them.
* A push service answering 404/410 ends that subscription. A device re-registers its subscription on each start.
* `GROVY_PUSH_CONTACT` (default: grovy's GitHub page) is the contact put in the tokens; Apple refuses placeholder
  addresses. Redirects from a push service are not followed (they would carry the token elsewhere).
* Untested against real push services from here: Apple's in particular (a home-screen app on an iPhone).
* The service worker caches nothing and handles no page requests, so the app is never a stale copy; the page
  registers it on every start, so an update reaches devices. `/sw.js` is public (nothing private in it).

## Making it yours

grovy's page can be extended by scripts that use one documented object, `window.grovy`, the same way for grovy's own
built-in extensions and for the user's. Everything personal lives in `~/.config/grovy/`, the *grovy home* (`GROVY_HOME`, formerly `GROVY_CONFIG_DIR`, still accepted), outside
the repo, so updates never conflict with it. `grovy setup` installs commented templates of `custom.css` and `custom.js`.

* **Built-in extensions** are `extensions/*.js` in the grovy folder, loaded in file-name order right after grovy's own
  script. They are ordinary features written against `window.grovy` only: `context-bar.js` (the line above the message
  box, see "Status info"), `git.js` (see "Git"), `issues.js` (see "Issues"), `links.js` (http(s) links in replies get an icon and a tint; a bare address shows as
  its host and its path (faint), the middle of a long one cut, set like code and like git.js's links, the full address as the title; inline code that is
  exactly an address becomes a link, `target=_blank` and `rel=noopener noreferrer` like markdown's; the address is read
  with `URL()` and only http(s) is touched), `math.js` (see "Math"), `swatch.js` (a dot of the colour in front of inline code that is exactly a hex colour,
  `#rgb`/`#rgba`/`#rrggbb`/`#rrggbbaa`: the only thing put in its `style`, after that check; code blocks are left
  alone) and `widgets.js` (the widget types, see "Widgets"). Each registers with `grovy.extension(name, setup, {about})`; the setups run at start-up,
  after `custom.js`, so `grovy.disable(name)` there turns one off (or replace it: disable it and copy it under another
  name).
* **⋯ → Extensions** (home menu, `index.html`) lists every named extension with its `about` line and a switch. A switch
  saves at once through `POST /api/extensions {name, on}` into `extensions-off.json` in the state dir (one list for
  every device; names must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`, at most 200, and anything else in the file is
  ignored). `load_index()` puts the list in a `<meta name="grovy-off">` tag, and the page skips those extensions at
  start-up like `grovy.disable()`. The file is part of the page's reload check, so the next page load gets the change.
  It applies on reload only ("Reload to apply" shows while the switches differ from what's running), since an
  extension can't be undone live. One turned off in `custom.js` shows as such and can't be switched on here. Turning
  off an extension whose daemon plugin stays on (git, push) only hides its page side.
* **Themes** (⋯ → Theme, home menu) are CSS files: built-in ones in `themes/` in the grovy folder (`nature.css`), the
  user's in `~/.config/grovy/themes/NAME.css` (NAME `[a-z0-9][a-z0-9_-]{0,31}`; one with a built-in's name replaces
  it, unless it can't be used, e.g. a dangling link or a refused file: then the built-in shows; an empty file is used,
  so it blanks the built-in). `POST /api/theme {theme}` (a listed name, or `null` for the default look) saves the choice to `theme.json` in
  the state dir (under a lock: the temp file of `atomic_write_json` is per process), for every device; the page then reloads. `load_index()` adds the chosen theme as
  `<style id="theme-css">` right after the built-in styles (so before extension styles and `custom.css`, which keep
  the last word), read with the same rules as `custom.css`, and lists the themes and the choice in a
  `<meta name="grovy-theme">` tag. A choice whose file is gone or refused means the default look. The default look
  has a few tokens only themes need: `--title-font` (titles, session names, reply headings, widget titles, section
  labels), `--grain` (an image under the page and its bars), `--tree` and `--tree-opacity` (the backdrop). Themes
  may restyle anything else too, but only the tokens are a stable API. The logo, the icons and the working dot keep
  their shapes: they only take the theme's colours (the logo is a mask filled with `--fg`, the dot is `--busy`).
* **`custom.css`** is added after the built-in styles (a later rule with the same selector wins).
* **`custom.js`** is added after the built-in extensions, as its own inline `<script>`. It is documented in
  `examples/custom.js`. `window.grovy` is a frozen object defined in the "extensions" part of `index.html`:
  * DOM and API helpers: `h`, `md`, `api`, `ask`, `say`, `sheet`, `css(text)` (placed before `custom.css`, which
    keeps the last word).
  * `slot("composer")`: the place above the message box (below the reply buttons), where the context bar sits.
  * The open session: `session`, and `state` as a copy.
  * `draft(text)`, and `send(text)`: the Send button's request, never forced, through the send hooks.
  * `widget(type, render)`: adds or replaces a widget renderer.
  * `math(render)`: draws the math in replies (`render(tex, display)` returns a node or throws); replaces the built-in.
  * `menu(label, run, {where, when, section})`: a ⋯ item; `when(state)` (the open session's state, null at home) shows it
    only when it returns true (a throwing or non-true `when` hides it). `section` groups it under a header in the
    session menu ("Conversation", "Session", or a name of its own, in that order then first-seen; the home menu is flat).
  * `key(combo, run)`: a shortcut.
    * It needs Ctrl or Alt (F-keys excepted), so it can't take typing.
    * Grovy's own Ctrl+Enter and Alt+↑/↓ are refused, and so are the browser's F5 and F12.
    * With Alt alone (Option on a Mac turns letters into symbols), letters and digits also match by physical key.
    * That fallback doesn't apply with AltGr (Ctrl+Alt), which types characters such as `@` that must stay typeable.
    * Shift is refused with a symbol: name the symbol it types instead.
  * `on(event, fn)`:
    * `render`: a message node was built and may be changed (the event is a copy).
    * `update`: a copy of the open session's state.
    * `route`.
    * `send`: may rewrite the text, or return `false` (or blank text) to keep it from being sent. It runs before the
      check that attachments don't go with a `/` or `!` command. An `async` hook stops the
      send, since its answer would come too late.
  * `extension`, `disable`, `extensions`.

  Start-up waits for `DOMContentLoaded`, so everything the extensions register is there for the first paint. A callback
  that throws, and an error while an extension loads, shows an error at the top of the page that stays until tapped.
  The failing callback is skipped. A `send` callback that throws stops that send.
* **`instructions.md`** (up to 16 KB) is appended to what the SessionStart hook tells Claude, after the widget guide
  and even with `GROVY_WIDGETS=0`: for example how to use a widget type `custom.js` adds. It is read at each start,
  resume, `/clear` and `/compact`.
* **`plugins/*.py`** run inside the daemon, loaded once it is serving (restart it to reload; `GROVY_PLUGINS=0` loads
  none, built-in ones included; events in the first moments after a start may not reach them). grovy's own built-in
  plugins (`plugins/*.py` in the grovy folder, such as `git.py`, see "Git") load first, the same way and with the same
  `Api`. A user plugin with the same file name (or the same module name: `a-b.py` for `a_b.py`) replaces the
  built-in one. Until plugins have loaded, `/api/x/...` answers 503 "still loading" (404 only with `GROVY_PLUGINS=0`),
  so a page asking just after a restart can tell "not yet" from "no such plugin". If loading itself fails (not one
  plugin: the loader), it's logged and the routes answer 404, never "still loading" for good. Each defines `setup(grovy)` and uses the `Api`
  it is given (`grovylib/plugins.py`, example in `examples/plugin.py`):
  * `on("event" | "state" | "notify", fn)`.
  * `route("GET" | "POST", path, fn)` for `/api/x/PLUGIN/PATH`. It sits behind the token. POST is checked like the
    rest of the API. A GET also needs an `X-Grovy: 1` header, which `grovy.api` sends and a tapped link or a page's
    navigation can't. Keep GET routes read-only all the same.
  * `command(fn)` (one per plugin): answers `grovy x PLUGIN ...` run inside a session, so a plugin can give the
    session's Claude a verb. `fn(cmd)` gets `cmd.args`, `cmd.session` and `cmd.cwd` (for a relative path) and returns
    the text to print, or `(status, text)`. The CLI forwards it over the daemon's socket; the file-sharing plugin
    (see "File sharing") uses it.
  * `download(path, name=None, session=None)`: offer a file on disk to the phone, returning a `/xfile/TOKEN` URL. The
    file is **not** copied; grovyd serves it straight from `path` (absolute) as a download (see "File sharing"). The
    offers live in a bounded in-memory registry on the plugins host, so they are lost on a restart.
  * `sessions()` and `session(name)`, as copies.
  * `send(session, text)`: the phone's rules, never forced, with a copy as its result.
  * `log()`.

  Isolation:
  * Store only queues the callbacks, with copied arguments, while it holds its lock. One worker thread runs them, so a
    slow or failing plugin never delays hooks, permissions or the page. Past 1000 waiting calls, new ones are dropped
    and logged.
  * Each `setup` runs on its own thread with a 10s deadline. A plugin that fails to import, raises (`SystemExit`
    included) or overruns is skipped whole: nothing it registered stays, and `on`/`route` refuse calls once `setup` is
    over. Two files that map to the same module name (`a-b.py`, `a_b.py`): the second is skipped.
  * A route runs on its own thread with a 30s deadline (504). At most 8 run at once, hung ones included (503), so they
    can't use up the server's connections. An answer must be JSON (no NaN), at most 4 MB, with a status that can carry
    a body. A `command` runs the same way, sharing those 8 slots and the deadline; its answer is text (≤ 4 MB), and a
    non-zero status prints it on stderr with that exit code.
  * A plugin that failed can't use its `Api` any more, even from a thread it left running.
  * A callback or route that raises is logged (a route answers 500), and the rest carries on.

The daemon rereads `extensions/`, `custom.css`, `custom.js`, the theme folders and `theme.json` when they change (mtime, size or inode: a save replaces the file; next page reload, no restart). Each
script's CSP hash is computed from exactly the block grovyd inserts, so nothing is loosened: only that text runs.
grovyd skips a file, with a line in the log, when it:
* is not a regular file;
* is over 256 KB;
* is not UTF-8, or contains a NUL;
* contains what would end its block early: `</style` in CSS; `</script` or `<!--` in a script.

Line endings are made LF, because the browser hashes a script after doing the same.

`custom.js` runs with the page's full access: it can read the conversation and use the API with your login. Plugins
likewise run with the daemon's rights. That is no new power: whoever can write your config folder can already change
grovy itself. It does make the config folder worth watching. A prompt-injected Claude that writes there gets
persistence: a `custom.js` that approves requests, or an `instructions.md` read by every new session. Claude's own
permission settings decide whether it may write outside the project. Check what changed there if a session did
something you didn't expect, and never install a `custom.js` or plugin you don't understand.

## How it works

**Identity.** Three different IDs, kept apart:
* *grovy name* (`api`): stable; validated `[A-Za-z0-9][A-Za-z0-9_-]{0,31}`.
* *tmux session* `grovy-api`: tagged with the tmux option `@grovy=api` and always addressed exactly
  (`=grovy-api`). A session counts as managed only if it carries the tag, and tmux is the authority on whether it exists.
* *Claude session_id*: learned from `SessionStart`. It changes on `/clear`, `/resume`, etc., and the latest one is
  stored in the registry for `grovy resume`.

  The hook command line is `grovy hook api`, so events are attributed from argv, not from cwd.

  **Subagents** run under the same settings, so their hooks call `grovy hook api` too, with the parent's
  `session_id` plus `agent_id`/`agent_type` (verified, Claude Code 2.1.325). The adapter forwards only
  `PermissionRequest` and `Notification` (they wait on you), `SubagentStart`/`SubagentStop`, and the subagent's
  `PostToolUse` (sent without waiting, never steered, its `tool_response` cut to `backgroundTaskId`). The daemon
  routes those last three to the background list only (see *Background work*), never to the session's state: a
  background subagent's `PostToolUse` can arrive after the parent's `Stop`, which would show an idle session as
  working. Nothing is printed for `SubagentStart`, so subagents never get grovy's guide (a forked subagent inherits
  its parent's context, guide included). `GROVY_SUBAGENT_HOOKS=1` forwards everything.
  Several sessions can share a project. A `claude` started inside a session (e.g. by a tool) does not
  inherit `--settings`, so it can't pose as the session.

**State** (per session, from hooks, never from screen scraping):

| event | state |
|---|---|
| tmux session created | `starting` |
| `SessionStart` | `idle` (ready for input) |
| `UserPromptSubmit`, `PostToolUse` | `working` |
| `PermissionRequest` | `needs_input` (with a pending request) |
| `Notification` `permission_prompt`/elicitation without our pending request | `needs_input` (terminal-only dialog) |
| `Stop` | `idle`, even with background work running (Claude accepts input meanwhile); the work is listed separately |
| `StopFailure` | `error` (input allowed) |
| `SessionEnd` or tmux session gone | `exited` |
| anything we can't vouch for | `unknown` |

`Notification idle_prompt` also re-asserts `idle`. A 10s reconciliation loop runs a single
`tmux list-panes` to detect vanished sessions. It never infers idleness from silence.

**Permissions.** `PermissionRequest` runs the hook synchronously. The hook connects to the daemon
socket and blocks while the phone shows the tool name and full input (Bash commands verbatim).
*Allow once* or *Deny* goes back as documented hook JSON
(`hookSpecificOutput.decision.behavior`). Verified with Claude Code 2.1.325:

* **The terminal dialog is shown while the hook waits.** Answering in the terminal makes Claude
  kill the hook. The daemon sees the socket close and marks the request `expired`, so the phone can't
  answer a dialog that no longer exists.
* The first decision wins; later or concurrent ones get HTTP 409. The UI only reports `delivered`
  after the hook has actually written the decision.
* If the daemon is down, crashes, or the request isn't answered within `GROVY_PERMISSION_TIMEOUT`, the hook
  exits 0 **with no output**, and Claude's normal terminal dialog takes over. **Nothing is ever approved
  by default.**
* No "always allow" and no rule changes. Your permission rules, deny rules and mode still apply first, so the
  web page only ever sees what Claude would have asked you anyway. In auto mode that's much less.

**Questions.** Claude's `AskUserQuestion` also arrives as a `PermissionRequest`, with the full
structured questions. The phone shows a question card: option buttons, multi-select toggles, and a
"type your own" field. Tapping only selects; nothing is sent until **Send answer**. The answer goes back as
`decision.behavior = "allow"` with `updatedInput.answers = {question: answer}`, and **Skip** sends a deny telling
Claude to use its judgment. Verified with real Claude 2.1.325, in auto and default mode, including free-text answers.
The terminal picker stays live in parallel, and whichever is answered first wins.

**Prompts.** Claude Code has no input API, so grovy types into the pane. Prompts up to 20,000 characters
are **typed** in 64-character `send-keys -l` chunks with 15ms pauses, with `C-j` for newlines and every `;` sent as a
raw byte (tmux drops a trailing `;` from an argument). Small bursts aren't detected as a paste, so Claude receives the
text as the user's own words. A real paste gets wrapped as `<pasted_content>`, which Claude treats as quoted material.
Longer prompts take the literal bracketed-paste path, with control characters stripped:
`tmux load-buffer -` (stdin, no shell) → `paste-buffer -p`. Then `send-keys Enter`. Safety rules:

* Permission cards show the complete tool input: special renderings (commands, diffs) plus every remaining field
  under "other fields". If the input is too large to show completely (over 512 KB), **Allow is disabled** on the
  phone and you approve or deny it in the terminal.
* Prompts are typed only when hook-derived state is `idle`/`error`, no permission is pending, the pane still
  belongs to that session (re-checked just before pasting), and no other web prompt is in flight.
  Otherwise they **queue**, survive daemon restarts (`prompts.json`), and are typed on the next `Stop`.
* Lifecycle: `queued` → `injected` (typed) → `submitted` (only when a matching `UserPromptSubmit` hook arrives).
  With no confirmation within 30s the prompt becomes `unconfirmed`. `/clear` and `/compact` emit no
  `UserPromptSubmit`; their receipt is `SessionStart` once they finish, so they get 10 minutes (compaction can
  take over a minute). **Prompts are never retried
  automatically**, and `injected` is persisted *before* typing, so a crash can't cause a double send.
* Receipts, WhatsApp-style: ✓ `queued` (grovy has it); ✓✓ `injected` with `typed` set (Enter was pressed, so Delivered bubbles float the time and ticks right, on the last line when it fits (`.st.in`).
* Full-screen message box: the box grows to 40% of the window, then takes the whole screen by itself while you type (not on opening a session with a long draft: the conversation shows first) (`#composer.full`, a fixed overlay; with `interactive-widget=resizes-content` the keyboard shrinks it rather than covering it). A ⤢ button in the box's top corner, shown from about three lines, toggles it by hand; shrinking a draft back keeps it small until the box is emptied. It returns to the small box when the message is sent or cleared, on switching session, and on Esc (after closing the `/` picker). While it's open the text runs edge to edge with no frame, a small "Message to NAME" title shares the top line with ⤡ and ✕ and send floats at the bottom right (the text gets bottom padding so its end can scroll clear of it); the reply chips, background-work line, hint, quote button and `composer` slot (the context bar) are hidden; the attachments and `/` picker stay. It pushes a history entry (`{full: 1}`, like the page viewer's), so the phone's back button shrinks it (and, like ⤡, keeps it small until the box empties); leaving full screen any other way pops that entry. It also shrinks when the session exits (its buttons are hidden then), sets `inert` on the header, sidebar and conversation while open, and on iOS (which ignores `interactive-widget`) follows `visualViewport` so the keyboard doesn't cover Send. The hint stays, cut to one line; attachments take at most a quarter of the height.
* Markdown as you type: the message box (`richBox` in `index.html`) is a `contenteditable` (`plaintext-only` where the browser has it) that shows markdown the WhatsApp way, the markers kept and dimmed: bold, italic, strikethrough, `code`, code blocks, `> ` quotes, headings and list markers (`richSegs`, pure, its pieces always join back to the exact text, so a caret offset is the same before and after a re-render). It answers like the textarea it replaced (`value`, `placeholder`, `setSelectionRange`, `selectionStart/End`, `focus`, `input` events), so the composer code didn't change. It re-renders only when the styling must change (compared by signature: plain typing leaves the browser's DOM alone), never while a phone keyboard composes a word (`isComposing`; again at `compositionend`), and puts the caret back by text offset. Enter, paste (plain text only) and undo/redo are its own (`beforeinput`): a browser would insert divs or rich HTML, and its undo stack points at nodes a re-render replaced. Typing within a second is one undo step; text set by the page (a session's draft, a quote, cleared after sending) starts undo afresh, so it never brings back another session's draft. Ctrl+B and other format commands are refused: markdown is typed.
* Scroll to latest: a round ⌄⌄ button above the composer shows when you're more than 300px from the bottom; it gets a dot when a render brought new events while you were scrolled up (auto-scroll only follows when you were already near the bottom).
* Selection: the page is `user-select:none` except message text, code, tool input and widgets, so long-pressing a card or button doesn't start a selection. Selecting conversation text shows a "Quote selection" button docked above the message box; it appends the text as `> ` lines to the draft, and user bubbles render `>` lines as a quote block. The rest of a user bubble is markdown, like a reply (`md(text, {widgets: false})`: a ```` ```widget ```` you type stays code), with its time and ticks floated at the end of the last paragraph. Copy is left to the system's menu. A page can't hide or extend that menu on phones (cancelling `contextmenu` on a touch long-press doesn't stop Chrome on Android showing it, tested), so a floating bar of ours only stacked a second menu next to it. Each Claude reply also has Copy (its markdown) and Quote (its rendered text, first 1000 chars). Quotes start with a `> Claude:` or `> You:` line; the bubble shows that as a label, and tapping the quote scrolls to the loaded message containing its first 60 characters (otherwise it says to load earlier messages).
* Attachments: the paperclip inside the empty text box (files come before the message; once anything is typed it becomes a ✕ that clears the box) uploads each picked file at once (`POST /api/sessions/NAME/attachments`, raw `application/octet-stream` body, which still forces a CORS preflight; name in `X-Filename`; ≤ `MAX_UPLOAD` 100 MB, streamed to disk). Files go to `grovy.attachments_dir()` = `$TMPDIR/grovy-UID[-hash of a non-default state dir]/attachments/SESSION/DATE-TIME-RAND-NAME` (name sanitised, mode 600, `O_EXCL|O_NOFOLLOW`). The `grovy-UID` folder lives in shared `/tmp`, so `private_dir` refuses to write unless it is a real directory owned by us with mode 700 (no planted symlink or open dir). Each upload prunes files older than 7 days. The message names them as `Attached: /path` lines (bubbles render those as chips); `/` and `!` commands can't carry attachments. Sessions get `permissions.additionalDirectories: [attachments_dir()]` in their `--settings` (verified: Read there is allowed without asking, denied in `-p` without it), so sessions started before this need ⋯ → Update session or will ask permission. Uploaded-but-unsent files are kept per session in the browser with the draft. Pictures (png/jpg/gif/webp) show as thumbnails via `GET /api/attachments/SESSION/FILE` (logged-in only; the name must match what `save_attachment` writes; opened `O_NOFOLLOW`, regular files only, under the same `private_dir` check; `Content-Security-Policy: default-src 'none'`, nosniff). Other files, and files already pruned, show as chips.
* Background work (shells started with `run_in_background`, async subagents): `s["tasks"]` mirrors Claude Code's own
  list. Every `Stop` and `SubagentStop` carries the whole list as `background_tasks` (`[{id, type: shell|subagent,
  status, description, command | agent_type}]`, verified 2.1.325) and replaces ours, keeping when we first saw each
  task and what it last did. Between those, a main `PostToolUse` whose `tool_response` has `backgroundTaskId` (a
  shell) or `isAsync`+`agentId` (a subagent) adds it at once, `SubagentStart` adds/refreshes a subagent (unnamed
  internal agents, `agent_type` "", are skipped), a subagent's `PostToolUse` sets its `last` tool and count (never
  re-adding one the list dropped), and a `<task-notification><task-id>X` or `<agent-message from="X">` prompt
  removes X. **A `SubagentStop` doesn't mean the subagent is done**: it also fires when the subagent ends a turn to
  wait on its own background shell, and the same id later fires `SubagentStart` again (verified). Only the list or
  the report ends it. Malformed list entries still count (never hide running work); at most `MAX_TASKS`=50 are
  detailed. The list is cleared by `SessionStart` startup/resume and `SessionEnd`, and not shown once exited. The UI
  shows "idle · ● N running" (pill, session list and sidebar: a separate blue mark, the state dot stays green) and a collapsed strip above the message
  box; tapping it lists each task with how long it has run, its command or agent type, and the subagent's last tool.
  `Stop` events also log `background_names`. Limitation: a background shell started by a *paused* subagent can't be
  attributed to it (the list has no owner field).
* "Worked for …" (in the reply's action row, before Copy and Quote; tap for the list): `UserPromptSubmit` starts `s["turn"]`; each main-agent `PostToolUse` appends `tool_summary` (tool name + its most telling argument, ≤120 chars; first `TURN_TOOLS`=100 kept, all counted); `Stop` records `worked`, `tools`, `tool_count` and ends the turn. In memory only: a daemon restart mid-turn loses that turn's list. Subagents' tools aren't listed there (they show in the background list).
  A turn with steps (below) folds those instead of the tool list.
* **Steps** (`grovylib/steps.py`): what Claude writes between tool calls, and the calls, read live from Claude Code's
  own transcript, since no hook carries them. Claude Code appends one JSONL record per finished content block, so
  steps arrive block by block, never token by token. `UserPromptSubmit` sets `s["tx"] = {path, off, n, more, skip}`:
  the payload's `transcript_path`, read from its current end (from 0 if it doesn't exist yet). If a `tx` for the same
  file already exists, its offset is kept: text typed mid-turn fires `UserPromptSubmit` without ending the turn, and an
  Esc interrupt sends no `Stop`. Readers are the daemon's tailer thread (`STEPS_EVERY`=1s, every session with a `tx`)
  and `ingest`, just before a main-agent `PostToolUse`, `Stop` or `StopFailure`, so the steps written so far come
  before that event. Only complete lines are read; each record of type `assistant` that isn't `isSidechain` gives
  `text` blocks (≤`STEP_TEXT`=4000 chars) and `tool_use` blocks (`tool_summary`) as `Step` events. Thinking, tool
  results and other record types are skipped. After `TURN_STEPS`=100 per turn they are only counted (`Stop.steps_more`).
  A line longer than `READ_MAX` is skipped whole. Two readers never record the same lines: each reads outside the lock,
  then applies only if `tx` and its offset are unchanged. Any main-agent hook naming another `transcript_path` (a
  `/clear`, a resume) moves `tx` to the end of the new file. `Stop`/`StopFailure`, `SessionStart` startup/resume
  and `SessionEnd` end it. `tx` lives in `state.json`, so a daemon restart mid-turn resumes where it was. It is
  removed from what the browser is sent. Verified (Claude Code 2.1.325): the answer's own text block reaches the file
  ~100ms *after* the `Stop` hook, so it's dropped with the ended `tx`. That's harmless, since `Stop` carries it. `detail`/`history` limits don't count steps, and `HISTORY` is 600 to make room.
  UI: steps belong to the next `Stop` (or `StopFailure`), else to the live turn. The header's ☰ button sets the
  default (`localStorage` `grovy-steps`, per device, off by default). "Worked for …" overrides it for one turn until
  the next view change or toggle. Trailing text steps that the final answer contains are hidden, as repeats.
  Widgets aren't drawn in steps. In final-only mode, the typing indicator shows the first line of the latest text step.
  Claude Code has it; for `/compact` the UI says "compacting…"); blue ✓✓ on the chat bubble (`UserPromptSubmit`
  or `Steered`: Claude read it). `typed` is only set after a send that wasn't aborted. Bubbles show the time the user sent the message: `UserPromptSubmit` and `Steered` events from a grovy prompt carry `sent` (its `created`), since the event's own `ts` is when Claude read it; prompts typed in the terminal fall back to `ts`.
* Typing re-checks between chunks and before Enter. If Claude starts something on its own meanwhile (a background
  task finishing) or opens a permission request, grovy stops **without pressing Enter**, so the Enter can't answer
  a dialog, and marks the prompt failed. Part of it may be left in the terminal input.
* `unknown` state blocks automatic typing. Send then asks "type anyway?" and only that prompt is forced.

**Steering.** A message sent while Claude works isn't typed (typing while Claude is busy could land in a permission
dialog that opens meanwhile: digits pick options, Enter confirms "Yes", and grovy hears of the dialog only ~100ms
later). It waits in the queue, and the hook of the main agent's next tool call (`PostToolUse`, not a subagent's)
asks the daemon for it and hands it to Claude as `additionalContext`, introduced as the user's own words sent from
the phone. The chat shows it as your message, marked "sent while Claude worked" (event `Steered`, prompt status
`steered`). Verified with Claude Code 2.1.325 (Haiku and Opus, and end to end through a scratch daemon): Claude acts
on it in the same turn.
* Queued plain messages go in order, up to 8,000 characters at a time. A queued slash command, and everything after
  it, waits for the turn's end, so nothing jumps it.
* If the turn ends before another tool call (e.g. Claude was only writing), the message is typed as usual once
  Claude is idle. A message also waits for a long-running tool call to finish; ■ interrupts.
* Steered messages don't appear in the terminal (only in the conversation itself). If the hook gives up waiting
  for the daemon's answer (3s), a message marked steered may not have reached Claude.
* (Typing in the terminal while Claude works is Claude Code's own steering: verified, it's queued at once, fires
  `UserPromptSubmit` immediately and is read at the next tool boundary.)

**Slash commands.** Claude Code trims leading whitespace and newlines before it looks for a command (verified: a
pasted `" /model"` or `"\n/model"` opens the model menu). So any message whose first non-blank character is `/` is
treated as a command, and only phone-safe ones are
accepted:
* skills and custom commands from `PROJECT/.claude/{skills,commands}` and `~/.claude/{skills,commands}`
* claude.ai-synced skills, as `anthropic-skills:NAME` (not yet verified by typing one)
* `/clear` and `/compact`

Anything else (`/model`, `/config`, `/resume`, …) opens a terminal menu or prints only to the terminal, so it's
refused with a pointer to `grovy attach`. Skill names that would shadow a built-in or alias (compared case-insensitively)
are ignored. A message starting with `!` (bash mode) is refused as well. Commands are typed followed by a space, which closes Claude Code's fuzzy menu so
Enter can't pick a neighbour. Skills confirm through `UserPromptSubmit`; `/clear` and `/compact` emit none and confirm
through `SessionStart(source=clear|compact)`. Typing `/` in the composer opens a picker of the allowed commands. `/clear`
asks for confirmation first (it sits next to `/compact` in the picker, and can't be undone).

**Interrupt.** While a session is `working` with no pending dialog and the message box is empty, the send button
becomes ■ (stop), which presses Esc (after a confirm); once you type, it becomes a blue steer arrow. Ctrl+Enter only
ever sends, never stops. Esc
emits no hook, so grovy records the interrupt itself and sets the state to `unknown`. The next send asks "type anyway?".

**Sessions from the phone.** The **＋** on the session list opens *New session*:
* **Folders.** Pick one from a list (never a typed path) of folders where Claude won't stop at the workspace-trust
  dialog, which would hold back every hook. Verified rule (Claude Code 2.1.325): a plain folder inherits trust from a
  trusted ancestor in `~/.claude.json`. A **git repo is its own boundary**: it needs to have been trusted itself
  (open it once in a terminal). Optionally tick *Ask before running tools* (`--permission-mode default`); resume
  remembers it.
* **Continue a conversation.** Picking a folder also lists Claude's saved conversations for it (from
  `~/.claude/projects/`), newest first, by AI title, size and age. Tapping one shows a preview (first message,
  Claude's last reply), then:
  * **Continue here** starts a grovy session with `claude --resume <id>`.
  * **Fork a copy** adds `--fork-session`, giving a new id and leaving the original untouched.
  * If the conversation is already open in a grovy session, you get **Open session X** instead.
  * If it's open in a plain terminal, only a fork is offered, so there's never a second writer. grovy detects that from
    Claude Code's own `~/.claude/sessions/<pid>.json` (undocumented; best effort, checked against `/proc` start
    times).
* **Session page.** The ⋯ menu is grouped under headers: *Conversation* (*Search this conversation*, *Pages* when
  Claude has published any, and extensions such as *Git*) and *Session* (*Rename*, *Update session*, *Stop session*
  — or *Resume session* and *Remove from list* once it has ended; ended sessions also get a *Remove* button in the
  list — and *Finish issue* for an issue). Items from `grovy.menu` fall under their `section`, or a trailing *More*
  if they name none. *Login link* is on the session-list menu only. Tapping the title switches between sessions. *Load earlier* pages back through the event log. Yes / No /
  Go ahead chips appear when Claude's last reply ends with a question; they fill the message box. Typing hides
  them but keeps their space until you send, so the conversation doesn't jump.
* **Update session** (⋯ menu, shown only when the session is out of date) restarts Claude with the latest grovy and
  resumes the same conversation. A session is out of date when what it was started with differs from what a start
  now would give: `start_session` records a fingerprint of the `--settings` content plus `SESSION_ENV_VERSION` (bumped
  when the session environment changes) in the registry, and the reconcile loop compares it (`outdated`). Sessions
  started before the fingerprint existed count as out of date. Hook changes
  apply right away, but the per-session settings file (status line, hook list) is only read when Claude starts.
  It runs only when nothing would be cut off (idle, no background task, no open request, no message being sent).
  Otherwise it says what is going on and offers **Update anyway**, which cuts those off. That consent covers that
  one tap, and the chat records what was cut off.

**Renaming.** Works while Claude runs: the tmux session is renamed live, and the registry keeps the old name as an
*alias*. The running Claude keeps reporting its hook events under the old name until its next start (Claude reads its
settings file only at start), so the daemon routes alias → current name. Old history records are shown under the new name. An
alias can't be taken by another session until the renamed one is removed.

**Status info.** Each session's status line runs `grovy statusline NAME`. It forwards model, effort, fast mode and
context use to the daemon (shown by the built-in extension `extensions/context-bar.js` as a full-width bar just above the message box, below
the reply chips, replacing the composer's top line, tap for details; and as "context N%" in the list), then prints your own
configured status line, so the terminal looks the same. Your status line is looked up in your user settings and the
session's own project folder, never in the folder Claude happens to be in: a cloned repo's `.claude/settings.json`
must not get its command run without Claude's trust dialog. Sessions started before this existed report it after
their next start.

**Git.** The built-in plugin `plugins/git.py` answers read-only questions about the repository a session runs in.
The built-in extension `extensions/git.js` uses it:
* ⋯ → Git: a graph of recent commits (all branches, or one), with the current branch, its upstream (ahead/behind) and
  the number of changed files, and a link to the remote's web page when there is one (the remote the branch tracks,
  else `origin`, else the only one). ssh, scp-style and `git://` addresses become `https://HOST/PATH`, without user
  name, password or `.git`; a local path, `file://` or a host name without a dot gives no link. Whether that host
  really has a web page isn't checked (a plain ssh server has none). Tap a commit for its message, files and diff (capped at 200 KB, shown at once when
  short), with its parents and refs as links. The message's hand-wrapped lines are joined back into paragraphs
  (list items, code blocks, headings and trailers such as `Co-Authored-By:` keep their own lines), since markdown
  here keeps line breaks and a phone would break each line twice. "Ask Claude about it" puts the commit in the message box.
* Inline `code` in a message whose text is a branch, remote branch, tag or commit hash (7–40 hex digits, not all
  digits) of that repository becomes a link, with a small icon for its type (commit, branch, tag): a commit opens it, a ref opens the graph on it. Names are looked up in
  batches (`POST /api/x/git/resolve`) after a message is drawn, and remembered per session; a miss is retried after a
  minute, and a session's answers are forgotten when its turn ends, since Claude may have made or moved a branch. A
  reply that ends a turn (Stop) is always looked up afresh: it is drawn before the update that ends the turn, and it
  is the message most likely to name what Claude just made. Code blocks are left alone.

Which folder: the one Claude last reported working in (the `cwd` of its latest hook event, as the session list
shows), so it follows Claude into a subfolder or another repo it `cd`s into. Before the first hook event it is unknown,
and the plugin answers `{"repo": null}` (the page says so), never "not a repository".

Safety: git runs only in that folder (never a path from the request), as an argument array, with `GIT_*` variables
removed from its environment. A ref from the request is used only if it is exactly one of the repo's own full ref
names, a hash only if it is hexadecimal and names a commit (`cat-file --batch-check`), both after `--end-of-options`;
so no revision expression or option can be passed in. That folder may hold a hostile repository (unpacked from an
archive, or with a `.git/config` a prompt-injected Claude edited), and the daemon must not run what it says, outside
Claude's permission prompts:
* No pager, fsmonitor, external diff or textconv driver; no signature checks (`log.showSignature=false`, which would
  run `gpg.program`); no lazy fetch in a partial clone (`GIT_NO_LAZY_FETCH`, which would run the remote's
  `uploadpack` or `sshCommand`, and needs the network).
* `git status` (the changed-file count) runs clean filters. If the repository's own config (local or worktree scope)
  defines any filter, there is no count: fail closed. Switching such filters off one by one was tried and bypassed
  (a name with a control character or invalid UTF-8 named a different key). The user's global filters, such as
  git-lfs, don't count. Status doesn't look into submodules (`--ignore-submodules=all`), and a commit's diff shows a
  submodule change as a pointer change (`--submodule=short`): with `diff.submodule=diff`, git would diff inside the
  submodule with that submodule's own config, its `diff.external` included.
* Log output is UTF-8 whatever `i18n.logOutputEncoding` says (UTF-16 would put NULs inside fields).
* This needs git 2.44 or newer (`GIT_NO_LAZY_FETCH`). With an older git the plugin still links names and draws the
  graph, but shows no changed-file count and no commit details (502), and logs why at start.
* `--no-optional-locks` keeps `git status` from writing the index while Claude may be committing.

Each git command runs in a process group of its own with a 10s deadline that kills the whole group (a child holding
the output pipe would otherwise outlive it, and a route could hang for good, holding one of the 8 plugin route
slots). stderr goes to a file, so a flood of warnings (thousands of broken refs) can't fill a pipe nobody reads.
Output is capped (4 MB per command, 200 KB of diff, cut on bytes, never inside a character). Records are separated by
NUL, the one byte no commit message or name can contain. Ahead/behind is computed for the current branch only: for
every branch it would walk the history once per branch with an upstream, on every lookup. At most 3 graphs and
commits are read at once (503 past that): every route holds one of the daemon's 8 plugin route slots, shared by all
plugins, until it ends, even once the page has given up on it. The changed-file count is skipped when the rest of the
graph already took 10s. A git that fails or times out is an error (502), never "not a repository". On the page, a
lookup that failed is tried again twice, 5s apart, since a message is drawn only once.

Limitations: the graph keeps a lane's column for its whole length (no compaction); with many lanes they are drawn
closer together, so the graph stays under about 180px. `--topo-order` walks the whole history unless the repo has a
commit-graph file (`git maintenance` and `gc` write one): on a very large repo without one, the graph can hit the 10s
deadline. Hashes in other forms (`abc1234^`, a URL) are not linked. A short hash that is also a branch or tag name
is taken as that ref. Without the plugin (`GROVY_PLUGINS=0`, or a daemon started before it existed) there are no
links, and ⋯ → Git says so (asked again after a minute: plugins load just after the server starts).

**Issues.** One small change, in a session and a git worktree of its own, merged back in one tap (`grovylib/issues.py`,
the `Issues` part of `Store`; the page side is the built-in extension `extensions/issues.js`).
* On the home screen an issue's session shows indented (a left-border guide), right below its root project's own
  session, keeping each session's sorted order (`orderSessions` in `index.html`, matching a session's `cwd` against the
  `root` each issue's summary now carries). Several issues of one project stack under it; an issue whose root project
  has no session of its own falls to the end, still indented.
* ⋯ → New issue (home screen) lists the trusted projects that are git repositories (a main checkout, not a linked
  worktree), each with its default branch: origin's HEAD if it has one locally, else `main`, else `master`. Always that
  branch, whatever is checked out: an issue is a fix for the main line. Claude Code's own `--worktree` isn't used: it
  branches from `origin`'s copy, which may be behind your local branch.
* Starting one: the session's name is `issue-` plus the first words of the text (`-2`, `-3`... when taken; also never a
  branch or folder that exists), the branch has the same name, and the worktree is `<repo>/.claude/worktrees/<name>`.
  Being inside the trusted repo, Claude starts there without its trust dialog (verified with Claude Code 2.1.327).
  `/.claude/worktrees/` is added to the repo's `.git/info/exclude` (local, never committed) so `git status` in your
  checkout stays clean. The issue is saved (`$STATE/issues.json`) before Claude starts, so its SessionStart hook can
  tell Claude: make the change, test it, commit it on this branch, don't merge, push or switch branches. The text is
  then queued as the first message, typed once Claude is idle, like any other (a text starting with `/` or `!` is
  refused before anything is made: Claude Code would take it as a command). If the session can't start, the
  worktree and branch are removed again.
* ⋯ → Finish issue (shown only in an issue's session, `grovy.menu(..., {when})`) shows the commits and merges them:
  **a fast-forward done by grovy**, never by Claude, so nothing lands on the base branch without your tap. The page
  sends the commit the sheet showed (`tip`), and only that is merged: if Claude committed again since, it's refused
  and you look again. Refused,
  changing nothing, while Claude is working or a request is pending, with uncommitted changes in the worktree, with no
  commits, or when the base branch moved on (the sheet then offers to ask Claude to commit or to rebase, as a draft you
  send). Where the base branch is checked out (your main checkout, usually), it is `git merge --ff-only` there, which
  updates those files and refuses if your own uncommitted changes are in the way; where it isn't, `git update-ref`
  moves it, only from the commit it was checked against. Then the session is stopped and forgotten, and the worktree
  (`git worktree remove`, which refuses if anything changed since) and the branch (once checked to be in the base branch: `git branch -d` would check the branch your checkout has out) deleted.
  Nothing is pushed.

**New projects.** ⋯ → New project (home screen) makes a project and starts a session in it (`grovylib/newproject.py`,
the `NewProjects` part of `Store`; the page side is `extensions/projects.js`).
* Always in the projects folder, `GROVY_PROJECTS_DIR` (default `~/dev`): `<dir>/<name>`, made only if nothing is there.
  Names: letters, digits, `.`, `-`, `_`, starting with a letter or digit (max 64). The session is named after the
  folder (other characters become `-`; `-2`, `-3`... when taken).
* Blank: `mkdir` + `git init`. From a remote: `git clone -- URL`, only `https://` (no user or password in it), `ssh://`
  or `user@host:path`, and only over https or ssh (`protocol.allow=never` with those two allowed: no `ext::`, which
  runs a command, nor `file`; no submodules; a host always starts with a letter or digit). git never asks anything:
  no credential prompt, and it runs in a new process session with no terminal, so ssh can't ask about a host key and a
  repo you have no access to fails at once. After 5 minutes the whole process group is killed (git's helpers too).
  Git LFS fetches nothing (`GIT_LFS_SKIP_SMUDGE`: the repo's `.lfsconfig` would choose where from). A failed one
  leaves nothing. The clone runs outside `project_lock`, its folder reserved in `fresh` meanwhile.
* **Workspace trust.** A new git repository is its own trust boundary, so Claude asks whether to trust it and holds
  back every hook until it's answered. grovy answers it for you: blank projects at once (grovy just made them, empty);
  a clone only after the phone has shown what Claude would load from it once trusted, and you tapped Trust (the
  request must say `trust: true`). The list (`newproject.review`): `.mcp.json` and every `settings*.json` under a
  `.claude/` first, with the commands they name (hooks, MCP servers, status line, key helpers, env, plugins and
  marketplaces; 6 per file, then "… and N more"); then the rest of each `.claude/` and every `CLAUDE.md` (up to 4
  folders deep). A symlink within the clone is reviewed as what it points to (a symlinked `.claude` is walked); one
  leading out of it is flagged with the ones that run commands. At most 40 lines, then "… and N more". A settings
  file that can't be read (over 1 MB, not JSON, nested too deep) says so and to not trust it unless you know it;
  a review that fails altogether says the same, never stranding the clone. Each made project gets a random token that only the page that made it holds:
  starting or deleting it needs it, so if another tab makes the same name again (which deletes the first, left
  behind), the first tab's Trust is refused rather than trusting a clone it never saw. Closing that sheet, Delete
  the clone, or a failed start after the sheet closed deletes the folder (only one grovy made and that never got a
  session: `fresh`, in memory). Answering: the session is started as any other, then `newproject.accept_trust`
  reads the visible screen (`capture-pane` without scrollback, the only screen reading grovy does) and presses Down
  only while "No, exit" is the one highlighted line of that question, and Enter only while "Yes, I trust this
  folder" is (wording verified with Claude Code 2.1.328, whose default is No); a key that changed nothing in 2s is
  pressed again, 3 times at most, and only while the screen is exactly as it was when it was pressed (Claude's
  dialogs drop keys for a moment after they open; a slow answer must not get a second Enter that lands on the next
  dialog). It is done when `~/.claude.json` says the
  folder is trusted: Claude writes that itself, grovy never does. Anything else on the screen and it stops, after
  20s, having pressed nothing else: the session opens with a note to answer in the terminal, and the first message
  is not queued (it is put in the message box).
* If starting fails after the tmux session was made (a tmux timeout), it counts as started: the folder is never
  deleted from under a Claude that may be running in it.
* The first message is optional, and queued like an issue's (a `/` or `!` start is refused before anything is made).
* Discard stops the session and deletes the worktree and branch with their commits and changes (`--force`, `-D`),
  after you confirm. An issue's session can't be removed from the list (⋯ → Remove): that would leave its worktree
  and branch with no way to finish or discard them. What a cleanup couldn't do (stop the session, remove the worktree
  or branch) is said, never hidden.
* git runs with argument arrays, a 60s deadline and no credential prompt, in your own trusted repository: its hooks
  (post-checkout, post-merge) run as they would for you. Issue actions take `issue_lock` (one at a time) and run git
  outside `Store.lock`. Renaming an issue's session keeps the issue (the branch keeps its name).

**Desktop.** At 1000px wide or more there is no home page: the session list is a sidebar beside every route, built
by the same `listNodes()`/`sessCard()` as the phone's home screen (worktree nesting, peer warnings, pages), made
compact by CSS, refreshed on any session's event at most every 400ms and only fetched while it's shown. `#/` shows
"Pick a session" on the right. Sheets (menus, dialogs) are centred dialogs there, with a ✕ (`#sclose`), rows that
highlight on hover, and their body scrolling under the ✕; the phone keeps the bottom sheet. The sidebar header has ＋ and a ⋯ with grovy's own menu (what the phone's home ⋯
has, plus New session). There is no header row: the pill and the session's ⋯ float at the top right
(over the margin beside the conversation; on a window just over 1000px they can overlap a message's corner). The grove
backdrop stays, fainter, behind every route. Right-click (a mouse only: the event's `pointerType`, or `pointer: fine`
when it has none) does what ⋯ menus and an item's own small buttons do on the phone (`#ctx`). Any node can carry items
(`h(..., {ctx: el => [...]})`; extensions: `grovy.contextMenu(node, items)`); the menu lists the nearest one's, then
each enclosing one's below a divider (a code block's Copy code, then its reply's Copy, Quote, Show tool calls). Items
that mirror a button go through `press(button)`, which clicks that button, so its checks hold (Allow on a card too large
to show stays disabled), and on a permission card the first, focused item is a Copy, so Enter in the menu never
approves. Carriers: session cards (Open, Rename, Update, Resume, Stop, Remove), page cards (Open, new tab, its session,
Delete), replies, your messages (Copy, Quote; Cancel while queued), subagent reports, code blocks, attachments, tool
lines in "Worked for", permission cards (Copy, Deny, Allow once), folders in New session (Start here, Copy path). The
conversation's background gives the open session's ⋯ items, the rest of the sidebar or an empty right side grovy's.
With text selected in a message (`selectedText()`, what the phone's Quote button takes), it offers Quote selection and
Copy; the docked Quote bar is then hidden (wide screen with a mouse), and letting go of a mouse selection in a
message shows a small bar with ❝ Quote and Copy right above it (`#selbar`; gone when the selection is, on scroll or resize). Text boxes, a selection anywhere else, links and pictures inside a message, and Shift+right-click keep the
browser's menu. Extensions' ⋯ items appear only for the open session: they act on `grovy.session`. Remove/Delete
buttons are hidden in the sidebar (they are in the menu). ↑/↓ move in it, Esc or Tab closes it. A page opened from the sidebar may belong to another session: the viewer looks pages up in both lists
(`pageOf`), so the conversation's refresh doesn't close it. Keyboard:
Ctrl/Cmd+Enter sends (Enter stays a new line, as on the phone); typing with nothing focused goes to the message box;
Alt+↑/↓ switch to the previous/next session (not from a full-screen message box); Esc closes a sheet or the page viewer, else the `/` picker, else shrinks a full-screen message box, else leaves
the box. In the `/` picker the first command is highlighted; ↑/↓ move, Tab or Enter completes. Sheets take focus, keep Tab inside, and give it back
when closed. With a mouse (`pointer: fine`), opening a session focuses the message box; never on a touch screen (it would
pop the keyboard), and permission cards are never focused automatically, so a stray Enter can't answer one. Live
re-renders keep keyboard focus on the same control (`paint()`, matching rebuilt rows by `data-fk`).

**Math.** Markdown rendering (`md()`/`inline()` in `index.html`, so replies and anything else drawn with `grovy.md`,
such as commit messages) finds LaTeX: `$…$` and `\(…\)` inside a line, `$$…$$` and `\[…\]` either inside a line or
on lines of their own (a block, up to 60 lines, ending at a blank line; one punctuation mark after the closing `$$` is
kept). Display math is centred and scrolls sideways when wide; inline math doesn't wrap. A `$…$` needs no space after
the opening `$`, none before the closing one, and no letter, digit, `$`, `(` or `{` right after it, so `$5 and $10`,
`$PATH:$HOME` and `$(pwd)/$(ls)` stay text; a code span that starts first wins. Math is matched last in the inline pattern, so it only wins where it starts first (`$a_1 * b_2$` is not italics, while
`**$x$**` is bold math). Drawing is the built-in extension `extensions/math.js` (`grovy.math`): a small TeX parser that
builds MathML node by node, which the browser lays out itself (MathML Core: Chrome 109+, Safari, Firefox), so no
library, font or CSP change. It covers what replies hold (fractions, roots, scripts and primes, Greek, operators,
relations and arrows, big operators with limits under them in display only, functions, accents, `\mathbb`-style fonts
through the Unicode math letters, `\text`, `\left…\right`, `\big`, matrices, cases, `aligned`, colours). An unknown
command shows in red and the rest still draws; TeX it can't read (unbalanced braces, a missing argument, `\left`
without `\right`, over 4000 characters, nesting over 40 deep, `\rm`/`\color` scopes included) shows as written in a
code span, with the reason as its tooltip, and so does all math when the extension is off. The TeX is untrusted: the
only values taken from it into attributes are a colour matching `#rgb`, `#rrggbb` or a plain name, and `\hspace`-style
lengths as numbers clamped to -2..10em. `\text{…}` keeps its words: spacing commands become spaces, other commands
are dropped. A sign right after an operator or at the start is unary
(`= -2`); plain `(`, `|` get no spacing, as in TeX. Claude is told about the delimiters in `WIDGET_GUIDE`. Not done:
macros (`\newcommand`), `\tag` numbering, line breaking of long inline math, display math inside list items, and
a `|` inside math in a table row (it splits the cell: Claude can write `\vert`). False positives remain possible
for `\(…\)`/`\[…\]` in prose about regexes.

**Widgets.** Claude's reply can contain ` ```widget ` blocks holding one JSON object: `chart` (bar, line, pie),
`stats`, `progress`, `note`, `log`, `checklist`, `choices`, `form`, `diagram` or `image`. The page draws them itself with DOM and SVG
(the renderers are the built-in extension `extensions/widgets.js`, one `grovy.widget` per type: `custom.js` can replace one,
or turn them all off with `grovy.disable("widgets")`, after which every block shows as not shown);
nothing from Claude is run or parsed as HTML. Invalid JSON or an unknown type shows "Widget not shown: <reason>"
followed by the raw block. `choices` and `form` only fill your message box (asking first if it already holds a draft);
you send it like any message. Claude learns the format from grovy's `SessionStart` hook (`additionalContext`, the
`WIDGET_GUIDE` text in `grovylib/claude.py`), on every start, resume, `/clear` and `/compact`. Hooks run the code
as it is on disk, so a changed guide applies without a restart. `GROVY_WIDGETS=0` turns the guide off. The terminal shows the raw JSON,
so Claude is told to keep the key facts in plain text too.

**Image widget.** `grovy image FILE [--caption T]` checks the file is a real png/jpg/gif/webp (extension and magic
bytes, ≤ 20 MB), copies it to `STATE/images/SESSION/DATE-TIME-RAND-NAME` (mode 600, kept like pages, never pruned)
and prints a ` ```widget ` block `{"type":"image","src":"SESSION/FILE"}` for Claude to put in its reply. The page
fetches it from `GET /api/images/SESSION/FILE` (logged in; name pattern and image types only; `O_NOFOLLOW`; served
as an image with `default-src 'none'`), so a widget can only show images grovy copied, never an arbitrary path.

**File sharing.** The built-in plugin `plugins/share.py` lets a session's Claude hand the user *any* file to download
on the phone: `grovy x share FILE [--caption T]` (the CLI `x` verb forwards to the plugin's `command`, see
"Plugins"). Unlike the image widget, the file is **not** copied — `grovy.download(path)` registers the file's path
and returns a `/xfile/TOKEN` URL (token = 16 random bytes), and the plugin prints a ` ```widget ` block
`{"type":"file","name":..,"size":..,"url":..}` for Claude's reply; the phone draws it as a card with a Download
button (the `file` widget in `extensions/widgets.js`). Tapping it is a top-level navigation, so the `SameSite=Lax`
login cookie goes with it. grovyd serves `GET /xfile/TOKEN` (behind the token like everything past `_authed`) by
streaming the file from disk — no size limit, not read into memory — as `Content-Disposition: attachment`,
`application/octet-stream`, nosniff, `default-src 'none'`, `X-Frame-Options: DENY`, so an arbitrary file a session
offered is always a download and can never render or run on grovy's origin. It opens the path fresh each time (a
regular file only), so a file that has since moved or been deleted is a 404, not a half-sent 200. The offer points
at a path, not a copy: the file must still be there when the user taps, and a session's own output (a report, CSV,
zip or log it just made) is the intended use. The registry is in memory and bounded (oldest dropped past 4000), so
offers don't survive a daemon restart. Because the daemon runs as the user and serves only paths a session (also the
user's own code) chose, this adds no read reach beyond what the session already had; the login is what keeps it off
the LAN. Without the plugin (`GROVY_PLUGINS=0`, or a user `share.py` that replaces it) there is no `share` verb.

**Live widgets (from scripts).** A long command Claude runs (training, a build, a batch job) can show cards in the
chat *while it runs*, between your message and Claude's reply, and update them in place. Sessions get grovy's folder
appended to `PYTHONPATH` (after any entries of your own), so a script can `import grovy`:
`grovy.track(iterable, title)` (a progress bar, like tqdm), `grovy.progress(title, total)` with `.set(n, note=)` /
`.advance()`, `grovy.note(markdown)`, `grovy.log(title)` (a file-like object showing the last lines written, ANSI
colours stripped) and `grovy.show(widget)` → `.update(widget)` for any widget type. Shell equivalents:
`cmd | grovy tail --title T` (copies stdin to stdout unchanged), `grovy progress KEY N --max M [--done|--failed MSG]`,
`grovy note TEXT`, `grovy show JSON --key K`. The guide tells Claude about them, for scripts it writes to run, not
for your code. Sessions started earlier get the path spelled out in the guide until their next start.
* **Never hurts the script.** Outside a grovy session every call does nothing. If the daemon can't be reached the
  library goes quiet for 30s instead of waiting on each call (1s timeout). The CLI reports problems on stderr and
  exits 0 (except for invalid JSON), so a `set -e` pipeline doesn't die because of the phone.
* **Throttled, last state guaranteed.** The library sends at most one update per widget every 0.5s; a background
  thread sends the latest state if no later call comes. Each update waits for the daemon's ack, so updates arrive in
  order. The daemon refreshes browsers at most every 0.5s per session (always including the last update).
* **Lifecycle.** A card is `running`, `done`, `failed` (with a message) or `stopped`. Used as a context manager, an
  exception marks it failed; widgets still open when the script exits are closed then: `done` for a complete bar,
  `failed` after an uncaught exception, otherwise `stopped`. A `kill -9`ed script leaves its card `running`, with the
  time of its last update shown.
* **Storage.** One `Live` event per card (socket message `live`: session, key, spec, status, message). Updates
  replace the event's data and bump `rev`. The event log gets the record again under the same id at most every 2s
  while running (the reconcile loop writes the latest), and at once on a final status. Loading keeps the last line
  per id, so history and restarts show each card once, in place. A key whose card has left the in-memory history
  starts a new card.
* **Limits.** 64 KB of JSON per widget, at most 30 new cards per session per minute (updates are unlimited), so a
  runaway loop can't push the conversation out of the history. The page validates and renders the widget, as for
  ` ```widget ` blocks; an invalid one shows "Widget not shown".

**Auto mode.** Its classifier can't know grovy is local: in auto mode, a "make me a local page" request had
`grovy page publish` blocked as *Unsanctioned Communication* (reproduced 2 of 2 times without the fix, 0 of 2 with
it). The per-session settings therefore carry an `autoMode` block (`auto_mode_hints()`): an `environment` entry
describing grovy as your own tool that only talks to your daemon over a private socket, and an `allow` entry saying
that showing work to you through grovy (page, note, progress, show, tail, the Python functions) is answering you. It
covers the grovy invocation only; anything else on the same command line is judged on its own. Both lists start with
`"$defaults"`, and grovy never sets `soft_deny`/`hard_deny`, so every built-in rule stays. Check the effective
config with `claude --settings $STATE/hooks/NAME.json auto-mode config`. The guide also tells Claude to run
`page publish` as a command of its own.

**Pages.** For anything bigger than a widget (a report, a dashboard, an interactive explorer), Claude writes an HTML
file and runs `grovy page publish FILE --title "..."` (`grovy page ls`, `grovy page rm NAME`). The session comes from
`$GROVY_SESSION`; pass `--session` elsewhere. grovy stores a **copy** (`$STATE/pages/<id>.html`, max 5 MB) and never
serves files from disk directly. Publishing the same name again creates a new version, and an open viewer reloads.
The chat shows a card; Open shows the page full-screen (the back button closes it), and ⋯ → Pages lists and deletes
them. Removing a session deletes its pages. A page is served from `/p/<id>` (token required) with a CSP sandbox:
* opaque origin, so no cookies, no `localStorage`, no grovy API, no access to the app or to other pages;
* scripts and styles only inline or from cdnjs, jsdelivr and unpkg, plus Google Fonts;
* `connect-src 'none'` and images only from `data:`/`blob:`, so no fetch, XHR, WebSocket or image beacons;
* no form submission, popups or top-level navigation.

`grovy.send(text)` in a page fills your message box, exactly like a widget; nothing is sent by itself.

**Sharing a page on the network.** In the page viewer, **Share** gives the page an
extra link, `/s/<random 128-bit token>`, that works **without the grovy token** for anyone who can reach the daemon
(so only when it listens beyond localhost, e.g. `--bind 0.0.0.0`). The link serves only that page, under the same
sandbox, without the `grovy.send` bridge, and grants nothing else. Republishing keeps the link and updates what it
shows. **Stop sharing** (`grovy page unshare NAME`) revokes it; sharing again issues a new link, and deleting the page
revokes it too. Sharing exists **only on the phone**: there is no CLI or socket command for it, so a Claude
session can publish pages but can't make one public.

## Other machines (peers)

**How the CLI looks (`grovylib/tui.py`).** Commands people read (`ls`, `peer ls`, `page ls`, `doctor`, `url`, the
result lines of `new`/`kill`/`rename`/`resume`/`forget`, `core.die`'s `grovy:`) are styled only when their stream is
a terminal and neither `NO_COLOR` nor `TERM=dumb` is set (`tui.color_on`, per stream: stdout and stderr each on their
own); anywhere else they print exactly what they always did, byte for byte, because `remote.trunk_lines` parses a
trunk's `grovy ls --here` by whitespace columns and scripts may parse the rest (`grovy ls --plain` asks for that on a
terminal too; `forget` stays silent off a terminal). `tui.table` is borderless: column widths from display width
(wide characters 2, combining marks 0), the header dim, one column (or several, in order) cut with … to fit the
terminal (paths from their start, so their end shows), and every cell passed through `tui.safe` (control characters
become `?`) before it is measured, whatever cleaning the caller did. `grovy ls` colours STATE by what it asks of
you: idle green, working/starting cyan, needs input bold yellow, error bold red, exited/unknown/`running?` dim, an
unknown state plain; this machine's paths use `~`, other machines' names have a dim `pc:`. Local rows are printed
at once; when other machines' rows arrive and need wider columns, the local lines are redrawn in place (cursor up,
erase) so it stays one table, but only if they surely are still on the screen (fewer lines than the terminal's
height, each narrower than it); otherwise the remote rows follow as their own block. Notes on stderr get a yellow ⚠
on a terminal. `grovy ls trunk` from a terminal runs `ssh -t`, so the trunk's grovy draws its own table.

**The CLI on another machine's sessions (`grovylib/remote.py`).** `MACHINE:NAME` names a session elsewhere (local
names never hold a colon). On a trunk, MACHINE is a graft from peers.json: `attach`, `logs`, `kill`, `resume`,
`forget`, `rename`, `new` (no extra claude arguments, no `--fresh`) go straight to that graft's grovy through its
tunnel socket with the token from peers.json (`peers.Peer` without a hub; the daemon isn't involved), and `grovy ls`
lists every graft's sessions after the local ones, which it prints first: the grafts are asked at once (daemon threads) and
get 4s in all, the whole answer included, so one asleep behind its tunnel or trickling bytes can't stall it, and
Ctrl-C doesn't wait for them; one answering junk only loses its own rows (`grovy ls pc`: that graft only; an unreachable graft, or a peers.json
problem, is a line on stderr). What a graft says is untrusted, like Claude's output: names are checked, and states,
folders, errors and `logs` text are stripped of control characters and lone surrogates before they reach the
terminal (and `ls` fields of newlines and tabs, so a graft can't print a made-up row). `rename` can't
move a session between machines; `new` needs an absolute folder (it is resolved on the other machine). On a graft, MACHINE is `trunk`: the command runs there as `ssh [-t] TRUNK sh -c 'exec grovy …'` with your
usual ssh (the tunnel's key can't run commands). A graft's plain `grovy ls` adds the trunk's own sessions as
`trunk:NAME`: `grovy ls --here` there over ssh with `BatchMode=yes` (a login that needs a password is a note on stderr,
never a prompt in the middle of the list) and a 5s bound; rows whose name isn't a plain session name (the trunk's grafts,
this one included) are left out, and the fields are cleaned like a graft's. **A plain name works across machines**
(local and remote as transparent as possible): for `attach`, `logs`, `kill`, `resume`, `forget`, `rename`, a NAME that
isn't this computer's (registry or tmux; `cli.here`) is looked for on the others (`remote.find`, the same bounded
asking as `ls`): on one machine it runs there (`(NAME is pc:NAME)` on stderr), on several it stops and lists them
(never a guess, `kill` included), on none it's the usual error plus the machines that didn't answer. This computer's
session always wins and costs no lookup. `new` always means here. Tab completion offers the other machines' names
alone too when they're unambiguous, from `$STATE/completion-names.json`, written by `grovy ls` and by a detached
`grovy __complete --refresh` started when it's over 30s old (a tab never waits for the network; the file is marked
fresh before the refresh starts, so a burst of tabs starts one). `logs` reads `GET /api/sessions/NAME/screen?lines=N` (X-Grovy).
**attach** (`grovylib/terminal.py`): `POST /api/sessions/NAME/terminal` with the token, `X-Grovy: 1` and `Upgrade:
grovy-terminal` (a browser can't send Upgrade) gets `101`, then the graft's grovyd runs `tmux attach-session -t
=grovy-NAME` in a pseudo-terminal (TERM and size from `X-Term`/`X-Rows`/`X-Cols`) and relays it: its output as
raw bytes; input as frames (type byte, 2-byte length; type 0 keys, type 1 rows+cols). Detaching (C-b d) or the
session ending closes it; either side closing kills the attach (the graft waits 0.2s after the trunk's side closes,
so keys typed just before `~.` reach tmux). **`grovy shell MACHINE`** is the same relay on `POST /api/shell`, under the
same `GROVY_TERMINAL=on` (the user's choice: a terminal on a session already lets you run anything there): the graft
runs `tmux new-session -A -s grovy_shell -c ~` (your login shell, in a plain tmux session that isn't a grovy one,
so a dropped link leaves it running and the next `grovy shell` joins it). `grovy shell trunk` on a graft is
`ssh -t TRUNK exec tmux new-session -A -s grovy_shell` with your usual ssh. A TERM the graft has no terminfo entry for (kitty,
ghostty...) becomes `xterm-256color` (tmux would refuse it). The child's environment and argv are built before the
fork (`execvpe`); when the relay ends the pty is closed first and the tmux client gets SIGHUP, then SIGKILL after 3s
(a hung tmux server can't keep the thread and its connection slot; the pty is non-blocking, and input tmux
doesn't read within 10s ends the relay). On the trunk, `~.` at the start of a line ends
it from there, as in ssh (`~~` sends a `~`; what was typed before it is still sent; a paste holding a newline
then `~.` ends it too, as in ssh); SIGTERM ends it cleanly; an end tmux didn't clean up after (`~.`, a
dropped link, a signal) resets the terminal (alternate screen, mouse, bracketed paste, cursor) and says why. The
size is the terminal's own (`os.get_terminal_size` on stdin). Only with `GROVY_TERMINAL=on` in the graft's
environment (setup asks a graft, default yes, and writes it into the unit): it is a full terminal (tmux runs
commands), so whoever holds the token, i.e. the trunk, can then run anything as you on the graft. Off, the trunk
still lists, starts, stops and messages the sessions, as the phone does.

**Setting one up (`grovy setup`, role "graft", `grovylib/graft.py`).** In setup's words the hub is the *trunk* and a
peer is a *graft*. Setup on the PC: picks the trunk (Host entries of `~/.ssh/config`, or a typed `user@host`) and
probes it over your usual ssh (`grovy peer -h` there: missing or too old grovy is said so before anything changes),
asks the name (the short host name by default), and plans the graft's own grovy on 127.0.0.1 (no HTTPS) plus: a
dedicated key `~/.ssh/grovy_tunnel` (ed25519, no passphrase: the service has no agent), one ssh run on the trunk
(`graft.ADD_SCRIPT`: this grovy's token and the public key on stdin, never in argv; it replaces any line holding the
same key in `~/.ssh/authorized_keys` and appends
`restrict,port-forwarding,permitopen="127.0.0.1:9",permitlisten="127.0.0.1:9",command="rm -f SOCK; echo SOCK" KEY
grovy-tunnel:NAME` (`permitlisten` stops the key opening TCP listeners on the trunk; a Unix-socket `-R` isn't limited
by it, see "Trust"; it needs OpenSSH 7.8 or newer on the trunk, whose older sshd rejects the whole line), makes the socket's folder 0700, runs `grovy peer add NAME --socket SOCK`, prints the path. The
file is edited by `edit_keys` (shared with the removal): adding drops the lines holding this key, or ending in
`grovy-tunnel:NAME` (another computer that had the name); removing drops only this key's line (the name may belong to
the computer that replaced this one by now), or the name's line when the key is gone here; comparing awk fields exactly on bytes (`LC_ALL=C`: grep
in a UTF-8 locale silently dropped lines that weren't valid UTF-8), writes a temporary file in `~/.ssh`, and puts it
in place (`mv`, or written through a symlink) only if the line counts add up; any failure leaves the file as it was.
An empty key, and a socket path outside `[A-Za-z0-9/._-]`, are refused. Then the
the `grovy-tunnel` user service (`grovy peer tunnel TRUNK --name NAME --port PORT --identity KEY`, `Restart=always`).
The forced command *is* the tunnel's preparation for that one socket (remove a stale one, print the path), so the
normal `grovy peer tunnel` works with the restricted key whatever the trunk's `StreamLocalBindUnlink`. Setup's own
logins share one ssh connection (`ControlMaster`, a private temporary `ControlPath`, closed at exit), so a password
is asked once. It ends by polling `grovy peer ls` on the trunk until the graft shows online (30s), else it says where
to look. `grovy cleanup` on the graft runs `graft.REMOVE_SCRIPT` on the trunk (drops the key's line, `grovy peer rm
NAME`; if the trunk can't be reached, a trunk that may be gone for good, it warns and says what to run there, and
goes on: the key is deleted here, so the line left there is dead) and deletes the key it made. Pointing a graft at another trunk takes it off the old one first; setup as a trunk on a
former graft stops and removes the tunnel and takes it off its trunk (`graft.leave`). A trunk that can't be reached
then is kept in the record (`old_grafts`, one entry per trunk and name, dropped once reached) for cleanup; a graft
set up before the record knew its trunk is read back from its unit. busybox awk splits a line at a NUL byte (gawk,
mawk keep it). Limits: the trunk's login shell must be sh-like (a csh can't
parse the multi-line `sh -c '…'`); with `ProxyJump`, `--identity` doesn't reach the jump host and the service has no
agent, so the tunnel fails even though setup worked.
Checked against OpenSSH 10.0 (a private sshd, default `StreamLocalBindUnlink no`): a command asked for over the key
runs the forced command instead, `-L` is refused, and the tunnel comes up over a stale socket.


One grovy (the **hub**) can show and command the sessions of grovy daemons on other machines (**peers**, e.g. a PC
with no public address) next to its own. All of it is `grovylib/peers.py`. Its state (each link, the peer's last
session and page lists) is not `Store` state: it lives in `Peers`/`Peer` under their own locks, and they call
`store.publish` / `store._notify` only without holding them, never under `Store.lock`.

```
PC: grovyd (127.0.0.1:7333) ◄── ssh -R ──── hub: ~/.local/state/grovy/peers/pc.sock ◄── HTTP ── hub grovyd ◄── phone
     grovy peer tunnel HUB      (the PC dials out)          (0600, folder 0700)        token of the PC
```

* **Every machine runs its own, unchanged grovyd.** Sessions, hooks, permissions, containers stay where they run, and
  a peer keeps working when the link is down (its own phone link included). Nothing peer-specific runs on the peer
  except the tunnel and `?notify=1` below.
* **Transport.** `grovy peer tunnel HOST [--name pc] [--remote-socket PATH] [--port 7333]`, run on the peer, loops
  forever: over ssh it first runs `sh -c '…'` on the hub to make the socket's folder (0700), remove a stale socket
  (sshd only binds over one with `StreamLocalBindUnlink yes`, which it usually lacks) and print the socket's absolute
  path; then `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o BatchMode=yes
  -R SOCK:127.0.0.1:PORT HOST`; when ssh ends it waits 2s, 4s, … up to 60s (reset after a minute up) and starts again.
  The tunnel's ssh reads your settings for HOST through `ssh -G HOST` written to a temporary `-F` file, minus the
  forwards (`RemoteForward`, `LocalForward`, `DynamicForward`, `ForwardAgent`, X11), connection sharing (`ControlMaster`/
  `ControlPath`), `RemoteCommand`/`RequestTTY` and, with `--identity`, your `IdentityFile`s: a forward meant for your
  own logins (a gpg-agent socket in `/run/user/…`) fails on the hub, and with `ExitOnForwardFailure` that ended every
  connect (`ClearAllForwardings` can't be used: it drops the tunnel's own `-R` too). `ProxyJump` becomes a
  `ProxyCommand` (`ssh -W`, your usual config for the jump hosts). If `ssh -G` fails, your config is used as it is.
  The other logins that run one command (setup's, `prepare`, `trunk:NAME`) pass `ClearAllForwardings=yes` and
  `RemoteCommand=none`.
  `--no-prepare` skips the remote shell entirely (needed with a key restricted to forwarding, below): then
  `--remote-socket` must be the absolute path, its folder made once by hand (`mkdir -p -m 700`), and a stale socket is
  left to the hub's sshd (`StreamLocalBindUnlink yes`, which sashimi has).
  Argument arrays throughout; the script is `shlex`-quoted for the remote shell, `~/` becomes `"$HOME"/`, a host that
  starts with `-` is refused. It's a **Unix socket, not a TCP port**: the hub is shared with other users, and a
  loopback port would be open to all of them; sshd creates the socket 0600 (`StreamLocalBindMask` 0177). The default
  socket is `${XDG_STATE_HOME:-~/.local/state}/grovy/peers/NAME.sock`, the same formula `grovy peer add` uses; paths
  over 100 bytes are refused (AF_UNIX).
* **Configuration.** `~/.config/grovy/peers.json`, `{"pc": {"socket": "/abs/pc.sock", "token": "…", "tls": false}}`,
  written by `grovy peer add NAME [--socket P] [--tls]` (the token is read from stdin, never argv: `ps` is visible to
  other users; on the peer `grovy peer token` prints it), `grovy peer rm`, `grovy peer ls` (with live state from the
  daemon, socket message `peers`). The file must be 0600 or it is refused whole. Names: `[a-z0-9][a-z0-9_-]{0,15}`.
  grovyd re-reads it every 10s (its reconcile loop), starting and stopping links; no restart needed. Without the file
  (or with no usable entry) `store.peers` is empty and **every route behaves exactly as before**.
* **Talking to a peer.** HTTP/1.1 over the socket (`http.client` with a Unix-socket connection; `tls: true` wraps it
  unverified for a peer that serves HTTPS: ssh already secures the link), `Authorization: Bearer <peer token>`,
  `Host: localhost`, as the phone would. Before connecting, the socket must be a socket owned by this user in a folder
  others can't write to (unless sticky): otherwise another user could stand in for the peer and be handed its token.
  The peer's token is only ever sent to the peer: the hub builds every request itself and forwards none of the
  phone's headers except `X-Request-Id` (so the peer's own retry protection applies too).
* **Names and ids.** The peer's session `foo` is `pc:foo` on the hub; `:` is not valid in a session name, so it can't
  collide with a local one, and a name without `:` never goes to a peer. The ids the phone acts on are prefixed the
  same way: permission requests and queued prompts (`pc:<hex>`), pages (`/p/pc:<hex>`, the `Page` event's
  `data.page`, the keys of `detail.pages`), share links (`/s/pc:<hex>`), offered downloads (`/xfile/pc:<hex>`).
  Routing is therefore stateless: the prefix says where to go. Rewriting is explicit per answer shape
  (`rewrite_detail`, `rewrite_event`, …): event ids stay numbers (history paging), and nothing inside a tool's input or
  a message is touched, so what the phone shows for approval is exactly what the peer sent. URL segments are
  percent-decoded first (`encodeURIComponent` sends `pc%3Afoo`).
* **What is proxied** (`Peers.handle_get` / `handle_post`, called first in `do_GET` / `_route_post` only when peers
  exist): a session's detail, history, pages and commands; prompts, interrupt, rename (within its machine), restart,
  resume, kill, forget; permission and question answers; prompt cancel; page view/share/delete and shared pages;
  attachment and image thumbnails (only `image/png|jpeg|gif|webp`, re-served with `default-src 'none'`); uploads
  (streamed to the peer, never held in memory); offered downloads (streamed, re-served as `attachment`
  octet-stream); an issue's status/finish/discard; plugin routes whose `session` (query or body) is a peer's (sent to
  the peer's plugin of the same name; GETs still need `X-Grovy`); new sessions (`POST /api/sessions` with name
  `pc:NAME`), with `/api/projects?peer=pc` and `/api/conversations?…&peer=pc` for the ＋ sheet. A peer's answer keeps
  its status code, its error text gets `pc: ` in front; a peer that refuses the token gives 502 (never 401, which the
  phone would take for its own login), an unreachable one 502 `pc is offline (last seen …): no tunnel (is grovy peer
  tunnel running on pc?)`. Answers are capped at 16 MB. Requests time out after 20s.
* **Pages from a peer** are served by the hub with the hub's own `PAGE_CSP` (and `X-Frame-Options: SAMEORIGIN`), only
  if the peer says `text/html`; the peer's headers are dropped. Shared ones (`/s/pc:<hex>`) need no login, exactly
  like the hub's own, and work while the peer is reachable.
* **Live updates.** grovy's event stream carries no events, only "session X changed" hints (`{"session": X}`), and the
  page re-fetches; so there are no event ids, and nothing to replay (`Last-Event-ID` doesn't apply). Per peer the hub
  holds one stream, `GET /api/events?notify=1`, and a refresher thread. A hint marks the session dirty; the refresher
  fetches the peer's session and page lists (hints arriving meanwhile coalesce into the next fetch), stores them,
  and only then tells the phones `pc:X` changed, so a phone that re-fetches sees the new state. When the stream says
  `hello` (it is subscribed: nothing after this can be missed), the hub re-fetches everything and then publishes
  `null` (phones re-fetch everything). A generation counter, bumped on every connect and disconnect, drops a fetch
  that started on an older link, so a list fetched just before a drop can't bring sessions back as idle. At least
  every 60s by the clock (steady hints don't put it off) the refresh is a full one and phones re-fetch everything,
  which repairs a hint the peer dropped (its per-stream queue holds 100). A list that isn't a list is a failed
  refresh, not an empty machine. A failed refresh is retried on the same link after 1s, 2s, … up to 30s, and
  forgotten if the link dropped meanwhile (the next `hello` re-fetches everything). The
  stream times out after 45s without data (the peer pings every 15s); it reconnects after 1s, 2s, … up to 30s. Both
  threads catch every error, log it and carry on (offline / `stale`), so a peer's odd answer can't freeze its state.
* **Reachability.** Each peer is `connecting` (no fresh list since the link came up), `online`, `stale` (the stream is
  up but 3 list fetches in a row failed: a peer whose token changed answers its stream until it restarts, but not the
  lists) or `offline` (with the reason and when it last answered). `/api/sessions` gains `host` (this machine's short
  name) and `peers` (`{name, state, last_seen, error}`), only when peers exist. Unless `online`, a peer's last known
  sessions are listed with state **`unknown`**, `accepts_input: false`, no pending count or background work, and
  "pc is offline (last seen 3 min ago)" or "pc isn't answering: …" as their activity: never `idle`. A request for a
  peer's session is still tried on the socket whatever the state (the tunnel may be back before the stream notices);
  only when that fails does it answer 502 with the reason.
* **Notifications.** On the peer, a stream opened with `?notify=1` (a hub) also receives `{"notify": {session, kind,
  text}}` at each `Store._notify` (browsers' streams never do, so they are unchanged); it is counted apart from the
  browsers at the same address in the per-client stream cap (through the tunnel that address is 127.0.0.1). The hub
  passes each one to its own `_notify` as `pc:NAME`: `GROVY_NOTIFY_CMD`, and plugins' `notify` callbacks. After the link was down, a session that now waits on you and didn't before gets one "needs you (while the
  link was down)" notification, read from the peer's state. At most 20 per peer per minute of each class are passed
  on (the rest are dropped and logged): "needs you" has a budget of its own, so a burst of "finished" can never
  swallow a permission request; and a peer removed or replaced in peers.json delivers nothing more, even from a stream that was
  still connecting.
* **UI.** A peer's session shows its machine as a small label before its name (list, sidebar, switcher, title bar);
  the home screen says which peers are offline or connecting; ＋ asks which machine (when peers exist) and lists that
  machine's trusted folders and conversations. Rename keeps the machine. The route accepts `#/s/pc:NAME` (and its
  percent-encoded form, which notifications use). `grovy.session` is `pc:NAME` there; `extensions/widgets.js` sends a
  peer's images and files to the peer.
* **Trust: what a peer sends.** Everything from a peer is untrusted input, like Claude's output. Ids must be exactly 32
  hex digits, or the item is dropped (permissions, prompts, pages, the keys of `detail.pages`, a `Page` event's page,
  share tokens): an id goes into URLs the phone builds, and `x/../../sessions/main/kill` would otherwise turn a tap on
  Allow into another request on the hub (the page also `encodeURIComponent`s every id it puts in a path). Session
  names must be valid; events need a numeric id and a type; every summary field the page reads is type-checked
  (unknown states become `unknown`); any answer that still doesn't fit gives 502 "pc answered something unexpected".
  The pre-login `/s/pc:…` route answers a plain 404 for every failure (no peer names, reachability or errors).
* **Trust: what the link gives a peer.** Through grovy, nothing: the hub connects to the peer, never the reverse, and
  no route lets a peer reach the hub's sessions, files or plugins. **The SSH login is another matter.** By default
  `grovy peer tunnel` logs in to the hub with an ordinary key and runs `sh -c` there, so whoever controls the PC (or
  its key) has a shell on the hub as you. A key restricted in the hub's `~/.ssh/authorized_keys` narrows that; checked
  with OpenSSH 10.0 (a private sshd as this user, `StreamLocalBindUnlink yes` like sashimi's):

  `restrict,port-forwarding,command="/bin/false",permitopen="127.0.0.1:9" ssh-ed25519 AAAA… pc-tunnel`

  with `grovy peer tunnel HUB --no-prepare --remote-socket /home/you/.local/state/grovy/peers/pc.sock`.
  * no shell or command (the forced command runs instead; `-N` asks for none), no agent or X11 forwarding, no pty;
  * `-L` to the hub's TCP ports **and** to its Unix sockets (e.g. grovyd's own) is refused once any `permitopen` is
    given (here a port nothing should listen on); with `restrict,port-forwarding` alone both still work;
  * `-R` to a Unix socket **is not limited by path**: `permitlisten` only applies to TCP. The key can bind a socket
    anywhere the account can write, and with `StreamLocalBindUnlink yes` **replace an existing one**, e.g. the hub
    grovyd's own `grovyd.sock` (then the hub's sessions' hooks, permission requests included, would talk to the PC).
    Only the hub's sshd configuration could narrow that (e.g. a `Match` block); grovy can't.

  So treat a PC with a tunnel key as able to act as you on the hub's grovy: give the key to machines you trust as much
  as the hub itself, and prefer the restricted entry above over a plain key.

## Out of memory

systemd-oomd kills a whole cgroup once memory pressure in your user manager (`user@UID.service`, limit 50% for 20s
on Debian) stays high, and each tmux pane is its own cgroup (`tmux-spawn-….scope`). So when a big job elsewhere ran
the machine short, oomd killed the Claude sessions, one after another, with everything they had started.

* **Shield.** The reconcile loop moves each session's Claude (the pane's process: tmux execs it directly) into its
  own scope, `grovy-NAME-PID.scope`, created with `ManagedOOMPreference=avoid`. It does this through D-Bus
  (`busctl --user call … StartTransientUnit`, what `systemd-run --scope` does). oomd then picks that scope only when
  no other candidate is left. Verified with systemd 257: the cgroup gets `user.oomd_avoid=1`, and oomd honours it
  because the scope and the monitored `user@UID.service` have the same owner. Setting the property later
  (`systemctl set-property`) does **not** set the attribute; it has to be given at creation. Sessions already running
  are moved too (their already-running children stay where they were), and a resumed session's new Claude is moved
  on the next pass. Without `busctl`/`journalctl` or a user manager, nothing is done.
* **Say so.** The session remembers its scopes. When its tmux session vanishes, the daemon asks the user journal
  (`journalctl --user -u SCOPE`) whether the OOM killer took it, and if so records a `Killed` event ("killed by the
  out-of-memory killer"), which the chat shows and the notifier sends.
* **Limitation.** What Claude runs directly is in its scope and shares the protection. A memory-hungry job started
  that way is only killed once nothing else is left (another user process may go first). Start heavy jobs in their
  own scope, ideally capped: `systemd-run --user --scope -p MemoryMax=40G CMD`. Then the kernel kills just that job
  when it goes over, and oomd prefers it over every session.

## Failure modes

| scenario | behaviour |
|---|---|
| SSH disconnects | nothing happens; Claude lives in tmux |
| the machine runs short of memory | see "Out of memory" below: sessions are the OOM killer's last choice, and a session it kills says so |
| phone sleeps / changes network | SSE reconnects automatically; on wake the page re-fetches. Drafts are kept in localStorage |
| someone without the token holds idle connections | at the 64-connection cap the longest-idle not-yet-requested connection is closed, and an address may hold 8 such at once; real requests still get through |
| daemon restarted | sessions untouched; state reloaded and reconciled against tmux; `needs_input` stays `needs_input` (Claude falls back to its terminal dialog); in-flight prompts → `unconfirmed`; a "type anyway" consent never survives a restart |
| daemon crashes during a permission | hook sees EOF → no output → terminal dialog |
| Claude exits / tmux killed by hand | `SessionEnd` or reconciliation → `exited`; pending permissions expire; queued prompts → `failed` |
| Claude dies at once on start (bad flag, unresolved `--resume` id, a `GROVY_CLAUDE` wrapper that fails its own setup) | the tmux session is already gone before `start_session` tags it, so `set-option` would fail with "no such session"; that's swallowed (only a tagging failure on a still-live session is raised) and `check_started` reports "claude exited immediately; to see why run: …" instead |
| resumed with a new Claude session_id | `SessionStart` updates the id; history stays under the grovy name |
| prompt while busy | queued, typed after `Stop` |
| two clients send prompts | each is queued (under a lock) and delivered one at a time, in order |
| two clients answer one permission | first wins, the other gets 409 |
| malformed hook JSON | ignored with a stderr diagnostic; exit 0; Claude unaffected |
| hook fires, daemon down | event lost (state will show stale/unknown until the next event); permission → terminal |
| truncated event log | bad lines skipped; a newline is added so the next record isn't corrupted |
| duplicate hook delivery | identical payloads are deduplicated (except permission requests, which are each a live hook) |
| you type in the terminal while the page is open | hooks report it like any other prompt; the page follows |
| a peer's tunnel drops (PC asleep, network change) | its sessions show `unknown`, "pc is offline (last seen …)" on the home screen, actions answer 502; the PC keeps working on its own; the tunnel and the hub reconnect by themselves and the hub re-fetches everything |
| a peer's grovyd restarts | the stream ends: offline, then online with a fresh list once it's back |
| the hub's grovyd restarts | the peer is unaffected; the hub reconnects at start (peers.json is read then) |
| stale socket left on the hub | removed by `grovy peer tunnel` before each connect |
| a peer's token changed | stream answers 401: offline with "wrong token?"; actions say to run `grovy peer token` / `grovy peer add` again |
| peers.json readable by others | refused whole (logged, and shown by `grovy peer ls`) |

## Known limitations

* **Issues merge into the default branch only**, by fast-forward: a branch that must be rebased needs Claude (or you)
  first. An issue's conversation stays resumable from ＋ but its folder is gone once it's finished.
* **Prompt injection is keystrokes into a TUI.** If you've half-typed something in the terminal input, the
  web prompt is appended to it. grovy can't see that without scraping the screen, which it deliberately doesn't do.
  If a dialog that emits no hook is open (`/model`, `/config`, a rewind menu, the workspace-trust prompt…), the
  keystrokes go to that dialog. grovy only types in hook-confirmed idle states, and every typed prompt must be
  confirmed by `UserPromptSubmit`, but a non-zero window remains. Use the terminal for anything unusual.
* **Workspace trust.** Claude holds back *all* hooks until a new folder's trust dialog is accepted. ⋯ → New project
  answers it for the projects it makes; for any other git repo you haven't opened in Claude before, `grovy attach`
  once and accept it. Until then the session shows `starting`. The phone only offers folders that won't hit the
  dialog. Answering it relies on the dialog's wording: if a Claude Code update changes it, grovy presses nothing and
  says to answer in the terminal. A clone left on the trust sheet when the daemon restarts stays on disk.
* **After answering a dialog in the terminal**, state can be `unknown` until Claude's next hook event
  (e.g. its `idle_prompt` notification); see the "type anyway?" confirmation above.
* Not every dialog emits `PermissionRequest` (e.g. MCP elicitations, plan approval). Those show as
  "needs input in terminal" at best. The page doesn't pretend to handle them.
* Events emitted while the daemon is down are not replayed.
* **Peers** (other machines, see "Other machines"):
  * ⋯ → New issue and ⋯ → New project work on the hub only (an issue session that already exists on a peer can be
    finished or discarded from the hub). ⋯ → Extensions, ⋯ → Theme, the login link, `custom.css`/`custom.js` are the hub's.
  * The hub's plugins see peers' sessions only through `notify`; `grovy.on("event")`,
    `grovy.on("state")`, `grovy.sessions()`, `grovy.session()` and `grovy.send()` cover local sessions only. The hub's
    `grovy` command (`ls`, `attach`, `logs`, `kill`, ...) is local too: the terminal of a peer's session is on the peer.
  * Notifications sent while the link is down are lost; on reconnect only "needs you" is made up for (from the peer's
    state). A "finished" or "failed" during the gap shows in the list, not as a notification.
  * Timestamps are the peer's: the clocks are assumed in sync (NTP). The phone keeps the hub's clock offset.
  * A peer running an older grovy works, without its notifications (it ignores `?notify=1`).
  * A peer's shared page link goes through the hub and works only while the peer is reachable.
  * No TLS on the hop from the hub's socket to the peer's grovyd: ssh carries it; `tls: true` is for a peer whose grovyd
    only serves HTTPS, unverified.
* Steps come from Claude Code's transcript file, whose format is undocumented. If it changes, steps silently
  stop appearing (unknown content is skipped, never guessed) and only the final answers show. They arrive per
  finished block (about 1s later), not as streamed text. A first block written before the daemon handled
  `UserPromptSubmit` (a race of milliseconds against the model's first reply) would be missed. Turns from before
  this feature have no steps, and a subagent's own text isn't shown (its `Agent` call is one step).
* Multi-select answers are sent as comma-separated labels. Claude receives them as text, which works, but the
  terminal's own encoding for multi-select hasn't been compared.
* Typing a long prompt takes about 1s per 3,000 characters. Anything typed in the terminal at the same moment
  interleaves with it.
* **A page can still navigate its own frame** (e.g. `location = "https://…?data"`): browsers offer no way to block
  that, so a page that wants to could carry what it shows to another site. Pages are made by your own Claude
  sessions, but a session that read hostile content could be steered into it. The viewer's title bar stays grovy's.
* Live widgets come from any process that can reach the daemon's socket (any process of yours, as with pages). A
  card shows the key and content it's given; nothing in it is run or sent to Claude.
* Pages are snapshots: they can't load data at view time, so Claude embeds the data and republishes to update.
* The CDN allowlist limits where code comes from, not what it does: jsDelivr and unpkg serve any npm package or
  GitHub file. What keeps a page's data in is `connect-src`/`img-src` plus the sandbox, and self-navigation
  (above) gets around them.
* A shared page's link is its only protection, over plain HTTP: anyone who sees the link, or the traffic on the
  network, can open it until you stop sharing.
* HTTPS with `grovy tls init`: each device installs `grovy.crt` by hand, again after every run. Firefox on Android
  ignores installed certificates unless "Use third party CA certificates" is on in its secret settings. Untested on
  real phones so far, name constraints on iOS in particular. HTTP and HTTPS can't both be served normally: once HTTPS
  is on, plain HTTP only gets the page pointing to it.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

These cover name validation, hook-adapter output (malformed input, daemon down, decisions), state
transitions, duplicate events, event-log recovery, restart recovery, permission
decide/duplicate/concurrent/timeout/hook-gone/session-exit, the prompt queue (busy, permission
pending, one in flight, no retry, concurrency, isolation, failure, force), live widgets (in-place updates,
persistence, throttling, the client library's no-op/failure/exit behaviour, and the CLI over a real socket), and HTTP
validation/CSRF/token/SSE. The integration tests run a fake `claude` in **real tmux** on a private
server (`tmux -L`) to check lifecycle, literal delivery of quotes, `$()`, backticks, unicode and multiline text,
isolation between sessions, and that an unrelated look-alike session is never touched.

`tests/test_peers.py` links a hub to a real peer daemon (fake tmux) through a Unix-socket forwarder standing in for
the SSH tunnel, and cuts and restores it: list, detail, history, prompts, queued-prompt cancel, permission answers,
interrupt/kill/resume/create, rename, live hints, notifications, offline `unknown` state and 502s, recovery with the
"needs you" catch-up, stale-fetch protection, pages under the hub's CSP and shared links, streamed uploads and
thumbnails, plugin routes, a wrong token, hostile names from a peer, removing a peer, peers.json validation and mode,
socket ownership checks, the tunnel's ssh argv, its remote script (run by a real `sh`, quoting included), backoff and
`--no-prepare`, and that nothing changes without peers.json. `TestHostilePeer` plays a peer that sends what grovy
never would: path-traversing and newline-ended ids, wrong types everywhere, a refresher that throws, lists failing on
a live stream (`stale`), a lost hint (periodic resync), a refresh in flight when the link drops (generation guard),
the pre-login `/s/` route, upload errors, notification floods and a closed peer. `TestPeerRound2`: a "finished" flood followed by a
permission request, the clock-based resync under steady hints, a non-list list, no retry loop while the link is down.

**Peers by hand** (no second machine needed): two scratch daemons (`--port 7397` hub, `7398` "pc", own state and
config dirs), `grovy peer token` (pc) piped into `grovy peer add pc --socket HUBSTATE/peers/pc.sock` (hub), and
instead of ssh a forwarder from that socket to 127.0.0.1:7398 (or `grovy peer tunnel` with a fake `ssh` on `PATH`
that runs the remote script locally and the forwarder for `-R`). Drive hooks on the pc with
`GROVY_STATE_DIR=… grovy hook NAME < payload.json`; stop the forwarder to go offline.

### Manual end-to-end procedure (real Claude Code)

1. `grovy serve` in one terminal; `grovy new demo /tmp/demo -- --permission-mode default`. If it stays
   `starting`, `grovy attach demo` and accept the trust dialog.
2. Open the page and check that `demo` becomes `idle`.
3. Send `Reply with exactly: pong` plus a second line containing `"quotes"` and `$HOME`. Expect a `queued`/typed
   bubble, then the user bubble from `UserPromptSubmit`, then `pong`.
4. Send `Use the Bash tool to run: touch x`. Expect a permission card with the exact command and the
   dialog also visible in `grovy attach demo`. Tap **Allow once** and confirm the file exists.
5. Repeat with `rm x`, but answer **No** in the terminal. The card should disappear (`expired`).
6. Stop the daemon mid-permission. The terminal dialog should remain answerable. Restart the daemon; the session is still there.
7. Close SSH, reopen, `grovy attach demo`.

Steps 1–5 were run against Claude Code 2.1.325 during development.
