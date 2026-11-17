# Spec format

What the plugin teaches Claude at SessionStart (R9), and what its parser reads (R6).

## Files

* A spec is a folder, conventionally `specs/NAME/`. Its `index.md` starts with a YAML header:

  ```yaml
  ---
  spec: 1               # format version
  title: Spec mode
  status: draft          # draft | active | done | archived
  next: {R: 17, D: 24, G: 8}
  parts: [format.md, decisions.md]
  ---
  ```

* The index holds the overview, **Now**, the roadmap (or its summary), open questions and the glossary. Big areas
  move to parts, so an agent reads only what it needs: the index is injected at SessionStart, parts are read on
  demand (grep for `{#R17}`).
* A project may have several independent specs.

## IDs

* Every item, decision and glossary term ends with a stable ID: `{#R17}` (roadmap item), `{#D5}` (decision),
  `{#G3}` (glossary term). Headings may carry one too.
* Claude assigns them: take the number from `next`, then bump it. IDs are never reused, even for dropped items.
* From another spec: `spec-mode#R17`. In prose and replies, `R17` is enough within the spec.

## Item states

| marker | state |
|---|---|
| `- [ ]` | todo |
| `- [~] @session` | doing, claimed by that session (or `@session/agent` for a subagent) |
| `- [x]` | done, not yet written up |
| `- [-]` | dropped (keep a word on why) |
| anchor in prose | done and written up as documentation |

* Claim an item before starting it; skip items someone else claimed.
* Once an item is done, fold it into its section's prose, in the present tense (how it works now), keeping its
  `{#R17}` as an anchor. The checklist shrinks as the documentation grows.
* Deleting an ID entirely is a lint error: drop the item instead.

## Decisions

* Every decision settled in a session with a spec gets an entry, written at once, then mentioned to the user in
  one line. The user may say not to record one.
* Append-only: never edit an old entry; write a new one that supersedes it.

  ```markdown
  - {#D5} 2026-11-17: Ticking is editing the file. Why: agents already edit files; no tool to remember.
    Supersedes D2.
  ```

## Glossary

`**Term** {#G3}: definition`, one per paragraph, in the index's `## Glossary`.
