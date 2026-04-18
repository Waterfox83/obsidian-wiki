# 📘 Handoff Document: LLM-Powered Knowledge Base System

## 🧠 Overview

This system is a **local, self-evolving knowledge base pipeline** built using:

* Markdown files (Obsidian vault)
* Local LLMs via Ollama
* Python scripts for ingestion, transformation, and reasoning

It transforms raw documents into a **linked, deduplicated, evolving knowledge graph**.

---

# 🏗️ System Architecture

## Pipeline Stages

```plaintext
raw/ → compile.py (new files only) → processed/ → wiki_generator.py
                ↘                ↘
            (idempotent)      wiki/ (concept graph)
                   ↘
              auto_linker.py (auto post-step)
                   ↘
   resolve_ghost_concepts.py (auto post-step)
          ↘
   auto_linker.py (post-ghost relink)
          ↘
           knowledge_linter.py (optional post-step)
                   ↘
              qa_agent / skills
```

---

# 📂 Folder Structure

```plaintext
WikiVault/
├── raw/                # source documents (from Web Clipper)
├── processed/          # structured + enriched docs
│   └── images/
├── wiki/               # concept-based knowledge graph
├── outputs/            # Q&A outputs (optional)
├── code/
│   ├── compile.py
│   ├── wiki_generator.py
│   ├── auto_linker.py
│   ├── resolve_ghost_concepts.py
│   ├── knowledge_linter.py
│   └── qa_agent.py / SKILL.md
├── wiki_state.json     # incremental processing state
```

---

# ⚙️ Components

---

## 1️⃣ compile.py (Ingestion + Enrichment)

### Responsibilities

* Read files from `raw/`
* Extract frontmatter (source URL)
* Download images:

  * Primary: Playwright (authenticated pages)
  * Fallback: requests + cookies
* Replace image links with local paths
* Generate image descriptions (LLM)
* Send enriched content to LLM → structured markdown
* Save output to `processed/`

### Key Features

* Playwright persistent session for authenticated content
* Graceful fallback if cookies fail (cron-safe)
* Relative image paths for Obsidian rendering
* Skips raw files that already exist in `processed/` (new-file-only ingestion)

---

## 2️⃣ wiki_generator.py (Concept Extraction + Merge)

### Responsibilities

* Read files from `processed/`
* Extract concepts using LLM
* Use section-aware extraction for large documents
* Split into individual concept sections
* Dedupe near-duplicate concepts across section outputs
* Normalize concept names
* Merge into existing concept files (if present)
* Maintain incremental processing via hashing
* Run post-steps (linking + ghost resolution + relinking + optional linter)

### Key Features

* **Hash-based processing** (avoids reprocessing unchanged files)
* **LLM-based concept merging**
* **Canonical naming + fuzzy matching**
* **Dynamic concept count** (no hard per-doc concept cap)
* **Section-aware extraction** for large files (`LARGE_DOC_THRESHOLD_CHARS`)
* **Automatic post-linking** via `auto_linker.py`
* **Automatic ghost resolution** via `resolve_ghost_concepts.py`
* **Automatic post-ghost relinking** via `auto_linker.py`
* **Optional post-linting** via `knowledge_linter.py`
* Overwrite replaced with merge strategy

---

## 🔥 Critical Logic

### Hash-based idempotency

```python
if file in state and state[file] == current_hash:
    skip
```

### Compile-time idempotency (new-file-only)

```python
if os.path.exists(processed_path):
  skip
```

---

### Concept merging

```python
if file exists:
    merge(old, new)
else:
    create
```

---

### Name normalization

* CamelCase → spaced words
* Remove formatting (`**`)
* Normalize whitespace

---

### Fuzzy deduplication

Uses `SequenceMatcher` to detect near-duplicate concepts.

### Large-doc section extraction

Large files are split into section chunks, extracted independently, then deduped globally.
This avoids losing concepts due to single-pass context compression.

### Post-steps in wiki_generator

`wiki_generator.py` now runs post-steps directly:

1. `auto_linker.py` (default enabled)
2. `resolve_ghost_concepts.py` (default enabled)
3. `auto_linker.py` again (only when ghost resolver runs)
4. `knowledge_linter.py` (default disabled)

Environment controls:

* `WIKI_RUN_AUTO_LINKER=true|false`
* `WIKI_RUN_GHOST_RESOLVER=true|false`
* `WIKI_RUN_KNOWLEDGE_LINTER=true|false`
* `WIKI_POST_STEP_TIMEOUT_SECONDS=<seconds>`

---

## 3️⃣ auto_linker.py (Graph Construction)

