# Spec format

What `grovy spec guide` tells Claude (`GUIDE` in `plugins/spec.py`), and what the parser reads.

## Files

* A spec is a folder, conventionally `specs/NAME/`. Its `index.md` starts with a YAML header:

  ```yaml
  ---
  spec: 1               # format version (a number)
  title: Spec mode
  status: draft         # draft | active | done | archived
  parts: [format.md, decisions.md]
  ---
  ```

* The index is a short map: overview, **Now**, the roadmap, open questions, the glossary. Documentation prose lives
  in parts, by topic (D35), so an edit or a read touches a small file. The check warns past 8 KB.
* A project may have several independent specs.

## IDs

* Every item, decision and glossary term ends with a stable ID: `{#R17}` (roadmap item), `{#D5}` (decision),
  `{#G3}` (glossary term). Headings may carry one too (for links; they don't count as items).
* The next ID is the highest in use plus one; `grovy spec add|decide|term` assign it (D32). Never reused.
* From another spec: `spec-mode#R17`. In prose and replies, `R17` is enough within the spec.

## Item states

| marker | state |
|---|---|
| `- [ ]` | todo |
| `- [~] @session` | doing, claimed by that session (`grovy spec claim`) |
| `- [x]` | done, not yet written up |
| `- [-]` | dropped (with why) |
| anchor in prose | done and written up as documentation |

* Claim an item before starting it; leave items someone else claimed.
* When a phase is done, write what it built into the topic's part, in the present tense, keeping each item's
  `{#R17}` there, and take the items out of the roadmap.
* Deleting an ID entirely loses its history: drop the item instead.

## Decisions

* Every choice the user settles is recorded at once with `grovy spec decide "..." --why "..." [--supersedes D2]`,
  then mentioned in one line. Append-only: a new entry supersedes an old one.

  ```markdown
  - {#D5} 2026-11-17: Ticking is editing the file. Why: agents already edit files. Supersedes D2.
  ```

## Glossary

`**Term** {#G3}: definition`, one per paragraph, in the index's `## Glossary` (`grovy spec term`).
