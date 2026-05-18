# WikiVault LLM Wiki Tool

Build and maintain the generated wiki in `llm-wiki/` from immutable raw sources in `raw/`, using a deterministic Python builder plus an optional LLM reachable over HTTP.

The main entrypoint is:

```bash
python code/wiki_cli.py
```

## What this tool does

- Scans `raw/` for new or changed markdown files
- Ingests those files into the wiki workflow
- Rebuilds `llm-wiki/` with source links and preserved source-derived detail
- Supports multiple LLM backends by URL
- Answers questions against the generated wiki
- Runs deterministic lint checks over the wiki structure

## Repository layout

```text
raw/                         immutable source corpus
llm-wiki/                    generated wiki output
code/build_compiled_wiki.py  deterministic wiki builder
code/wiki_cli.py             CLI for scan / ingest / build / query / lint
code/wiki_tool_manifest.json auto-ingest manifest written by the CLI
code/wiki_tool_history.json  persisted query / ingest / lint history
code/wiki_tool_state.json    scan state used to detect new and changed files
CLAUDE.md                    repository-specific wiki conventions
```

## Requirements

- Python 3.10+
- A model endpoint only if you want LLM-assisted ingest planning or answer generation

The CLI works without an LLM for:

- `build`
- `scan`
- `ingest --no-llm`
- `query --no-llm`
- `lint`

## Quick start

Build the current wiki:

```bash
python code/wiki_cli.py build
```

See what changed in `raw/`:

```bash
python code/wiki_cli.py scan
```

Ingest new or changed files using heuristic planning only:

```bash
python code/wiki_cli.py ingest --no-llm
```

Ask a question using retrieval only:

```bash
python code/wiki_cli.py query --no-llm "What does Unified Authentication Service do?"
```

Run wiki health checks:

```bash
python code/wiki_cli.py lint
```

## Commands

### `build`

Rebuilds `llm-wiki/` from the deterministic compiled builder.

```bash
python code/wiki_cli.py build
```

This reads the static builder config in `code/build_compiled_wiki.py` plus any dynamic entries from `code/wiki_tool_manifest.json`.

### `scan`

Reports new, changed, and removed markdown files under `raw/`.

```bash
python code/wiki_cli.py scan
```

Notes:

- On the first run, all current files appear as new because no prior scan state exists yet.
- The state file is updated after a successful `ingest`.

### `ingest`

Ingests raw files into the dynamic manifest, records a history entry, and rebuilds `llm-wiki/`.

```bash
python code/wiki_cli.py ingest
```

To ingest specific files:

```bash
python code/wiki_cli.py ingest raw/UAS/khub-architecture.md
python code/wiki_cli.py ingest "raw/MCP authentication and authorization implementation guide.md"
```

Useful flags:

- `--dry-run` - plan the ingest without writing files
- `--no-llm` - skip model calls and use heuristic clustering/planning

Example:

```bash
python code/wiki_cli.py ingest --dry-run --no-llm raw/UAS/khub-security.md
```

How ingest works:

1. Selects either the explicit files you passed or the new/changed files from scan state.
2. Groups files into clusters, usually by top-level folder under `raw/`.
3. Uses the LLM, if configured, to decide whether each cluster should:
   - extend an existing durable page via an overlay, or
   - create a new topic page plus source pack
4. Writes the result into `code/wiki_tool_manifest.json`.
5. Appends an entry to `code/wiki_tool_history.json`.
6. Rebuilds `llm-wiki/`.

### `query`

Answers a question against `llm-wiki/`.

```bash
python code/wiki_cli.py query "How does UAS relate to OprMS?"
```

Useful flags:

- `--no-llm` - show the top retrieved pages instead of generating an answer
- `--top-k N` - change how many pages are retrieved before answer generation

Examples:

```bash
python code/wiki_cli.py query --no-llm "What is Gateway Service?"
python code/wiki_cli.py query --top-k 8 "How does Launchpad Units preview work?"
```

### `lint`

Runs deterministic checks over `llm-wiki/`.

```bash
python code/wiki_cli.py lint
```

Current checks include:

- broken wikilinks
- source references pointing at missing raw files
- orphan pages with no inbound wiki links
- raw markdown files not yet covered by wiki page frontmatter

## LLM configuration

The CLI reads a JSON config file. By default:

```text
wiki-tool.config.json
```

You can generate starter config files with:

```bash
python code/wiki_cli.py init-config --provider openai-chat
python code/wiki_cli.py init-config --provider ollama-generate --path ollama.json
python code/wiki_cli.py init-config --provider http-json --path custom-endpoint.json
```

