# grovy

Run Claude Code on your computer and keep working from your phone: see what each session is doing,
approve or deny what Claude wants to run, answer its questions, and send it messages, from a web page
on your home network or VPN. Nothing to install on the phone.

## Install

You need Linux (or macOS), Python 3.11+, tmux, and Claude Code signed in.

```sh
git clone <this repo> ~/grovy && ~/grovy/grovy setup
```

Setup asks a few questions (use arrow keys and enter), then shows everything it will change and waits for
your yes:

* **Trunk or graft.** A *trunk* is a grovy of its own: pick it unless you already have grovy on another
  machine. A *graft* (worker) joins this computer to that trunk instead (see "Another computer").
* **Who can reach it.** "This computer only" is the default; to use grovy from your phone, pick your VPN or
  local network. The port is checked, and a free one suggested if 7333 is taken.
* **HTTPS**, if other devices can reach it: off by default (it means installing a certificate on each device;
  notifications need it).

It then installs grovy as a background service that starts at boot and ends with a **QR code**: scan it, or
open the link, once on each device to log it in. Bookmark the page or "Add to Home Screen".

Run it again to change your answers, show the login link, or remove grovy. On macOS, or with `--no-service`,
setup prints a command to start grovy yourself instead. `--dry-run` shows what it would do; `-y` takes the
recommended answers without asking.

**One-time per project:** Claude asks whether you trust a folder the first time it runs there, and grovy can't
see a session until that's answered. So run `claude` once in a new project on the computer (or `grovy attach`
the session and answer it). The phone only offers folders that are already trusted.

**Updating:** `grovy update`. It shows what's new, updates the grovy folder (only if it can do so cleanly), checks
that the new version works, and restarts grovy; your sessions keep running. To give a running session the newest
grovy, use **⋯ → Update session** on the phone.

## What it is

Each Claude session runs in its own tmux session on your computer, exactly as if you had started `claude`
in a terminal. grovy listens to Claude's hooks and shows everything on a phone-friendly page:

- **Sessions:** a list with live status (working, idle, needs you), context used, and the last activity.
- **Background work:** subagents and commands Claude left running show as "idle · 2 running"; tap the strip above the
  message box to see each one, how long it has run and what it is doing. Sessions started before you updated grovy
  get this after ⋯ → Update session.
- **Chat:** Claude's replies, rendered properly (tables, code, lists), plus what's happening now.
- **Approvals:** when Claude wants to run a command or edit a file, you see the full command or diff and
  tap Allow or Deny. Questions Claude asks you show up as cards you answer with a tap.
- **Messages:** type to Claude; messages wait in a queue while Claude is busy. `/` lists the skills and
  commands that work from the phone.
- **Start, resume, rename, stop:** start Claude in any trusted folder, continue or fork a past conversation,
  stop sessions and resume them later.
- **Math:** formulas Claude writes in LaTeX (`$x^2$`, `$$\sum_i x_i$$`) show as real math.
- **Widgets and pages:** Claude can answer with charts, checklists, diagrams, images or buttons to pick from, and
  publish full HTML pages (reports, dashboards) that open full-screen. A page can be shared on your network
  with a link.
- **Shared files:** Claude can hand you any file it made — a report, a CSV, a zip, a log — as a card with a Download
  button. The file isn't copied anywhere; grovy serves it straight from the project, just for you.
- **Steering:** a message you send while Claude is working reaches it at its next step, so you can correct course
  without waiting or interrupting (the button turns blue). With the box empty, the same button is ■ to stop Claude.
- **Quote and copy:** each reply has Copy and Quote buttons; or select part of it and tap Quote selection. Tap a quote
  to jump back to where it came from. "Worked for 2m · 14 tool calls" under a reply lists what Claude ran.
- **Long messages:** once a message outgrows the box, it takes the whole screen; the ⤢ / ⤡ button in its corner switches by hand.
- **Attachments:** tap the paperclip in the empty message box to send photos or files; Claude gets them as files it can open (kept for 7 days). Photos
  show in your message.
- **Live progress:** when Claude runs something long (a build, training, a batch job), its script can show a progress
  bar, notes or the latest output lines in the chat while it runs. Sessions started before you updated grovy get
  this after ⋯ → Update session.
