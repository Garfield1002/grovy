# Peers (multi-machine) — working notes

Merged into `main` (rebased off `https`, without its HTTPS/push commits).

## What exists (all committed)
- `grovylib/peers.py`: peers.json (0600, validated), HTTP over the tunnel's Unix socket (`UnixHTTPConnection`,
  `check_socket` ownership/folder checks), `Peer` (event stream thread + list refresher thread, generation counter,
  connecting/online/offline), rewriting (`rewrite_*`: pc:NAME and pc:<hex> ids), routing (`Peers.handle_get`,
  `handle_post`, `shared`, `upload_target`), CLI `grovy peer add|rm|ls|token|tunnel` (`run_tunnel` loop).
- `grovyd.py`: peer dispatch first in `do_GET` (after login; `/s/pc:..` before it) and `_route_post`; `_upload` and
  `_relay_download` stream; `/api/sessions` adds `host` + `peers`, `/api/pages` merges, only with peers; `?notify=1`
  event streams; socket message `peers`; `serve()` loads peers and re-reads peers.json every 10s.
- `grovylib/store.py`: `session_order()` (shared sort), `Store.peers`, `_notify` fans out to `notify` subscribers.
- UI: `index.html` (machine label, offline line, machine choice in ＋, `#/s/pc:NAME`, rename keeps machine,
  attachments of peer sessions, open_in fix), `extensions/widgets.js` (peer images/files).
- Docs: DESIGN "Other machines (peers)", failure modes, limitations, tests; README "Another computer"; module map;
  CLAUDE.md files table.
- Tests: `tests/test_peers.py` (55 tests). Full suite green.

## E2E done (2026-11-22, scratch daemons in a private tmux server via TMUX_TMPDIR, all cleaned up)
hub :7397 + pc :7398, forwarder socket -> pc; list, view, prompt, permission allow/deny through the hub (fake
hooks), live SSE hint `pc:pdemo`, kill/resume, offline (502 + unknown) and recovery incl. a permission opened while
down; one real Claude session on the pc daemon driven entirely through the hub (prompt -> Bash permission card ->
allow -> file created -> Stop), hub GROVY_NOTIFY_CMD got "[pc:real] permission: Bash" and "[pc:real] finished: done";
kill + resume of it through the hub; `grovy peer tunnel` with a fake `ssh` on PATH (prep script + -R), drop and
automatic reconnect; real `ssh -G` accepts the argv. Screenshots of list/detail/offline/new-session sheet looked right.

## Review round 1 (adversarial review findings, all fixed; see commit "Peers: review fixes")
- Peer ids fullmatch-checked (32 hex) or dropped; UI encodeURIComponent on every id in a path; core.ID_RE now \A..\Z.
- Every peer field type-checked; threads catch everything; `stale` state when lists fail on a live stream; full
  resync every 60s; `/s/pc:..` pre-login = plain 404; upload 401 -> 502, phone-side upload failure -> 408;
  offline sessions lose `background`; notifications throttled (20/min/peer) and dead after close().
- Honest trust docs + `--no-prepare`; restricted authorized_keys entry verified against a private OpenSSH 10.0 sshd
  (no shell; -L blocked only with a permitopen; -R unix NOT limited by path and can replace sockets with
  StreamLocalBindUnlink yes). Real-ssh e2e of the tunnel (restricted key + --no-prepare, and plain key) passed.

## Review round 2 (fixed)
- Notification budget split: "needs you" (input, incl. the reconnect catch-up) has its own, so "done" floods can't
  drop it. Full resync is time-based (`full_at`), not "the wait timed out". A non-list sessions/pages answer is a
  failure (-> stale), not an empty machine. Failed refreshes respect the generation and the link (no requeue while
  down) and back off 1s..30s.

## Next steps / ideas
- Adversarial review before merge (user's rule).
- Real two-machine test once the user runs `grovy peer tunnel` on the PC.
- Maybe: show peers' issue/new-project creation; plugins' event/state for peer sessions (needs full events on the
  stream); `grovy ls` on the hub listing peers.
