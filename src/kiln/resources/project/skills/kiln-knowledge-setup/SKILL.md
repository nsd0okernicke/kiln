---
name: kiln-knowledge-setup
description: Discover and propose local project documentation for Kiln's searchable knowledge catalog. Use after project initialization or when approved knowledge sources need deliberate revision.
---

# Kiln Knowledge Setup

Build a useful source catalog without treating every repository document as authoritative.

1. Run `kiln knowledge setup --json` from the project root to discover likely Markdown, text,
   and PDF sources.
2. Inspect each candidate enough to explain what decisions it can support. Exclude generated
   reports, dependencies, temporary output, secrets, and documents unrelated to project work.
3. Ask about important sources that repository discovery cannot identify. Accepted: project-local
   Markdown, UTF-8 text, PDFs, directories containing them, and `http(s)` URLs pointing at
   documentation pages. Discovery never proposes a URL -- a human must name it.
4. Present the proposed source ID, path, title, type, tags, purpose, and any duplication or
   contradiction before changing `kiln/project/knowledge.json`.
5. Wait for explicit approval. Then use `kiln knowledge add` for approved entries and run
   `kiln knowledge sync`.
6. Report indexed, skipped, removed, and failed counts. Resolve failures without deleting or
   modifying the original documents unless the user separately requests that work.

The constitution is authoritative. Knowledge sources are supporting evidence and must not
silently rewrite or override constitutional rules. Only HITL curates sources; autonomous roles
may search and show indexed documents but must not change the catalog.

Never add credentials, agent conversations, `.kiln/`, `.worktrees/`, or generated instructions.
Always leave original source files in their existing project-owned location.

Judge a URL more carefully than a file. It is fetched on every sync and its content can change
without review, so prefer a versioned documentation page over a wiki anyone can edit, and copy
the document into the repository instead when the decision it supports has to stay reproducible.
A URL that needs a login cannot be indexed; catalog credentials are refused outright.