- **Issues:** ⋯ → New issue starts Claude on one small fix in a copy of the project of its own (a git worktree, on a
  new branch). When it has committed, ⋯ → Finish issue shows the commits; one tap merges them into your main branch
  and cleans everything up.
- **New projects:** ⋯ → New project makes a blank repository or clones one into `~/dev` and starts Claude in it.
  For a clone, you see first which of its files Claude would load (some can run commands) and say whether to trust it.
- **Git:** ⋯ → Git shows the session's repository as a graph of branches and commits; tap a commit for its files
  and diff. Branch names and commit hashes in Claude's replies are links to them.
- **Notifications (with HTTPS):** ⋯ → Notifications on each phone, to hear when a session needs you, finishes or
  fails, even with grovy closed. The notification travels empty through your phone maker's push service; the phone
  then fetches what happened from grovy itself. On iPhone, open grovy from the home screen first (Share → Add to
  Home Screen).
- **On a computer:** on a wide screen your sessions and pages stay in a sidebar beside the conversation. Right-click a
  session, a page, a message or the conversation for what you can do with it. Ctrl+Enter sends, Alt+↑/↓ switches session, Esc closes, Tab
  reaches every button.

The terminal never goes away: `grovy attach NAME` puts you in the real session (detach with Ctrl-b d).

## On the computer

```sh
grovy new api ~/src/api        # start Claude in a folder (also from the phone: +)
grovy ls                       # sessions and their state
grovy attach api               # the real terminal
grovy resume api               # continue an ended session
grovy kill api                 # stop it
grovy url                      # the phone link again (also on the phone: ⋯ → Login link)
grovy doctor                   # check that everything is in order
```

In a terminal, `grovy ls` is a coloured table (sessions waiting on you stand out). Piped into another command, or
with `NO_COLOR` set or `--plain`, it prints the plain columns scripts can read.

`grovy setup` also sets up tab completion in bash (commands, options, session names); for a shell without the
bash-completion package, add `eval "$(grovy completion bash)"` to `~/.bashrc`.

## Another computer

One grovy (the *trunk*, or hub, say your server) can also show the sessions of another computer (a *graft*, or
worker, say your PC, even one with no public address), next to its own. Each computer runs its own grovy and keeps
working on its own; the PC connects to the trunk over SSH, so it needs no open port. Its sessions show as `pc:NAME`
with a small "pc" label, and ＋ asks which machine to start on.

1. Set up the trunk first (`grovy setup` there, "Trunk").
2. On the PC, run `grovy setup` and pick **Graft**. It asks which machine is the trunk (from your `~/.ssh/config`,
   or an address), checks it, and asks this computer's name there. It then makes a key just for the connection,
   adds it on the trunk (your usual ssh login, once; a password is fine), and runs the connection as a service that
   reconnects by itself. It ends by checking that the trunk sees the PC.

**Mind the key:** the trunk only lets that key keep the connection (no shell, no other commands), but treat the PC
as trusted all the same: details in `docs/DESIGN.md` ("Other machines").

On the trunk, grovy's commands work on the PC's sessions too: `grovy ls` lists them, and `grovy attach pc:NAME`
(also `logs`, `kill`, `resume`, `rename`, `new`) reaches one; on the PC, `grovy ls` lists the trunk's sessions too,
as `trunk:NAME`, and `grovy attach trunk:NAME` reaches one. A name alone works too when only one machine has it
(`grovy attach Tups`); when several do, grovy asks which. `grovy shell pc` (or `grovy shell trunk` on the PC) opens a shell on the other machine, in tmux: C-b d leaves
it running, and the next `grovy shell` picks it up. Setup asks whether the trunk may open a terminal on the PC
(`attach` and `shell`); without it, the rest still works.

`grovy peer ls` on the trunk says whether the PC is online. `grovy cleanup` on the PC removes it from the trunk too.
While the PC is off, its sessions show as "unknown" and the home screen says so. What works on the trunk only (new
issues, new projects) and other limits are in `docs/DESIGN.md` ("Other machines"). (By hand, without setup:
`grovy peer token | ssh HUB '~/.local/bin/grovy peer add pc'`, then `grovy peer tunnel HUB --name pc` on the PC.)

