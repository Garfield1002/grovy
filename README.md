# grovy: Claude Code sessions, from your phone

A small session manager for Claude Code. Each session is a `claude` process in its own
tmux session. Claude Code **hooks** feed a small daemon that serves a mobile web page,
where you can watch sessions, read the last responses, approve or deny permission
requests, and send prompts. tmux is always the escape hatch: `grovy attach NAME`.

Python ≥ 3.11 standard library only. No database, no frontend build, no dependencies besides
`tmux` and `claude`.

```
claude ──hooks──► grovy hook NAME ──unix socket──► grovyd ──HTTP+SSE──► phone
   ▲                                               │
   └───────────── tmux paste-buffer ◄──────────────┘  (prompts typed only when known safe)
```

| file | what |
|---|---|
| `grovy` / `grovy.py` | CLI, tmux helpers, session registry, hook adapter |
| `grovyd.py` | daemon: state from hooks, permission broker, prompt queue, HTTP/SSE |
| `index.html` | the whole UI (vanilla JS, no innerHTML) |
| `tests/test_grovy.py` | unit + tmux integration tests (`python3 -m unittest discover -s tests`) |
| `examples/` | systemd unit, Telegram notify script, the generated hook config |

> **Renamed from `ccs`.** On first run, `~/.local/state/ccs` moves to `~/.local/state/grovy`, and a symlink is
> left behind for sessions started before the rename. `CCS_*` environment variables are still accepted, and
> `ccs`/`ccs.py` remain as aliases. Phones logged in with the old cookie stay logged in.

## Install

```sh
ln -s "$PWD/grovy" ~/.local/bin/grovy          # any dir on PATH
grovy doctor
```

**No changes to `~/.claude/settings.json` are needed.** `grovy new` writes a per-session
settings file (`$STATE/hooks/NAME.json`, see `examples/claude-hooks.json`) and starts
`claude --settings THAT_FILE`. Hook arrays from `--settings` are *merged* with your own
settings, so your existing hooks keep working. Claude processes not started by grovy are unaffected.

## Use

```sh
grovy serve                                  # daemon (or use the systemd unit below)
grovy new api ~/src/api                      # start Claude in tmux session "grovy-api"
grovy new web ~/src/web -- --model sonnet    # extra args after -- go to claude
grovy ls                                     # NAME / STATE / PATH
grovy attach api                             # the real terminal; detach with C-b d
grovy logs api -n 100                        # recent scrollback
grovy kill api                               # explicit; never touches non-grovy tmux sessions
grovy resume api                             # restart an exited session: claude --resume <last session_id>
grovy resume api --fresh                     # ...or start a new conversation in the same slot
grovy forget api                             # drop an exited session from the list
grovy rename api backend                      # rename (also while running; "api" stays reserved as its alias)
```

### Daemon as a service

```sh
cp examples/grovy.service ~/.config/systemd/user/   # edit ExecStart path + GROVY_BIND
systemctl --user daemon-reload && systemctl --user enable --now grovy
loginctl enable-linger "$USER"
```

Restarting the daemon never touches Claude: sessions belong to tmux.

### Phone access over the VPN

The daemon binds to **127.0.0.1 by default**. To reach it from the phone, bind it to the
machine's VPN address (Tailscale / WireGuard / …), never `0.0.0.0` on a machine with a public interface:

```sh
GROVY_BIND=100.101.102.103 grovy serve        # or 0.0.0.0 for every interface
grovy url                                   # prints http://ADDR:7333/?token=... : open it once on the phone
```

**A token is always required, loopback included**, because other local users and DNS-rebinding web pages can
reach 127.0.0.1 too. It's generated once into `$STATE/token` (mode 0600). `GROVY_TOKEN` overrides it. Opening the `grovy url` link sets an HttpOnly, SameSite=Strict
cookie for a year. Without it the page shows "login needed" and the API returns 401. To rotate the token, delete the
file and restart the daemon. `GROVY_AUTH=off` temporarily accepts requests without a token while still setting the
cookie from the link. That's useful for enrolling a phone whose only way to see the link is the grovy page itself.
Without TLS the token protects against other peers on the network, not an on-path observer; the VPN provides the
encryption. Cookies aren't isolated by port, so another web service on the same host name/IP that your phone visits
would also receive the cookie. Only browse services there that you trust.

