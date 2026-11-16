# grovy

Run Claude Code on your computer and keep working from your phone: see what each session is doing,
approve or deny what Claude wants to run, answer its questions, and send it messages, from a web page
on your home network or VPN. Nothing to install on the phone.

## Install

You need Linux (or macOS), Python 3.11+, tmux, and Claude Code signed in.

```sh
git clone <this repo> ~/grovy && ~/grovy/grovy setup
```

Setup asks one question (which network address your phone will use; it suggests your Tailscale address if
you have one), installs grovy as a background service that starts at boot, and prints a link. **Open that
link once on your phone**: it logs the phone in. Bookmark the page or "Add to Home Screen".

On macOS, or with `--no-service`, setup prints a command to start grovy yourself instead.
`grovy setup --dry-run` shows what it would do without changing anything; rerunning it is safe.

**One-time per project:** Claude asks whether you trust a folder the first time it runs there, and grovy can't
see a session until that's answered. So run `claude` once in a new project on the computer (or `grovy attach`
the session and answer it). The phone only offers folders that are already trusted.

**Updating:** `cd ~/grovy && git pull && systemctl --user restart grovy`. Your sessions keep running. To give a
running session the newest grovy, use **⋯ → Update session** on the phone.

## What it is

Each Claude session runs in its own tmux session on your computer, exactly as if you had started `claude`
in a terminal. grovy listens to Claude's hooks and shows everything on a phone-friendly page:

- **Sessions:** a list with live status (working, idle, needs you), context used, and the last activity.
- **Chat:** Claude's replies, rendered properly (tables, code, lists), plus what's happening now.
- **Approvals:** when Claude wants to run a command or edit a file, you see the full command or diff and
  tap Allow or Deny. Questions Claude asks you show up as cards you answer with a tap.
- **Messages:** type to Claude; messages wait in a queue while Claude is busy. `/` lists the skills and
  commands that work from the phone.
- **Start, resume, rename, stop:** start Claude in any trusted folder, continue or fork a past conversation,
  stop sessions and resume them later.
- **Widgets and pages:** Claude can answer with charts, checklists, diagrams or buttons to pick from, and
  publish full HTML pages (reports, dashboards) that open full-screen. A page can be shared on your network
  with a link.
- **Steering:** a message you send while Claude is working reaches it at its next step, so you can correct course
  without waiting or interrupting.
- **Live progress:** when Claude runs something long (a build, training, a batch job), its script can show a progress
  bar, notes or the latest output lines in the chat while it runs. Sessions started before you updated grovy get
  this after ⋯ → Update session.

The terminal never goes away: `grovy attach NAME` puts you in the real session (detach with Ctrl-b d).

## On the computer

```sh
grovy new api ~/src/api        # start Claude in a folder (also from the phone: +)
grovy ls                       # sessions and their state
grovy attach api               # the real terminal
grovy resume api               # continue an ended session
grovy kill api                 # stop it
grovy url                      # the phone link again
grovy doctor                   # check that everything is in order
```

## Make it yours

- **Looks:** edit `~/.config/grovy/custom.css`. It starts with the colour names you can change (your message
  bubbles, accents, backgrounds, in light and dark mode). Reload the page to see it. It's outside the grovy
  folder, so updates never overwrite it.
- **Anything else:** grovy is three small files, with no dependencies. The easiest way to change it is to ask
  Claude, in a grovy session opened on the grovy folder: it knows how to change grovy safely while you use it.

## Safety

- The page needs the login link (a secret token); without it, nothing is visible. Only expose grovy on a home
  network or VPN you trust: it uses plain HTTP.
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
| "claude not found" when starting from the phone | rerun `grovy setup` from a terminal where `claude` works |
| "killed by the out-of-memory killer" | the computer ran out of memory, usually because of a big job; Resume continues. Start heavy jobs with a cap, e.g. `systemd-run --user --scope -p MemoryMax=40G …` |
| "state unknown" | Claude did something the page couldn't follow (e.g. you answered in the terminal); the next send asks first |
| anything else | `grovy doctor`, then `journalctl --user -u grovy` |

**Uninstall:** `systemctl --user disable --now grovy`, then delete `~/.config/systemd/user/grovy.service`,
`~/.local/bin/grovy`, `~/.config/grovy` and `~/.local/state/grovy`.

## For developers

How it works, the full security model, failure modes, limitations, settings and tests: [`docs/DESIGN.md`](docs/DESIGN.md).
Rules for changing grovy (for people and Claude): [`CLAUDE.md`](CLAUDE.md).
