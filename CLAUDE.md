# Working on grovy

grovy runs Claude Code sessions in tmux and serves a phone UI for them. `docs/DESIGN.md` documents behaviour,
invariants and limitations: read the section you are changing (not the whole file) and keep it in sync.
`README.md` is for users (install, what it does, troubleshooting): update it only for user-visible changes, and
keep it short and non-technical.
This file covers **how to change it safely**.

## You may be running inside grovy

If `$GROVY_SESSION` (or `$CCS_SESSION`, for sessions started before the rename from ccs) is set, you are a grovy-managed session. The user is probably talking to you
**through the code you're editing**. Breaking it cuts the user off.

- **Never** kill your own tmux session (`grovy-$GROVY_SESSION`), the daemon's (`grovyd`), or any session you
  didn't create. Never run `tmux kill-server` on the default server.
- `grovy.py hook NAME` runs for **every** hook event of every session, with the code as it is on disk the moment
  you save. It loads `grovy.py`, `grovylib/hook.py` and what that imports (`core`, `client`, `claude`, `projects`,
  `sessions`, `tmuxctl`, `scriptlib`, `issues`, `states`). A syntax error there makes all hooks fail. That fails safe (permissions fall back
  to the terminal dialog), but the phone goes blind. Run `python3 -c "import grovy; from grovylib import hook"` right
  after editing any of them.
- `index.html` is hot-reloaded by the daemon, and the user gets it on their next page reload.
- The daemon loads `grovyd.py` and `grovylib/` once, so changes to them need a daemon restart (changes to the modules
  above reach hooks at once, and the daemon only after the restart). Restarting never touches Claude sessions:
  `tmux respawn-pane -k -t =grovyd: "<same command line it was started with>"` (check it with
  `tmux list-panes -t =grovyd: -F '#{pane_start_command}'`). Restart only after the tests pass. Events sent during
  the ~1s gap are lost.
- Never `pkill -f 'grovy serve'`: the pattern matches your own shell's command line and kills it.
  Use a pidfile, or a `[c]cs serve` style pattern.

## Files

| file | what |
|---|---|
| `grovy.py` | the `grovy` command (hook/statusline go straight to `grovylib/hook.py`), and what `import grovy` gives scripts |
| `grovylib/` | everything else, one file per topic: **`grovylib/__init__.py` has the map**. Cross-module references are always `module.name` (e.g. `sessions.registry()`), so one `mock.patch.object(module, "name")` reaches every caller |
| `grovyd.py` | daemon entry: `Http` (API, SSE), `SockHandler` (hooks, CLI), `serve()`, `load_index()` |
| `Store` | `grovylib/store.py`, assembled from one class per feature (`Tasks`, `Permissions`, `Prompts`, `SessionLifecycle`, `Pages`, `LiveCards`, `Attachments`, `Issues`, `NewProjects`), each in its own module, all sharing `self.lock` and `self.sessions` |
| `extensions/` | built-in page extensions (`context-bar.js`, ...): features written only against `window.grovy`, loaded before the user's `custom.js`. A self-contained UI feature belongs here rather than in `index.html` when the API can carry it; when it can't, grow the API |
| `plugins/` | built-in daemon plugins (`git.py`), loaded like the user's own and only through the plugin `Api`; a user plugin with the same name replaces one. Built-in extensions get their data from them |
| `index.html` | the whole UI: vanilla JS, one inline `<script>` (its CSP hash is computed at load); a map of its parts is at the top of the script |
| pages | `grovy page` (`cli.cmd_page`) → socket `page` → `Store.publish_page` (`pages.py`); served by `/p/<id>` with `PAGE_CSP` + `PAGE_BRIDGE`. Never loosen `PAGE_CSP` (no `allow-same-origin`, no `connect-src`) without the user agreeing |
| setup | `grovy setup` (`install.cmd_setup`, `SERVICE_UNIT`; keep `examples/grovy.service` in sync) |
| customizing | the user's own files in `~/.config/grovy/`, never in the repo: `custom.css` and `custom.js` (added to the page by `load_index()`; the script gets its own CSP hash), `instructions.md` (added to Claude's SessionStart context by `claude.user_instructions()`), `plugins/*.py` (loaded by the daemon, `grovylib/plugins.py`; their callbacks run on a worker thread, never under `Store.lock`). `custom.js` uses only `window.grovy` (the "extensions" part of `index.html`); plugins use only the `Api` object. Keep both APIs stable, documented in `examples/custom.js` and `examples/plugin.py`, and grow them rather than change them |
| widgets | `WIDGET_GUIDE` in `grovylib/claude.py` (what Claude is told, via the SessionStart hook) and `extensions/widgets.js` (renderers, registered with `grovy.widget`; `index.html` only draws the frame and the error). Change both together; every renderer validates its input and throws a readable `Error` |
| live widgets | client library in `grovylib/scriptlib.py` (`track`, `progress`, `note`, `log`, `show`, re-exported by `grovy.py`; CLI `tail`/`progress`/`note`/`show`) → socket `live` → `Store.live_update` (`live.py`) → `Live` event, rendered by `liveNode`. User scripts import this: it must stay import-cheap, and those functions must never raise or block |
| `tests/test_grovy.py` | unit tests that drive `Store` with fake tmux/paste callables, plus real-tmux integration tests on a private server |
| peers | `grovylib/peers.py`: other machines' grovyd shown as `pc:NAME` (proxy, event stream, `grovy peer ...`), tested by `tests/test_peers.py` with a real peer daemon behind a Unix-socket forwarder. Without `~/.config/grovy/peers.json` nothing may change |

