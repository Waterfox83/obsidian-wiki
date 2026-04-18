# WikiVault Pipeline

Build a local, concept-driven knowledge base from raw markdown using Python + a local LLM.

## Features

- Ingests raw documents from `raw/` into structured markdown in `processed/`
- Downloads and relinks images for local rendering
- Extracts and merges concepts into `wiki/` pages
- Auto-links concept mentions (`[[Concept]]`) across wiki files
- Resolves ghost concepts and optionally lints wiki quality
- Uses incremental/idempotent processing to skip unchanged work

## Pipeline

```text
raw/ -> compile.py -> processed/ -> wiki_generator.py -> wiki/
																			-> auto_linker.py
																			-> resolve_ghost_concepts.py
																			-> auto_linker.py (re-link)
																			-> knowledge_linter.py (optional)
```

## Project Layout

The scripts in this folder expect these sibling directories at the repo root:

- `raw/` — input/source markdown files
- `processed/` — transformed markdown output
- `wiki/` — concept graph markdown files and reports
- `code/` — pipeline scripts

## Requirements

- Python 3.10+
- A local OpenAI-compatible LLM endpoint (`/v1/chat/completions`)
- Optional for richer image ingestion:
	- Playwright
	- `browser_cookie3`

## Configuration

Environment variables used by scripts:

- `LLM_BASE_URL` (default: `http://127.0.0.1:1234`)
- `LLM_MODEL` (default: `google/gemma-4-26b-a4b`)
- `WIKI_RUN_AUTO_LINKER` (`true|false`)
- `WIKI_RUN_GHOST_RESOLVER` (`true|false`)
- `WIKI_RUN_KNOWLEDGE_LINTER` (`true|false`)

## Quick Start

From repository root:

```bash
python code/run_pipeline.py
```

Run just ingestion/compile:

```bash
python code/compile.py
```

## Core Scripts

- `compile.py` — ingestion + enrichment + structured markdown generation
- `wiki_generator.py` — concept extraction, merge, dedupe, and post-steps
- `auto_linker.py` — internal concept linking
- `resolve_ghost_concepts.py` — ghost concept resolution and cleanup
- `knowledge_linter.py` — optional wiki quality analysis/reporting
- `run_pipeline.py` — orchestration entry point

## Troubleshooting

- `Connection refused` on `/v1/chat/completions`
	- Your LLM service is not running or `LLM_BASE_URL` is incorrect.
- `dyld ... libintl.8.dylib`
	- Your Python binary is linked to missing Homebrew `gettext` libraries.

## Notes

- This repo is currently set up to commit code artifacts only (excluding local env/profile directories such as `venv/` and `playwright-profile/`).

