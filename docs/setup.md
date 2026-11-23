# Setting up grovy

[← README](../README.md)

## Install

You need Linux (or macOS), Python 3.11+, tmux, and Claude Code signed in.

```sh
git clone https://github.com/Garfield1002/grovy ~/grovy && ~/grovy/grovy setup
```

Setup asks a few questions (use arrow keys and enter), then shows everything it will change and waits for your yes:

* **Trunk or graft.** A *trunk* is a grovy of its own: pick it unless you already have grovy on another machine. A
  *graft* (worker) joins this computer to that trunk instead (see [Another computer](#another-computer)).
* **Who can reach it.** "This computer only" is the default; to use grovy from your phone, pick your VPN or local
  network. The port is checked, and a free one suggested if 7333 is taken.
* **HTTPS**, if other devices can reach it: off by default (it means installing a certificate on each device;
  notifications need it).

It then installs grovy as a background service that starts at boot and ends with a **QR code**: scan it, or open
the link, once on each device to log it in. Bookmark the page or "Add to Home Screen".

Run it again to change your answers, show the login link, or remove grovy. On macOS, or with `--no-service`, setup
prints a command to start grovy yourself instead. `--dry-run` shows what it would do; `-y` takes the recommended
answers without asking.

**One-time per project:** Claude asks whether you trust a folder the first time it runs there, and grovy can't see a
session until that's answered. So run `claude` once in a new project on the computer (or `grovy attach` the session
and answer it). The phone only offers folders that are already trusted.

## Updating

`grovy update` shows what's new, updates the grovy folder (only if it can do so cleanly), checks that the new
version works, and restarts grovy; your sessions keep running. To give a running session the newest grovy, use
**⋯ → Update session** on the phone.

## HTTPS

Without it grovy uses plain HTTP, so only expose it on a home network or VPN you trust. Notifications need HTTPS.

1. `grovy tls init --name NAME` (the name or address your phone opens grovy by), then restart grovy.
2. Copy the certificate file it names to each device yourself and install it (it prints how).
3. Use the `https://` link from `grovy url`.

Each new `grovy tls init` needs installing again.

## Another computer

One grovy (the *trunk*, or hub, say your server) can also show the sessions of another computer (a *graft*, or
worker, say your PC, even one with no public address), next to its own. Each computer runs its own grovy and keeps
working on its own; the PC connects to the trunk over SSH, so it needs no open port. Its sessions show as `pc:NAME`
with a small "pc" label, and ＋ asks which machine to start on.

1. Set up the trunk first (`grovy setup` there, "Trunk").
2. On the PC, run `grovy setup` and pick **Graft**. It asks which machine is the trunk (from your `~/.ssh/config`, or
   an address), checks it, and asks this computer's name there. It then makes a key just for the connection, adds it
   on the trunk (your usual ssh login, once; a password is fine), and runs the connection as a service that
   reconnects by itself. It ends by checking that the trunk sees the PC.

**Mind the key:** the trunk only lets that key keep the connection (no shell, no other commands), but treat the PC as
trusted all the same: details in [`DESIGN.md`](DESIGN.md) ("Other machines").

On the trunk, grovy's commands work on the PC's sessions too: `grovy ls` lists them, and `grovy attach pc:NAME`
(also `logs`, `kill`, `resume`, `rename`, `new`) reaches one. On the PC, `grovy ls` lists the trunk's sessions too, as
`trunk:NAME`, and `grovy attach trunk:NAME` reaches one. A name alone works when only one machine has it
(`grovy attach Tups`); when several do, grovy asks which.

`grovy shell pc` (or `grovy shell trunk` on the PC) opens a shell on the other machine, in tmux: Ctrl-b d leaves it
running, and the next `grovy shell` picks it up. Setup asks whether the trunk may open a terminal on the PC
(`attach` and `shell`); without it, the rest still works.

`grovy peer ls` on the trunk says whether the PC is online. While the PC is off, its sessions show as "unknown" and
the page says so. What works on the trunk only (new issues, new projects) and other limits are in
[`DESIGN.md`](DESIGN.md) ("Other machines"). By hand, without setup:
`grovy peer token | ssh HUB '~/.local/bin/grovy peer add pc'`, then `grovy peer tunnel HUB --name pc` on the PC.

## Uninstalling

`grovy cleanup` undoes what setup did, after showing you the list. It keeps your own files in `~/.config/grovy`
unless you say otherwise, and running Claude sessions unless you ask it to end them. On a graft, it removes the PC
from the trunk too.