**Host check:** requests are accepted only when the `Host` header is an IP address, `localhost`, or this machine's
host name. That defeats DNS rebinding, where a malicious page reaches the daemon through its own domain name. To use
another name, such as a MagicDNS name, add it to `GROVY_HOSTS` (comma-separated). Hook ingestion is **never** on the network: it uses a Unix socket with mode 0600 in the state directory.

"Add to Home Screen" on the phone gives it an app-like icon.

### Configuration (environment variables)

| var | default | |
|---|---|---|
| `GROVY_BIND`, `GROVY_PORT` | `127.0.0.1`, `7333` | HTTP listener (`grovy serve --bind/--port` too) |
| `GROVY_TOKEN` | `$STATE/token` | web UI token (auto-generated) |
| `GROVY_HOSTS` | unset | extra accepted `Host` names, comma-separated |
| `GROVY_AUTH` | `on` | `off` = don't require the token (enrollment only) |
| `GROVY_STATE_DIR` | `$XDG_STATE_HOME/grovy` → `~/.local/state/grovy` | all state, mode 0700 |
| `GROVY_PERMISSION_TIMEOUT` | `600` | seconds a permission waits for the phone (fixed per session at `grovy new`) |
| `GROVY_CLAUDE` | `claude` | claude executable |
| `GROVY_NOTIFY_CMD` | unset | command run with a short message on stdin (see below) |
| `GROVY_PUBLIC_URL` | unset | appended to notifications as a link |
| `GROVY_DEBUG` | unset | log HTTP requests |
| `GROVY_WIDGETS` | `1` | `0` = don't tell Claude sessions about widgets |

### Notifications (Telegram or anything else)

`GROVY_NOTIFY_CMD` runs, without a shell, for: *permission needed*, *needs input in terminal*,
*turn finished*, and *error*. Each message is one line, `[name] reason` (+ link), and never contains
command output or the full prompt. It is rate-limited to one per session per kind per 60s, and runs
in a background thread with a 15s timeout, so a failing bot never blocks hooks or Claude.
`examples/notify-telegram.sh` posts to an existing bot (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

## How it works

**Identity.** Three different IDs, kept apart:
* *grovy name* (`api`): stable; validated `[A-Za-z0-9][A-Za-z0-9_-]{0,31}`.
* *tmux session* `grovy-api`: tagged with the tmux option `@grovy=api` and always addressed exactly
  (`=grovy-api`). A session counts as managed only if it carries the tag, and tmux is the authority on whether it exists.
* *Claude session_id*: learned from `SessionStart`. It changes on `/clear`, `/resume`, etc., and the latest one is
  stored in the registry for `grovy resume`.

  The hook command line is `grovy hook api`, so events are attributed from argv, not from cwd.
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
| `Stop` | `idle`; `background_tasks` count shown (Claude accepts input meanwhile) |
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
* `unknown` state blocks automatic typing. Send then asks "type anyway?" and only that prompt is forced.

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
through `SessionStart(source=clear|compact)`. Typing `/` in the composer opens a picker of the allowed commands.

**Interrupt.** While a session is `working` with no pending dialog, the ■ button presses Esc (after a confirm). Esc
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
* **Session page.** The ⋯ menu has *Search this conversation*, *Rename* and *Stop session* (or *Remove from list*
  once it has ended; ended sessions also get a *Remove* button in the list). An exited session shows **Resume
  session**. Tapping the title switches between sessions. *Load earlier* pages back through the event log. Yes / No /
  Go ahead chips appear when Claude's last reply ends with a question.

**Renaming.** Works while Claude runs: the tmux session is renamed live, and the registry keeps the old name as an
*alias*. The running Claude keeps reporting its hook events under the old name until its next start (Claude reads its
settings file only at start), so the daemon routes alias → current name. Old history records are shown under the new name. An
alias can't be taken by another session until the renamed one is removed.

**Status info.** Each session's status line runs `grovy statusline NAME`. It forwards model, effort, fast mode and
context use to the daemon (shown under the session header and as "context N%" in the list), then prints your own
configured status line, so the terminal looks the same. Sessions started before this existed report it after their next
start.

**Widgets.** Claude's reply can contain ` ```widget ` blocks holding one JSON object: `chart` (bar, line, pie),
`stats`, `progress`, `checklist`, `choices`, `form` or `diagram`. The page draws them itself with DOM and SVG;
nothing from Claude is run or parsed as HTML. Invalid JSON or an unknown type shows "Widget not shown: <reason>"
followed by the raw block. `choices` and `form` only fill your message box (asking first if it already holds a draft);
you send it like any message. Claude learns the format from grovy's `SessionStart` hook (`additionalContext`, the
`WIDGET_GUIDE` text in `grovy.py`), on every start, resume, `/clear` and `/compact`. Hooks run the current `grovy.py`,
so a changed guide applies without a restart. `GROVY_WIDGETS=0` turns the guide off. The terminal shows the raw JSON,
so Claude is told to keep the key facts in plain text too.