## Rules

- Python standard library only. No frontend build, no npm. External processes are always argument arrays, never `shell=True`.
- UI: build DOM with `h()`, never `innerHTML`. Claude's output and tool inputs are untrusted.
  `replaceChildren(null)` inserts the text "null", so spread conditionally.
- Every state change goes through `Store` under `self.lock`. Calls to tmux are bounded (`tmux(..., timeout=)`); keep
  lock-held sections short.
- **Never guess state.** Prefer `unknown` to inference from silence. Typing into a session requires a hook-confirmed
  idle state. Nothing is ever approved without an explicit user decision.
- A "force"/"type anyway" consent applies only at the moment it's given; it must not survive a later state or a restart.
- Every bug fix gets a regression test. Check that it **fails on the old code**, e.g. with
  `git worktree add /tmp/x HEAD` and the new test file copied in.
- Keep `docs/DESIGN.md` in sync (behaviour, failure modes, limitations), and `README.md` for user-visible changes.

## Testing

```sh
python3 -m unittest discover -s tests        # ~10s, no API key, private tmux server
```

**Scratch daemon** (never the live one, never `~/.local/state/grovy`, never port 7333). Keep the state dir short:
AF_UNIX paths over ~100 bytes fall back to `$XDG_RUNTIME_DIR`.

```sh
D=$(mktemp -d /tmp/grovydev-XXXX)
tmux new-session -d -s grovyd-scratch "GROVY_STATE_DIR=$D python3 $PWD/grovy.py serve --port 7397"
GROVY_STATE_DIR=$D ./grovy new t1 /some/trusted/dir      # real claude; costs a few small API calls
T=$(cat $D/token); curl -H "Cookie: grovy_token=$T" localhost:7397/api/sessions/t1
GROVY_STATE_DIR=$D ./grovy kill t1; tmux kill-session -t =grovyd-scratch; rm -rf $D
```

Each daemon only sees tmux sessions tagged with its own state dir (`@grovy_state`), so scratch sessions don't show
up on the user's phone. Remember that `tmux new-session` takes its environment from the tmux server, not your
shell: pass config explicitly.

