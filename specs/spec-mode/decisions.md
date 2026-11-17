# Decisions

Append-only. Settled in the grill session of 2026-11-17 unless stated otherwise.

- {#D1} 2026-11-17: A spec is markdown with a YAML header, in the repo. grovy only reads it. Why: agents already
  read and edit files, the spec stays useful without grovy, and git keeps its history.
- {#D2} 2026-11-17: Items have stable IDs, assigned by Claude from a `next` counter in the YAML header. Why:
  positional numbers (A6.4 ii) go stale when sections move, breaking links and ledger references.
- {#D3} 2026-11-17: Claude decides how fine-grained items are.
- {#D4} 2026-11-17: A project may have several independent specs. A spec belongs to the project folder: every
  session working in that repo sees it.
- {#D5} 2026-11-17: Ticking an item is editing the file (`[ ]` to `[x]`) with Claude's normal tools. Why: no
  extra tool to remember.
- {#D6} 2026-11-17: grovy finds specs by `spec: true` in the YAML header of git-tracked `.md` files.
- {#D7} 2026-11-17: History comes from hook events: each Edit/Write touching a spec triggers a reparse and a
  snapshot. Edits made outside Claude are missed (open question).
- {#D8} 2026-11-17: The plugin teaches Claude the conventions through SessionStart context, only in projects that
  have a spec. Needs a plugin API addition (R2).
- {#D9} 2026-11-17: Shipped built-in: a daemon plugin in a new repo `plugins/` folder plus `extensions/spec.js`, on
  by default, can be turned off. Why: dogfoods both public APIs (grovy's pi-like direction).
- {#D10} 2026-11-17: The spec view is native (rendered by the extension with `grovy.md`), not a sandboxed page.
  Needs a full-screen view in `window.grovy` (R4).
- {#D11} 2026-11-17: Item states are todo `[ ]`, doing `[~]`, done `[x]`, dropped `[-]`.
- {#D12} 2026-11-17: In chat: tappable `#R17` references, a `spec` widget, decisions recorded automatically,
  glossary terms explained on tap.
- {#D13} 2026-11-17: Progress over time is a burn-up chart (total and done). Why: scope grows, and a burn-down
  hides that.
- {#D14} 2026-11-17: Each spec has its own append-only ledger; a reversal is a new entry that supersedes the old.
- {#D15} 2026-11-17: A spec is the project's roadmap, documentation and history at once. Project decisions end up
  in a spec.
- {#D16} 2026-11-17: A spec is a folder: `index.md` (YAML header, overview, links) plus parts. IDs are unique
  across it. Why: splitting keeps what an agent must read small.
- {#D17} 2026-11-17: A done item is folded into the documentation as prose; its ID stays as an anchor. Why: the
  spec should read as docs, not as an ever-growing checklist.
- {#D18} 2026-11-17: Converting `docs/DESIGN.md` into a spec is the first real test of the format.
- {#D19} 2026-11-17: Claude records decisions as soon as they're settled, then says so in one line.
- {#D20} 2026-11-17: An ID with no checkbox is done; a vanished ID is a lint warning. Lint problems reach Claude
  mid-turn through `PostToolUse` `additionalContext` (needs R3).
- {#D21} 2026-11-17: From the phone, specs change only through Claude: item actions draft messages. grovy never
  writes the repo.
- {#D22} 2026-11-17: Glossary terms live in a `## Glossary` section of the index.
- {#D23} 2026-11-17: Parallel work claims items with `[~] @session`. Snapshots live in grovy's state dir, not the
  repo. First slice: API growth, parser and lint, the view, then the DESIGN.md migration; burn-up, widget,
  references and glossary later.
- {#D24} 2026-11-17: The YAML header says `spec: 1`, the format version, instead of `spec: true`. Amends D6.
- {#D25} 2026-11-17: Specs are found only in git repos, for now.
- {#D26} 2026-11-17: Specs are never shared: no shared link, no page.
- {#D27} 2026-11-17: Edits made outside Claude (by hand, `git pull`) are ignored for now: no rescan.
- {#D28} 2026-11-17: Plugin notes ride on the after-edit wait the main session already does for steering. Subagents
  wait too, but only after editing a spec file, so their spec mistakes are caught and other edits stay fast.