You can also override config values per command with flags like `--provider`, `--base-url`, `--model`, and `--api-key`.

### OpenAI-compatible config

Use this for any endpoint that supports a `/v1/chat/completions` style API, whether local or remote.

```json
{
  "provider": "openai-chat",
  "base_url": "http://127.0.0.1:1234",
  "model": "google/gemma-3-27b-it",
  "timeout_seconds": 120,
  "temperature": 0.2,
  "headers": {}
}
```

Example:

```bash
python code/wiki_cli.py query \
  --config wiki-tool.config.json \
  "What does the security domain contain?"
```

### Ollama-compatible config

Use this for Ollama-style `/api/generate` endpoints.

```json
{
  "provider": "ollama-generate",
  "base_url": "http://127.0.0.1:11434",
  "model": "llama3.1:8b",
  "timeout_seconds": 120,
  "temperature": 0.2,
  "headers": {}
}
```

### Generic HTTP JSON config

Use this when your model server exposes some other JSON API.

The CLI will:

- interpolate `{{prompt}}`, `{{system}}`, `{{model}}`, and `{{temperature}}` into `request_template`
- POST that JSON to `base_url`
- read the response using `response_path`

Example:

```json
{
  "provider": "http-json",
  "base_url": "http://127.0.0.1:8080/infer",
  "model": "optional-model-name",
  "timeout_seconds": 120,
  "temperature": 0.2,
  "headers": {},
  "request_template": {
    "model": "{{model}}",
    "input": "{{prompt}}",
    "temperature": "{{temperature}}"
  },
  "response_path": "output.text"
}
```

If your endpoint needs auth, either put the header in `headers` or pass `--api-key` for Bearer auth on OpenAI-compatible endpoints.

## Dynamic manifest files

### `code/wiki_tool_manifest.json`

This file is owned by the CLI and is merged into the deterministic builder.

It currently supports:

- `topics` - new topic pages created by auto-ingest
- `source_packs` - explicit source pack pages for those ingests
- `overlays` - source/bullet/source-pack additions to existing durable pages

You normally do not edit it by hand unless you want to fine-tune generated ingest behavior.

### `code/wiki_tool_history.json`

Stores append-only structured entries for:

- ingest operations
- query operations
- lint passes

The compiled wiki builder folds this history into `llm-wiki/log.md`.

### `code/wiki_tool_state.json`

Stores the last seen hash of each raw markdown file so `scan` and `ingest` can detect changes incrementally.

## Typical workflows

### Add new raw files and ingest them

```bash
python code/wiki_cli.py scan
python code/wiki_cli.py ingest
```

If you want to preview the plan first:

```bash
python code/wiki_cli.py ingest --dry-run
```

If no model is available:

```bash
python code/wiki_cli.py ingest --no-llm
```

### Ask questions against the generated wiki

```bash
python code/wiki_cli.py build
python code/wiki_cli.py query "How do UAS and OAZ differ?"
```

If you want retrieval only:

```bash
python code/wiki_cli.py query --no-llm "How do UAS and OAZ differ?"
```

### Rebuild after changing the manifest or builder

```bash
python code/wiki_cli.py build
```

## Troubleshooting

### `No LLM config found; falling back to heuristic ingest planning.`

This is expected when:

- you did not create a config file
- you did not pass any LLM override flags
- you are intentionally running without a model

### `Raw markdown file not found under raw/`

Pass paths relative to the repo root or under `raw/`, for example:

```bash
python code/wiki_cli.py ingest raw/UAS/khub-architecture.md
```

### The first `scan` shows everything as new

That is normal. The CLI only knows what changed after it has written `code/wiki_tool_state.json` during a successful ingest.

### `lint` reports missing raw sources or uncovered files

Those are content coverage issues in the corpus or builder config, not necessarily CLI failures.

## Legacy scripts

The repo still contains older pipeline scripts such as:

- `code/compile.py`
- `code/wiki_generator.py`
- `code/run_pipeline.py`
- `code/knowledge_linter.py`

Those belong to the earlier `raw/ -> processed/ -> wiki/` flow.

The newer CLI documented here is for the `raw/ -> llm-wiki/` flow driven by:

- `code/wiki_cli.py`
- `code/build_compiled_wiki.py`

## Conventions

- `raw/` is immutable
- generated wiki content lives in `llm-wiki/`
- page conventions and workflow rules are documented in `CLAUDE.md`
