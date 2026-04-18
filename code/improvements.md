# WikiVault Improvement Backlog

Derived from comparison with Karpathy's pure-LLM wiki pattern.
Pick one item at a time, implement, check it off.

---

## 🟥 High Leverage, Low Effort

- [ ] **1. File Q&A answers back into the wiki**
  - After a useful query, save the answer as a new wiki page in `wiki/`
  - This is Karpathy's most powerful insight — every question compounds the knowledge base
  - Could be a flag in `qa_agent.py`: `--save` writes output to `wiki/<topic>.md`
  - The skill layer (Claude) can also be instructed to persist good answers

- [x] **2. Add `log.md`**
  - Append-only chronological record of ingests, wiki generation runs, linter passes
  - Each entry format: `## [YYYY-MM-DD] ingest | <filename>` (grep-parseable)
  - Add a write step at the end of `compile.py`, `wiki_generator.py`, and `knowledge_linter.py`
  - Lets you (and the LLM) know what's been done recently without reading all logs

- [x] **3. Add `index.md`**
  - Auto-generated catalog: every wiki page listed with a one-line summary + link
  - Organized by category (concepts, services, source summaries, Q&A outputs)
  - LLM reads this first when answering queries — avoids loading the full wiki
  - Regenerate at the end of each `wiki_generator.py` run

---

## 🟧 Medium Effort, High Payoff

- [x] **4. Concept alias system**
  - LLM generates aliases per concept at wiki-generation time (not at link time — too slow)
  - Aliases cached in `aliases.json`: `{ "Integration Gateway": ["IGW", "integration-gateway", "Integration Gateways"] }`
  - `auto_linker.py` loads `aliases.json` and expands scan patterns to include all variants
  - LLM call added to `save_concept()` in `wiki_generator.py`: given concept name → returns abbreviations, hyphenated forms, plurals, common synonyms
  - Cache is transparent/auditable — you can hand-correct bad LLM suggestions
  - Only re-generates aliases when the concept page is updated (hash-gated, same as concepts)
  - Eliminates most ghost concepts at the source (root cause fix vs. ghost resolver workaround)

- [x] **5. Source tracking per concept**
  - Each concept page frontmatter records which `raw/` files contributed to it
  - Example YAML: `sources: [raw/mcs-assembly/chapter-3.md, raw/MCS Core.md]`
  - Enables queries like "what sources say X?" and makes contradictions traceable
  - Add to concept merge logic in `wiki_generator.py`

- [x] **6. Make the linter actionable**
  - Currently `knowledge_linter.py` writes `knowledge_report.md` but does nothing
  - Add a `--fix` mode: weak concepts get patched, missing concepts get stubs created, duplicates get merged
  - The report becomes an audit trail of what was fixed, not just a list of problems

- [ ] **7. Embedding-based retrieval for Q&A**
  - Replace keyword filtering in `qa_agent.py` with vector search
  - Options: local `chromadb` or `sqlite-vec`, or shell out to `qmd` (BM25 + vector hybrid)
  - Makes query answers reliable at scale (keyword matching breaks badly as wiki grows)
  - Already listed in your future roadmap — this is the right time to do it

---

## 🟦 Architectural / Longer Term

- [ ] **8. Semantic auto-linker (replace regex)**
  - Current `auto_linker.py` only matches exact strings — misses synonyms, abbreviations, plurals
  - Replace with embedding similarity: find concept mentions even when string doesn't match
  - Can be a second pass after the existing regex linker (hybrid approach)
  - Biggest structural weakness in the current pipeline

- [ ] **9. Conversational ingest review pass**
  - After batch pipeline runs, add an interactive step: LLM reviews what changed and does a synthesis pass
  - Bridges Karpathy's "alive" compounding loop with your reliable deterministic pipeline
  - Could be a post-step in `run_pipeline.py` that opens a chat session summary

- [ ] **10. Handle non-markdown source types**
  - Current pipeline assumes Web Clipper markdown
  - Add handlers for: PDF, YouTube transcript, Slack export, meeting notes (plain text)
  - Each handler normalizes to the same structured markdown format `compile.py` expects

---

## ✅ Done

- **2. Add `log.md`** *(2026-04-12)* — Append-only log written by `compile.py`, `wiki_generator.py`, `knowledge_linter.py`. Bootstrapped from existing wiki.
- **4. Concept alias system** *(2026-04-12)* — LLM generates aliases per concept in `save_concept()`, cached in `wiki/aliases.json`. `auto_linker.py` loads alias patterns and rewrites variants to `[[canonical]]` links before the main concept scan.
- **5. Source tracking per concept** *(2026-04-12)* — `save_concept()` now stores `raw/<original-path>` (e.g. `raw/mcs-assembly/chapter-3.md`) in frontmatter `sources:` instead of the flat processed filename. Merge logic unions sources across ingest runs.
- **6. Make the linter actionable** *(2026-04-12)* — `knowledge_linter.py --fix` reads `knowledge_report.md` and applies: patches weak concepts in-place (LLM fill-in), creates stubs for missing/suggested concepts. Logs fix summary to `log.md`.

---

*Last updated: 2026-04-12*
