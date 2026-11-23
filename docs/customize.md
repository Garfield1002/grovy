# Make grovy yours

[← README](../README.md)

Everything here lives in `~/.config/grovy/`, outside the grovy folder, so updates never overwrite it.

- **Extensions:** ⋯ → Extensions on the home screen switches built-in parts on or off (math, widgets, git,
  issues...) for all your devices; reload to apply.
- **Themes:** ⋯ → Theme on the home screen picks a look for all your devices (grovy comes with *nature*: ink on
  paper, serif titles). To make your own, copy [`themes/nature.css`](../themes/nature.css) to
  `~/.config/grovy/themes/NAME.css` and change it; it shows up in the list.
- **Looks:** edit `~/.config/grovy/custom.css`. It starts with the colour names you can change (your message
  bubbles, accents, backgrounds, in light and dark mode). Reload the page to see it.
- **Your own buttons, shortcuts and widgets:** `~/.config/grovy/custom.js` can add items to the ⋯ menu or the
  right-click menu, keyboard shortcuts, buttons under Claude's replies, or new kinds of widgets, and switch off or
  replace built-in parts such as the context bar. It starts with examples to uncomment (if you installed grovy
  before it existed, copy [`examples/custom.js`](../examples/custom.js) there). Reload the page to see it.
- **Telling Claude something in every session** (for example how to use your new widget): write it in
  `~/.config/grovy/instructions.md`.
- **Behind the scenes:** a Python file in `~/.config/grovy/plugins/` can react to what your sessions do (log it,
  send it somewhere, answer in your place) and give your `custom.js` its own data. See
  [`examples/plugin.py`](../examples/plugin.py).
- **Anything else:** grovy is plain Python and one HTML file, with no dependencies and nothing to build. The easiest
  way to change it is to ask Claude, in a grovy session opened on the grovy folder: it knows how to change grovy
  safely while you use it.

Each grovy session gets about 450 tokens of instructions about widgets and pages. `GROVY_WIDGETS=0` in grovy's
environment turns that off.