## Failure modes

| scenario | behaviour |
|---|---|
| SSH disconnects | nothing happens; Claude lives in tmux |
| phone sleeps / changes network | SSE reconnects automatically; on wake the page re-fetches. Drafts are kept in localStorage |
| daemon restarted | sessions untouched; state reloaded and reconciled against tmux; `needs_input` stays `needs_input` (Claude falls back to its terminal dialog); in-flight prompts → `unconfirmed`; a "type anyway" consent never survives a restart |
| daemon crashes during a permission | hook sees EOF → no output → terminal dialog |
| Claude exits / tmux killed by hand | `SessionEnd` or reconciliation → `exited`; pending permissions expire; queued prompts → `failed` |
| resumed with a new Claude session_id | `SessionStart` updates the id; history stays under the grovy name |
| prompt while busy | queued, typed after `Stop` |
| two clients send prompts | each is queued (under a lock) and delivered one at a time, in order |
| two clients answer one permission | first wins, the other gets 409 |
| malformed hook JSON | ignored with a stderr diagnostic; exit 0; Claude unaffected |
| hook fires, daemon down | event lost (state will show stale/unknown until the next event); permission → terminal |
| truncated event log | bad lines skipped; a newline is added so the next record isn't corrupted |
| duplicate hook delivery | identical payloads are deduplicated (except permission requests, which are each a live hook) |
| you type in the terminal while the page is open | hooks report it like any other prompt; the page follows |

## Known limitations

* **Prompt injection is keystrokes into a TUI.** If you've half-typed something in the terminal input, the
  web prompt is appended to it. grovy can't see that without scraping the screen, which it deliberately doesn't do.
  If a dialog that emits no hook is open (`/model`, `/config`, a rewind menu, the workspace-trust prompt…), the
  keystrokes go to that dialog. grovy only types in hook-confirmed idle states, and every typed prompt must be
  confirmed by `UserPromptSubmit`, but a non-zero window remains. Use the terminal for anything unusual.
* **Workspace trust.** Claude holds back *all* hooks until a new folder's trust dialog is accepted. For a new
  project (or any git repo you haven't opened in Claude before), `grovy attach` once and accept it. Until then the
  session shows `starting`. The phone only offers folders that won't hit the dialog.
* **After answering a dialog in the terminal**, state can be `unknown` until Claude's next hook event
  (e.g. its `idle_prompt` notification); see the "type anyway?" confirmation above.
* Not every dialog emits `PermissionRequest` (e.g. MCP elicitations, plan approval). Those show as
  "needs input in terminal" at best. The page doesn't pretend to handle them.
* Events emitted while the daemon is down are not replayed.
* Only the final assistant message per turn is shown (`Stop.last_assistant_message`), with no streaming. While
  Claude works, the page shows a typing indicator with the current tool and an elapsed timer, which turns grey after
  60s without events. `MessageDisplay` exists in current Claude Code and could add live text later.
* Multi-select answers are sent as comma-separated labels. Claude receives them as text, which works, but the
  terminal's own encoding for multi-select hasn't been compared.
* Typing a long prompt takes about 1s per 3,000 characters. Anything typed in the terminal at the same moment
  interleaves with it.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

These cover name validation, hook-adapter output (malformed input, daemon down, decisions), state
transitions, duplicate events, event-log recovery, restart recovery, permission
decide/duplicate/concurrent/timeout/hook-gone/session-exit, the prompt queue (busy, permission
pending, one in flight, no retry, concurrency, isolation, failure, force), and HTTP
validation/CSRF/token/SSE. The integration tests run a fake `claude` in **real tmux** on a private
server (`tmux -L`) to check lifecycle, literal delivery of quotes, `$()`, backticks, unicode and multiline text,
isolation between sessions, and that an unrelated look-alike session is never touched.

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