## Make it yours

- **Extensions:** ⋯ → Extensions on the home screen switches built-in parts on or off (math, widgets, git, issues...)
  for all your devices; reload to apply.
- **Looks:** edit `~/.config/grovy/custom.css`. It starts with the colour names you can change (your message
  bubbles, accents, backgrounds, in light and dark mode). Reload the page to see it. It's outside the grovy
  folder, so updates never overwrite it.
- **Your own buttons, shortcuts and widgets:** `~/.config/grovy/custom.js` can add items to the ⋯ menu, keyboard
  shortcuts, buttons under Claude's replies, or new kinds of widgets, and switch off or replace built-in parts such as
  the context bar. It starts with examples to uncomment (if you
  installed grovy before it existed, copy `examples/custom.js` there). Reload the page to see it. To tell Claude something in every session (for example how to use your new widget), write it in
  `~/.config/grovy/instructions.md`.
- **Behind the scenes:** a Python file in `~/.config/grovy/plugins/` can react to what your sessions do (log it, send
  it somewhere, answer in your place) and give your `custom.js` its own data. See `examples/plugin.py`.
- **Anything else:** grovy is plain Python and one HTML file, with no dependencies and nothing to build. The easiest way
  to change it is to ask Claude, in a grovy session opened on the grovy folder: it knows how to change grovy safely
  while you use it.

## Safety

- The page needs the login link (a secret token); without it, nothing is visible. Only expose grovy on a home
  network or VPN you trust: it uses plain HTTP unless you turn on HTTPS.
- **HTTPS (optional):** run `grovy tls init --name NAME` (the name or address your phone opens grovy by), then
  restart grovy. Copy the certificate file it names to each device yourself and install it (it prints how), then
  use the `https://` link from `grovy url`. Each new `grovy tls init` needs installing again.
- **Nothing is approved without you.** If you don't answer on the phone, Claude's normal question in the
  terminal is still there. Your own Claude permission settings keep applying.
- grovy only touches the sessions it started. Your other Claude windows and tmux sessions are left alone.
- Claude can publish pages but can't share them; only you can, from the phone. Pages run in a sandbox: they
  can't see grovy, your login, or send data out (one exception, and other limits, in `docs/DESIGN.md`).
- Each grovy session gets about 450 tokens of instructions about widgets and pages. `GROVY_WIDGETS=0` in
  grovy's environment turns that off.

## Troubleshooting

| what you see | what to do |
|---|---|
| a session stays "starting" | Claude is asking whether to trust the folder: `grovy attach NAME` and answer |
| the link doesn't open on the phone | grovy listens only on the computer: rerun `grovy setup` and pick another address |
| "grovy doesn't know the name …" | open it by IP address, or add the name to `GROVY_HOSTS` |
| the home-screen app says "login needed" | it doesn't share the browser's login (iPhone): paste the login link into the field on that page (⋯ → Login link on a logged-in device copies it) |
| "claude not found" when starting from the phone | rerun `grovy setup` from a terminal where `claude` works |
| "killed by the out-of-memory killer" | the computer ran out of memory, usually because of a big job; Resume continues. Start heavy jobs with a cap, e.g. `systemd-run --user --scope -p MemoryMax=40G …` |
| "state unknown" | Claude did something the page couldn't follow (e.g. you answered in the terminal); the next send asks first |
| "pc is offline: no tunnel" | on the PC, check that `grovy peer tunnel HUB` runs and what it prints; `grovy peer ls` on the hub |
| `grovy peer tunnel` says "remote port forwarding failed" | the hub's sshd forbids it (`AllowStreamLocalForwarding`), or the socket path is wrong: compare with `grovy peer ls` on the hub |
| anything else | `grovy doctor`, then `journalctl --user -u grovy` |

**Uninstall:** `grovy cleanup` undoes what setup did, after showing you the list. It keeps your own files in
`~/.config/grovy` unless you say otherwise, and running Claude sessions unless you ask it to end them.

## For developers

How it works, the full security model, failure modes, limitations, settings and tests: [`docs/DESIGN.md`](docs/DESIGN.md).
Rules for changing grovy (for people and Claude): [`CLAUDE.md`](CLAUDE.md).