**UI checks.** Syntax: extract the script and run `node --check`. Visual: build a copy of `index.html` with
`fetch` stubbed to return captured API JSON, `EventSource` stubbed, `setTimeout` in `refresh()` made synchronous
and `*{animation:none!important}` added, then screenshot it:
`firefox --headless --no-remote --profile $(mktemp -d) --window-size=390,844 --screenshot out.png file://...`.
Put scratch files in your scratchpad, not the repo.

## Verified Claude Code behaviour (2.1.325). Don't re-derive this

- Hooks are injected per session with `claude --settings FILE`; hook arrays merge with the user's settings.
  **The `--settings` file is read once at start**: editing it doesn't affect a running Claude (verified with a
  statusLine marker). Changes to it reach a session on its next start: ⋯ → Update session (`Store.restart`, in `manage.py`).
- The status line (`statusLine` in the same file → `grovy statusline NAME`) gets JSON on stdin: `model.display_name`,
  `effort.level`, `fast_mode`, `context_window.{used,remaining}_percentage` (null until the first message), `cost.*`,
  `cwd`, `session_id`. grovy forwards a trimmed copy and prints the user's own status line, so the terminal is unchanged.
- Hooks inherit the environment. **All hooks are held back until the workspace-trust dialog is accepted.**
  Trust (`~/.claude.json` `projects[path].hasTrustDialogAccepted`): a plain folder inherits it from any trusted
  ancestor, but a **git repo is its own boundary**: inside trusted `~/dev`, an untrusted repo still gets the dialog.
- `PermissionRequest`: the terminal dialog is shown *while* the hook waits. Answering it in the terminal kills
  the hook, so the daemon sees EOF. Exit 0 with no output means "no opinion": normal dialog.
- `AskUserQuestion` arrives as a `PermissionRequest`. Answer with `allow` plus `updatedInput.answers =
  {question: answer}`; free text works. This holds in auto and default mode.
- Esc (interrupt) and answering "No" in the terminal emit **no hook**. Built-in commands like `/context` emit
  none either. Skills emit `UserPromptSubmit` with the literal `/name args`. `/clear` and `/compact` emit
  `SessionStart(source=clear|compact)`; compaction can also happen automatically mid-turn.
- Large or fast pasted input is wrapped as `<pasted_content>`, which Claude treats as quoted material. That's why
  prompts are *typed* in small chunks. tmux drops a trailing `;` from a `send-keys` argument, so `;` is sent as
  `-H 3b`. Typing a leading `/` or `!` switches Claude Code's input mode, and Claude Code **trims leading
  whitespace and newlines** before looking for a command, so a pasted `" /model"` still opens the menu. Command
  detection must use `text.lstrip()`.
- Subagent reports and task notifications are injected as prompts, so they fire `UserPromptSubmit` too. The UI
  must not show them as the user's messages.
- `claude --resume <id>` on a conversation that was never saved exits immediately with an error.
- Hooks fired inside a subagent carry `agent_id`/`agent_type` and the parent's `session_id`. A background subagent's
  `PostToolUse` can arrive after the parent's `Stop`.
- Text typed while Claude works is queued by Claude Code (fires `UserPromptSubmit` at once, read at the next tool
  boundary). `PostToolUse` `hookSpecificOutput.additionalContext` reaches Claude mid-turn and it acts on it: grovy's
  steering uses that, never typing while busy.
- (systemd 257) systemd-oomd watches `user@UID.service` and kills whole scopes; each tmux pane is one. A scope's
  `ManagedOOMPreference=avoid` is honoured only if given when the scope is created (`user.oomd_avoid` xattr), not via
  `set-property`. Killed scopes are garbage-collected; the reason stays in `journalctl --user -u SCOPE`.
- A `--settings` file's `autoMode` block (`environment`/`allow`, prose, `"$defaults"` keeps the built-ins) reaches the
  auto-mode classifier: check with `claude --settings FILE auto-mode config`.
