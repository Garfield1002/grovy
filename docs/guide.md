# Using grovy

[← README](../README.md)

Each Claude session runs in its own tmux session on your computer, exactly as if you had started `claude` in a
terminal. grovy listens to Claude's hooks and shows everything on a phone-friendly page. The terminal never goes
away: `grovy attach NAME` puts you in the real session (detach with Ctrl-b d).

## Sessions

- **The list:** every session with live status (working, idle, needs you), context used, and the last activity.
- **Start, resume, rename, stop:** ＋ starts something — a session in any trusted folder (or continue or fork a past
  conversation there), a quick issue, or a new project. The folder list has a search box and a Recent ⇄ A–Z sort, so
  it stays quick with many projects. ⚙ holds grovy's own settings. ⋯ on a session pins it to the top of the list
  (📌), renames or stops it; an ended session can be resumed later.
- **Command palette:** ⚡ on the session list (⋯ → Search commands in a session), or `Ctrl`/`⌘`+`K` on a keyboard,
  opens one searchable list of everything you can do here plus a jump to any session or page — start typing and press Enter.
  *Restart grovy* there reloads grovy's daemon (after you change grovy itself); your sessions keep running.
- **Background work:** subagents and commands Claude left running show as "idle · 2 running"; tap the strip above
  the message box to see each one, how long it has run and what it is doing.
- **Updating a session:** after `grovy update`, ⋯ → Update session gives a running session the newest grovy
  (Claude restarts and resumes the same conversation). Some features below need it on older sessions.
- **Up to date?** grovy checks GitHub once a day for a newer release and shows it under ⚙ ("Update available", or
  "Check for updates" to look now). To update, run `grovy update`. Turn the check off with `GROVY_UPDATE_CHECK=off`,
  or point it at your own fork with `GROVY_UPDATE_REPO=owner/repo`.

## Talking to Claude

- **Chat:** Claude's replies, rendered properly (tables, code, lists), plus what's happening now.
- **Messages:** type to Claude; messages wait in a queue while Claude is busy. `/` lists the skills and commands
  that work from the phone.
- **Steering:** a message you send while Claude is working reaches it at its next step, so you can correct course
  without waiting or interrupting (the button turns blue). With the box empty, the same button is ■ to stop Claude.
- **Quote and copy:** each reply has Copy and Quote buttons; or select part of it and tap Quote selection. Tap a
  quote to jump back to where it came from. "Worked for 2m · 14 tool calls" under a reply lists what Claude ran.
- **Steps:** the stacked-lines button at the top cycles how much you see on the way to each answer, live as it works:
  just the answers, or also Claude's words between tool calls, or also the tool calls themselves (the icon grows a
  line at each step). Tap "Worked for …" under a reply to open or close just that one.
- **Long messages:** once a message outgrows the box, it takes the whole screen; the ⤢ / ⤡ button in its corner
  switches by hand.
- **Formatting:** type markdown (`**bold**`, `*italic*`, `` `code` ``, `> quote`, lists) and the message box shows it
  as you type; your messages show it formatted too.
- **Attachments:** tap the paperclip in the empty message box to send photos or files; Claude gets them as files it
  can open (kept for 7 days). Photos show in your message.

## Approvals and questions

When Claude wants to run a command or edit a file, you see the full command or diff and tap Allow or Deny.
Questions Claude asks you show up as cards you answer with a tap. If you don't answer on the phone, Claude's
normal question in the terminal is still there.

## What Claude can show you

- **Math:** formulas Claude writes in LaTeX (`$x^2$`, `$$\sum_i x_i$$`) show as real math.
- **Widgets:** charts, checklists, diagrams, images, or buttons to pick from.
- **Pages:** full HTML pages (reports, dashboards) that open full-screen. You can share a page on your network with
  a link; Claude can't.
- **Shared files:** Claude can hand you any file it made (a report, a CSV, a zip, a log) as a card with a Download
  button. The file isn't copied anywhere; grovy serves it straight from the project, just for you.
- **Live progress:** when Claude runs something long (a build, training, a batch job), its script can show a
  progress bar, notes or the latest output lines in the chat while it runs.

## Projects and git

- **Issues:** ＋ → New issue starts Claude on one small fix in a copy of the project of its own (a git worktree, on
  a new branch). Pick the project from a searchable, sortable list, then say what to fix. When it has committed,
  ⋯ → Finish issue shows the commits; one tap merges them into your main branch and cleans everything up.
- **Report (for grovy itself):** press **Alt+R**, or hold two fingers on the screen for a moment, to paint over
  anything in grovy's own UI you don't like, add a line about what's wrong, and file it as an issue in your grovy
  repo; the agent gets a screenshot with your paint on it. It paints on top of what you were doing and leaves it untouched, so cancelling puts you right back.
- **New projects:** ＋ → New project makes a blank repository or clones one into `~/dev` and starts Claude in it.
  For a clone, you see first which of its files Claude would load (some can run commands) and say whether to trust it.
- **Git:** ⋯ → Git shows the session's repository as a graph of branches and commits; tap a commit for its files
  and diff. Branch names and commit hashes in Claude's replies are links to them, and a file path Claude mentions is
  a link too — tap it to read that file's current contents.
- **Links and colours:** web links in replies stand out, long addresses are shortened to their site, and colour
  codes such as `#3e5482` show a dot of the colour.

## Notifications

With [HTTPS](setup.md#https) on, ⚙ → Notifications on each phone lets you hear when a session needs you, finishes or
fails, even with grovy closed. The notification travels empty through your phone maker's push service; the phone
then fetches what happened from grovy itself. On iPhone, open grovy from the home screen first (Share → Add to
Home Screen).

## On a computer

On a wide screen, your sessions and pages stay in a sidebar beside the conversation, and menus open as dialogs.

- **Right-click** a session, a page, a message, a code block or the conversation for what you can do with it.
  Select text in a message for Quote and Copy. Shift+right-click gives the browser's own menu.
- **Keys:** Ctrl+Enter sends, Alt+↑/↓ switches session, Esc closes, Tab reaches every button. Typing with nothing
  focused goes to the message box.

## Commands

```sh
grovy new api ~/src/api        # start Claude in a folder (also from the phone: ＋)
grovy ls                       # sessions and their state
grovy attach api               # the real terminal
grovy resume api               # continue an ended session
grovy rename api backend       # rename it
grovy kill api                 # stop it
grovy url                      # the phone link again (also on the phone: ⚙ → Login link)
grovy update                   # update grovy itself
grovy doctor                   # check that everything is in order
```

In a terminal, `grovy ls` is a coloured table (sessions waiting on you stand out). Piped into another command, or
with `NO_COLOR` set or `--plain`, it prints the plain columns scripts can read.

`grovy setup` also sets up tab completion in bash (commands, options, session names); for a shell without the
bash-completion package, add `eval "$(grovy completion bash)"` to `~/.bashrc`.

With [another computer](setup.md#another-computer) joined, these commands work on its sessions too.