### Responsibilities

* Scan all wiki files
* Replace concept mentions with `[[Concept]]` links

### Key Fixes Implemented

* Protect existing `[[links]]` before replacement
* Replace longest concepts first
* Avoid nested linking issues

---

## 4️⃣ knowledge_linter.py (Quality Layer)

### Responsibilities

* Analyze entire wiki using LLM
* Run as optional post-step from `wiki_generator.py` (or standalone)
* Generate report:

  * Missing concepts
  * Weak concepts
  * Duplicate concepts
  * Suggested new concepts

Output:

```plaintext
wiki/knowledge_report.md
```

---

## 5️⃣ resolve_ghost_concepts.py (Ghost Resolution + Cleanup)

### Responsibilities

* Find unresolved `[[Concept]]` links with no backing file
* Generate only grounded concept pages using existing wiki knowledge
* Skip low-confidence concepts (`INSUFFICIENT DATA` / invalid format)
* Merge into similar existing concepts when applicable
* Delete files for candidates skipped due to quality checks
* Write run report and detailed logs

Outputs:

```plaintext
wiki/ghost_resolution_report.json
wiki/ghost_resolution.log
```

---

## 6️⃣ Q&A Layer

### Two approaches:

#### A. qa_agent.py

* Script-based
* Retrieves relevant docs
* Generates answers
* Saves output

#### B. Claude Skill (SKILL.md)

* Tool-based integration
* Agent calls skill dynamically
* Optional persistence

---

# 🧠 Design Principles

---

## 1. Incremental Processing

* Avoid reprocessing unchanged files
* Use content hashing

---

## 2. Concept-Centric Model

* Knowledge stored as concepts, not documents
* Multiple documents contribute to same concept

---

## 3. Merge Over Overwrite

* Concepts evolve over time
* LLM merges new knowledge with existing

---

## 4. Separation of Concerns

| Component         | Responsibility |
| ----------------- | -------------- |
| compile.py        | ingestion      |
| wiki_generator.py | structuring    |
| auto_linker.py    | graph building |
| linter            | quality        |
| agent/skill       | interaction    |

---

## 5. Local-first AI

* Uses Ollama models
* No external dependencies required

---

# ⚠️ Known Challenges & Solutions

---

## 1. Cron + Cookies issue

* macOS Keychain blocks cookie decryption
* Solution:

  * fallback to no-cookie requests
  * use Playwright for auth

---

## 2. Duplicate concepts

* Cause: naming inconsistencies
* Solution:

  * normalization + fuzzy matching

---

## 3. Nested link corruption

Example:

```plaintext
[[[[Launchpad]] Units]]
```

Fix:

* protect existing links before replacement

---

## 4. Context overload (Q&A)

* Avoid loading full wiki
* Use retrieval:

  * keyword filtering
  * embeddings (optional)

---

## 5. Reprocessing same files

* Fixed via `wiki_state.json` (`wiki_generator.py`)
* `compile.py` also skips files already present in `processed/`

---

# 🚀 Current Capabilities

* Multi-document knowledge synthesis
* Image-aware enrichment (proxy)
* Concept graph generation
* Incremental updates
* Auto-linking graph (automatic after wiki generation)
* LLM-powered QA
* Optional knowledge linting (toggleable per run)

---

# 🔮 Future Enhancements

---

## High Priority

* Inline image placement (instead of append)
* Concept alias system
* Source tracking per concept
* Smart retrieval (embedding-based)

---

## Medium Priority

* Concept confidence scoring
* Deduplication refinement
* Merge optimization (avoid unnecessary LLM calls)

---

## Advanced

* Replace Web Clipper with Playwright ingestion
* Auto-learning loop (agent updates wiki)
* UI layer (web app or plugin)

---

# 🧠 Mental Model

This system is NOT:

* a note-taking app
* a RAG pipeline

It IS:

> A **self-evolving knowledge graph powered by LLMs**

---

# 🔑 Key Insight for Next Agent

The most important parts to preserve:

1. **Concept merging logic**
2. **Hash-based incremental processing**
3. **Canonical naming + deduplication**
4. **Separation of pipeline stages**

---

# 🧾 Summary

You are inheriting a system that:

* Ingests raw knowledge
* Structures it into concepts
* Links it into a graph
* Continuously improves it

👉 Your job is NOT to rebuild it
👉 Your job is to **refine and extend it**

---

# ✅ Immediate Next Tasks (Recommended)

1. Improve concept merging quality
2. Add embedding-based retrieval
3. Implement alias mapping
4. Optimize auto-linking precision

---

**End of Handoff**
