"""grovy's code, one file per topic. Start here to find what to change.

Entry points (in the grovy folder):
  grovy.py       the `grovy` command, and the library scripts `import grovy` for (scriptlib.py)
  grovyd.py      the daemon: HTTP API + live updates for the browser, Unix socket for hooks and the CLI
  index.html     the whole web UI (one file; its map is at the top of its <script>)
  extensions/    built-in page features written against window.grovy, like a user's custom.js
  plugins/       built-in daemon plugins (git.py), written against the plugin Api, like a user's own

Shared:
  core.py        settings from the environment, paths, login token, JSON files, names, small text helpers
  client.py      talking to the daemon over its Unix socket

Claude sessions (used by the CLI and the daemon):
  claude.py      how a session's Claude starts: command line, --settings file (hooks, status line), instructions
  sessions.py    the registry (sessions.json); start, resume, rename, stop
  tmuxctl.py     tmux, and typing into Claude safely
  projects.py    Claude's own files: trusted folders, past conversations
  oom.py         keeping sessions out of the out-of-memory killer's way
  hook.py        the hook adapter Claude Code runs for every event (`grovy hook NAME`, `grovy statusline NAME`)

Command line:
  cli.py         the `grovy` subcommands
  setupflow.py   `grovy setup`: the guided install (questions, one review, then apply)
  graft.py       setup's graft role: joining a trunk over ssh with a restricted key (and its cleanup)
  remote.py      the CLI on another machine's sessions (MACHINE:NAME): a graft's API, or ssh to the trunk
  terminal.py    `grovy attach pc:NAME`: a terminal relayed by the graft's grovy over the tunnel
  install.py     doctor, url, tls init, and what setup and cleanup share (service unit, setup record, links)
  cleanup.py     `grovy cleanup`: undoes setup (from its record, setup.json)
  tls.py         optional HTTPS: which certificate the daemon serves; the one `grovy tls init` makes
  tui.py         the terminal UI setup and cleanup use: colours, step lines, arrow-key menus (stdlib only)
  qr.py          a tiny byte-mode QR encoder, for the scannable login code `grovy setup` ends with
  scriptlib.py   track/progress/note/log/show for scripts running inside a session

Daemon state (Store, in store.py, is assembled from one class per feature; all share self.lock):
  store.py       sessions, events, history, what the browser is sent
  states.py      session states and their timing rules
  tasks.py       background work a session left running
  perms.py       permission requests and questions: waiting for the user's decision, never guessing one
  prompts.py     the prompt queue: typing into idle sessions, steering busy ones, interrupting
  commands.py    the slash commands offered on the phone
  manage.py      start/resume/rename/restart/stop from the phone; reconciling with tmux
  pages.py       HTML pages Claude publishes, their sandbox, sharing
  live.py        live widgets scripts show and update
  uploads.py     files sent from the phone
  issues.py      issues: a session in a git worktree of its own, merged back by fast-forward
  newproject.py  new projects: a blank repo or a clone in ~/dev, and Claude's trust question answered on your say
  notify.py      GROVY_NOTIFY_CMD
  plugins.py     daemon plugins: the built-in ones (plugins/) and the user's (~/.config/grovy/plugins/*.py)

Other machines:
  peers.py       peers: another machine's grovyd shown here (pc:NAME) over an SSH reverse tunnel's Unix socket;
                 peers.json, proxying, the per-peer event stream; `grovy peer add/rm/ls/token/tunnel`
"""
