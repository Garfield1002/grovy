# Troubleshooting

[← README](../README.md)

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
