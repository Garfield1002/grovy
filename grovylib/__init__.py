"""grovy's code, one file per topic. Start here to find what to change.

Entry points (in the grovy folder):
  grovy.py       the `grovy` command, and the library scripts `import grovy` for (scriptlib.py)
  grovyd.py      the daemon: HTTP API + live updates for the browser, Unix socket for hooks and the CLI
  index.html     the whole web UI (one file; its map is at the top of its <script>)
  extensions/    built-in page features written against window.grovy, like a user's custom.js

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
  install.py     setup, doctor, url
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
  notify.py      GROVY_NOTIFY_CMD
  plugins.py     the user's own daemon plugins (~/.config/grovy/plugins/*.py)
"""
