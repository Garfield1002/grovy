# Peers (multi-machine) — working notes

Branch `peers` (from `https`). Not merged; the user reviews and merges.

## Design (as agreed)
- Every machine runs its own unchanged grovyd. The PC dials out: `grovy peer tunnel HOST` runs
  `ssh -N -R <hub-socket>:localhost:<port> HOST` in a reconnect loop (stale socket removed over ssh first).
- The hub reads `~/.config/grovy/peers.json` (0600): `{"pc": {"socket": "/abs/path.sock", "token": "...", "tls": false}}`,
  managed with `grovy peer add/rm/ls`. `grovylib/peers.py` holds everything peer-related.
- Peer sessions are `pc:name`; ids (permissions, prompts, pages, share tokens, downloads) are `pc:<hex>`.
- Live updates: one `/api/events?notify=1` stream per peer; names re-emitted prefixed; every (re)connect resyncs the
  whole list (grovy's SSE carries only "something changed" hints, no ids, so there is nothing to replay).

## State
- (see git log on branch `peers`)

## Next steps
- (updated as work proceeds)
