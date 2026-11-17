---
spec: 1
title: Spec mode
status: draft
next: {R: 17, D: 30, G: 8}
parts: [format.md, decisions.md]
---

# Spec mode

A spec is a project's living document: its **roadmap** (what's next), its **documentation** (how things work
now) and its **history** (the decisions that got it here). It replaces long grill sessions ending in several
loose plan files that agents keep re-reading. It is plain markdown in the repo, written by Claude, read by grovy,
and shipped as a built-in plugin that can be turned off.

* How specs are written: [format.md](format.md)
* Why it is this way: [decisions.md](decisions.md)

## Now

Nothing yet: design settled on 2026-11-17, see [decisions.md](decisions.md).

## Roadmap

### Phase 1: grow the APIs {#R16}

The plugin must be built only on public APIs (D10), so these come first.

- [x] Built-in daemon plugins: a repo `plugins/` folder loaded like `~/.config/grovy/plugins/`, on by default,
      each one can be turned off {#R1}
- [x] Plugin API: add text to a session's SessionStart context (like `WIDGET_GUIDE`), per project {#R2}
- [x] Plugin API: `grovy.after_edit(fn, glob)` answers an edit with a note Claude reads before its next step,
      riding on the wait the main agent's hook already does for steering. A subagent's hook waits only when the
      edited path matches a glob some plugin registered (D29) {#R3}
- [ ] `window.grovy`: a full-screen view of an extension's own, with a route so it can be linked to {#R4}

### Phase 2: parser and lint

- [ ] Find specs: git-tracked `.md` files whose YAML header has `spec: N` (the format version); their parts are
      listed in `parts:`. Git repos only {#R5}
- [ ] Parse items, states, claims, anchors, decisions, glossary, across the index and its parts {#R6}
- [ ] Lint: duplicate IDs, a `next` counter behind the highest ID, dangling references, an ID that vanished,
      a claim by an exited session. Problems go back to Claude mid-turn (R3) {#R7}
- [ ] Snapshots: on each Edit/Write touching a spec file, store done/total per section and who changed what, in
      the plugin's folder under grovy's state dir {#R8}
- [ ] SessionStart: in a project with specs, add the conventions and each spec's index (not its parts) {#R9}

### Phase 3: the spec view

- [ ] List the project's specs from the session's ⋯ menu; open one in the full-screen view {#R10}
- [ ] Sections with progress bars, items with their state and claim, superseded decisions greyed {#R11}
- [ ] Item actions only draft messages to Claude ("work on R6", "drop R6", "explain D5"); grovy never writes the
      repo {#R12}

### Phase 4: dogfood

- [ ] Convert `docs/DESIGN.md` into `specs/grovy/`, and point CLAUDE.md's "keep DESIGN.md in sync" rule at it
      {#R13}

### Later

- [ ] Burn-up chart (total and done over time) from the snapshots {#R14}
- [ ] `spec` widget: a live progress card for a spec or a few items; `#R17` / `name#R17` references in replies
      become chips that open the view there; glossary terms are underlined, a tap shows the definition {#R15}

## Glossary

**Spec** {#G1}: a folder whose `index.md` has `spec: 1` (the format version) in its YAML header, plus the parts it lists.

**Part** {#G2}: another markdown file of the same spec, listed in the index's `parts:`. IDs are unique across
all of a spec's files.

**Item** {#G3}: a piece of work with a stable ID (`{#R6}`). Claude decides how fine-grained items are.

**Anchor** {#G4}: an item's ID left in the prose once the item is done and written up as documentation. Links
and references keep working.

**Claim** {#G5}: `[~] @session` on an item: someone is working on it, others leave it alone.

**Ledger** {#G6}: a spec's append-only list of decisions (`{#D5}`), each one dated, with its reason and what it
supersedes.

**Snapshot** {#G7}: what the plugin records when a spec changes: done/total per section, who changed which items.
Burn-up charts are drawn from snapshots.
