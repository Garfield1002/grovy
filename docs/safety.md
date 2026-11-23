# Safety

[← README](../README.md)

- **You need the login link.** The page needs a secret token; without it, nothing is visible. Only expose grovy on
  a home network or VPN you trust: it uses plain HTTP unless you turn on [HTTPS](setup.md#https).
- **Nothing is approved without you.** If you don't answer on the phone, Claude's normal question in the terminal is
  still there. Your own Claude permission settings keep applying.
- **grovy only touches the sessions it started.** Your other Claude windows and tmux sessions are left alone.
- **Pages are sandboxed.** Claude can publish pages but can't share them; only you can, from the phone. Pages can't
  see grovy or your login, or send data out (one exception, and other limits, in [`DESIGN.md`](DESIGN.md)).
- **Another computer** joins over SSH with a key that can only keep the connection open, but treat a joined PC as
  trusted all the same ([details](setup.md#another-computer)).

The full security model is in [`DESIGN.md`](DESIGN.md).
