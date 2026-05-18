from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import build_compiled_wiki as compiled

DEFAULT_CONFIG_PATH = compiled.REPO / "wiki-tool.config.json"
STATE_PATH = compiled.REPO / "code" / "wiki_tool_state.json"
SUPPORTED_PROVIDERS = ("openai-chat", "ollama-generate", "http-json")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass
class LLMConfig:
    provider: str
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    timeout_seconds: int = 120
    temperature: float = 0.2
    headers: dict[str, str] = field(default_factory=dict)
    request_template: Any = None
    response_path: str | None = None
    chat_path: str = "/v1/chat/completions"
    generate_path: str = "/api/generate"


def read_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def slugify(text: str) -> str:
    value = text.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "auto-ingest"


def humanize(text: str) -> str:
    if not text:
        return "Auto-ingest"
    if text.isupper():
        return text
    words = [part for part in re.split(r"[-_]+", text) if part]
    if not words:
        return "Auto-ingest"
    return " ".join(word.upper() if word.isupper() else word.capitalize() for word in words)


def shorten(text: str, limit: int = 120) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def normalize_page_path(path: str | None) -> str:
    cleaned = str(path or "").strip().lstrip("/")
    if not cleaned:
        return ""
    if cleaned.startswith("llm-wiki/"):
        cleaned = cleaned[len("llm-wiki/") :]
    if not cleaned.endswith(".md"):
        cleaned += ".md"
    return cleaned


def relative_raw_ref(path: Path) -> str:
    return path.resolve().relative_to(compiled.REPO).as_posix()


def file_hash(path: Path) -> str:
    checksum = 1
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            checksum = zlib.adler32(chunk, checksum)
    return f"{checksum & 0xFFFFFFFF:08x}"


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def extract_frontmatter(content: str) -> str:
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    return match.group(1) if match else ""


def extract_title(content: str, fallback: str) -> str:
    frontmatter = extract_frontmatter(content)
    if frontmatter:
        for line in frontmatter.splitlines():
            if line.startswith("title:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    body = compiled.strip_frontmatter_text(content).strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return fallback


def extract_sources(content: str) -> list[str]:
    frontmatter = extract_frontmatter(content)
    if not frontmatter:
        return []

    inline = re.search(r"^sources:\s*\[([^\]]*)\]", frontmatter, re.MULTILINE)
    if inline:
        return dedupe(
            [item.strip().strip('"').strip("'") for item in inline.group(1).split(",") if item.strip()]
        )

    block = re.search(r"^sources:\s*\n((?:\s+-\s+.+\n?)*)", frontmatter, re.MULTILINE)
    if block:
        return dedupe(
            [re.sub(r"^\s+-\s+", "", line).strip().strip('"').strip("'") for line in block.group(1).splitlines()]
        )

    single = re.search(r"^sources:\s*(.+)$", frontmatter, re.MULTILINE)
    if single:
        value = single.group(1).strip().strip('"').strip("'")
        return [value] if value else []

    return []


def extract_wikilinks(content: str) -> list[str]:
    links: list[str] = []
    for raw_link in WIKILINK_RE.findall(content):
        target = raw_link.split("|", 1)[0].strip()
        if target.startswith("llm-wiki/"):
            links.append(normalize_page_path(target))
    return dedupe(links)


def extract_excerpt(content: str, limit: int = 220) -> str:
    body = compiled.strip_frontmatter_text(content).strip()
    for paragraph in re.split(r"\n{2,}", body):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        if all(line.startswith("-") for line in lines):
            continue
        excerpt = shorten(" ".join(lines), limit=limit)
        if excerpt:
            return excerpt
    return ""


def build_wiki_link(path: str) -> str:
    return f"[[llm-wiki/{path[:-3] if path.endswith('.md') else path}]]"


def resolve_raw_path(value: str) -> Path:
    raw_root = compiled.RAW.resolve()
    direct = (compiled.REPO / value).resolve()
    under_raw = (compiled.RAW / value).resolve()

    candidates = [direct, under_raw]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() == ".md":
            if raw_root in candidate.parents:
                return candidate

    raise SystemExit(f"Raw markdown file not found under raw/: {value}")


def snapshot_raw_hashes() -> dict[str, str]:
    snapshot = {}
    for path in sorted(compiled.RAW.rglob("*.md")):
        if any(part.startswith(".") for part in path.parts):
            continue
        snapshot[path.relative_to(compiled.REPO).as_posix()] = file_hash(path)
    return snapshot


def load_state() -> dict[str, dict[str, str]]:
    state = read_json_file(STATE_PATH, {"raw_hashes": {}})
    if not isinstance(state, dict):
        return {"raw_hashes": {}}
    raw_hashes = state.get("raw_hashes", {})
    if not isinstance(raw_hashes, dict):
        raw_hashes = {}
    return {"raw_hashes": {str(key): str(value) for key, value in raw_hashes.items()}}


def save_state(raw_hashes: dict[str, str]) -> None:
    write_json_file(STATE_PATH, {"raw_hashes": raw_hashes})


def diff_raw_hashes(previous: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    previous_keys = set(previous)
    current_keys = set(current)
    return {
        "new": sorted(current_keys - previous_keys),
        "changed": sorted(path for path in current_keys & previous_keys if previous[path] != current[path]),
        "removed": sorted(previous_keys - current_keys),
    }


def empty_manifest() -> dict[str, list[dict]]:
    return {"overlays": [], "topics": [], "source_packs": []}


def load_manifest() -> dict[str, list[dict]]:
    manifest = read_json_file(compiled.MANIFEST_PATH, empty_manifest())
    if not isinstance(manifest, dict):
        return empty_manifest()
    for key in ("overlays", "topics", "source_packs"):
        if not isinstance(manifest.get(key), list):
            manifest[key] = []
    return manifest


def save_manifest(manifest: dict[str, list[dict]]) -> None:
    sorted_manifest = {
        "overlays": sorted(manifest["overlays"], key=lambda item: (item.get("path", ""), item.get("id", ""))),
        "topics": sorted(manifest["topics"], key=lambda item: (item.get("path", ""), item.get("id", ""))),
        "source_packs": sorted(manifest["source_packs"], key=lambda item: (item.get("path", ""), item.get("id", ""))),
    }
    write_json_file(compiled.MANIFEST_PATH, sorted_manifest)


def append_history(operation: str, subject: str, details: str = "") -> None:
    history = read_json_file(compiled.HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "date": date.today().isoformat(),
            "operation": operation,
            "subject": subject,
            "details": details.strip(),
        }
    )
    write_json_file(compiled.HISTORY_PATH, history)


def build_page_catalog() -> list[dict[str, str]]:
    compiled.refresh_dynamic_collections()
    pages: list[dict[str, str]] = []
    for collection in (
        compiled.DOMAIN_PAGES,
        compiled.CONCEPT_PAGES,
        compiled.SERVICE_PAGES,
        compiled.TOPIC_PAGES,
    ):
        for spec in collection:
            pages.append(
                {
                    "path": spec["path"],
                    "title": spec["title"],
                    "summary": spec["summary"],
                    "tags": ", ".join(spec["tags"]),
                }
            )
    return sorted(pages, key=lambda item: item["path"])


def load_wiki_pages() -> list[dict[str, str]]:
    if not compiled.WIKI.exists():
        raise SystemExit("llm-wiki/ does not exist yet. Run `python code/wiki_cli.py build` first.")

    pages = []
    for path in sorted(compiled.WIKI.rglob("*.md")):
        rel_path = path.relative_to(compiled.WIKI).as_posix()
        content = path.read_text(encoding="utf-8", errors="ignore")
        pages.append(
            {
                "path": rel_path,
                "wiki_path": f"llm-wiki/{rel_path}",
                "title": extract_title(content, path.stem),
                "content": content,
                "excerpt": extract_excerpt(content),
            }
        )
    return pages


def rank_pages(question: str, pages: list[dict[str, str]], top_k: int) -> list[dict[str, str]]:
    question_lower = question.lower()
    query_tokens = tokenize(question)
    query_counts = Counter(query_tokens)

    scored: list[tuple[float, dict[str, str]]] = []
    for page in pages:
        title_tokens = Counter(tokenize(page["title"]))
        path_tokens = Counter(tokenize(page["path"]))
        body_tokens = Counter(tokenize(page["content"]))
        score = 0.0
        score += sum(min(query_counts[token], title_tokens[token]) * 8 for token in query_counts)
        score += sum(min(query_counts[token], path_tokens[token]) * 5 for token in query_counts)
        score += sum(min(query_counts[token], body_tokens[token]) * 1.5 for token in query_counts)
        if question_lower in page["title"].lower():
            score += 12
        if question_lower in page["content"].lower():
            score += 8
        if page["path"] in {"index.md", "overview.md"}:
            score += 1
        if page["path"].startswith("sources/"):
            score -= 1
        scored.append((score, page))

    scored.sort(key=lambda item: (item[0], item[1]["path"]), reverse=True)
    selected = [page for score, page in scored if score > 0][:top_k]

    for default_path in ("index.md", "overview.md"):
        if any(page["path"] == default_path for page in selected):
            continue
        for _score, page in scored:
            if page["path"] == default_path:
                selected.append(page)
                break

    return dedupe_pages(selected)[: max(top_k, 2)]


def dedupe_pages(pages: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for page in pages:
        if page["path"] in seen:
            continue
        seen.add(page["path"])
        deduped.append(page)
    return deduped


def build_query_prompt(question: str, pages: list[dict[str, str]], char_limit: int = 18000) -> str:
    chunks: list[str] = []
    current = 0
    for page in pages:
        snippet = page["content"]
        if len(snippet) > 4000:
            snippet = snippet[:4000].rsplit("\n", 1)[0]
        block = f"=== {page['wiki_path']} ===\n{snippet.strip()}\n"
        if current + len(block) > char_limit and chunks:
            break
        chunks.append(block)
        current += len(block)

    context = "\n".join(chunks)
    return (
        "Answer the user's question using only the provided wiki pages.\n"
        "Cite supporting pages inline using Obsidian wikilinks exactly like [[llm-wiki/path/to/page]].\n"
        "If the provided pages are insufficient, say so plainly.\n\n"
        f"Question:\n{question}\n\n"
        f"Wiki pages:\n{context}"
    )


def build_cluster_digest(paths: list[Path]) -> str:
    sections = []
    for path in paths[:8]:
        raw_ref = relative_raw_ref(path)
        headings = compiled.extract_focus_headings(path, max_items=4)
        paragraphs = compiled.extract_detail_paragraphs(path, max_paragraphs=1, char_limit=260)
        list_items = compiled.extract_list_items(path, max_items=3, char_limit=160)

        parts = [f"### {raw_ref}", f"Heading: {compiled.extract_heading(path)}"]
        if headings:
            parts.append("Focus areas:\n" + "\n".join(f"- {item}" for item in headings))
        if paragraphs:
            parts.append("Preserved detail:\n" + "\n\n".join(paragraphs))
        if list_items:
            parts.append("Representative points:\n" + "\n".join(f"- {item}" for item in list_items))
        sections.append("\n".join(parts))
    return "\n\n".join(sections)


def cluster_id_for_path(path: Path) -> str:
    rel_path = path.relative_to(compiled.RAW)
    if len(rel_path.parts) > 1:
        return slugify(rel_path.parts[0])
    return slugify(rel_path.stem)


def group_raw_paths(paths: list[Path]) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(paths):
        grouped[cluster_id_for_path(path)].append(path)
    return dict(sorted(grouped.items()))


def heuristic_cluster_plan(cluster_id: str, source_paths: list[Path]) -> dict[str, Any]:
    cluster_label = source_paths[0].relative_to(compiled.RAW).parts[0] if len(source_paths[0].relative_to(compiled.RAW).parts) > 1 else source_paths[0].stem
    headings = dedupe([compiled.extract_heading(path) for path in source_paths])[:3]
    focus_points = dedupe(
        [item for path in source_paths for item in compiled.extract_focus_headings(path, max_items=2)]
    )[:4]
    bullets = focus_points or [f"Source file: {path.name}" for path in source_paths[:4]]
    summary_subject = ", ".join(headings) if headings else humanize(cluster_label)
    themes = ", ".join(focus_points or headings or [humanize(cluster_label)])

    return {
        "cluster_title": f"{humanize(cluster_label)} incremental sources",
        "slug": f"auto-{cluster_id}",
        "summary": f"Auto-ingested source cluster covering {summary_subject}.",
        "bullets": bullets[:4],
        "themes": themes,
        "related": [],
        "target_path": None,
    }


def first_json_object(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response.")
    return json.loads(text[start : end + 1])


def sanitize_cluster_plan(raw_plan: dict[str, Any], cluster_id: str, source_paths: list[Path], catalog: list[dict[str, str]]) -> dict[str, Any]:
    valid_paths = {item["path"] for item in catalog}
    fallback = heuristic_cluster_plan(cluster_id, source_paths)
    raw_bullets = raw_plan.get("bullets", [])
    raw_related = raw_plan.get("related", [])
    if not isinstance(raw_bullets, list):
        raw_bullets = []
    if not isinstance(raw_related, list):
        raw_related = []

    cluster_title = compiled.clean_title(str(raw_plan.get("cluster_title", ""))) or fallback["cluster_title"]
    slug = slugify(str(raw_plan.get("slug", "")) or f"auto-{cluster_id}")
    if not slug.startswith("auto-"):
        slug = f"auto-{slug}"

    bullets = dedupe([compiled.normalize_text(str(item)) for item in raw_bullets])[:4]
    if not bullets:
        bullets = fallback["bullets"]

    related = dedupe([normalize_page_path(item) for item in raw_related])
    related = [path for path in related if path in valid_paths]

    target_path = normalize_page_path(raw_plan.get("target_path"))
    if target_path not in valid_paths:
        target_path = None

    summary = compiled.normalize_text(str(raw_plan.get("summary", ""))) or fallback["summary"]
    themes = compiled.normalize_text(str(raw_plan.get("themes", ""))) or fallback["themes"]

    return {
        "cluster_title": cluster_title,
        "slug": slug,
        "summary": summary,
        "bullets": bullets,
        "themes": themes,
        "related": related,
        "target_path": target_path,
    }


def plan_cluster_with_llm(
    llm: "LLMClient | None",
    cluster_id: str,
    source_paths: list[Path],
    catalog: list[dict[str, str]],
) -> dict[str, Any]:
    if llm is None:
        return heuristic_cluster_plan(cluster_id, source_paths)

    catalog_lines = "\n".join(
        f"- {item['path']} | {item['title']} | tags={item['tags']} | {item['summary']}" for item in catalog
    )
    digest = build_cluster_digest(source_paths)
    prompt = (
        "You are curating an Obsidian wiki built from raw architecture documents.\n"
        "Decide whether this raw source cluster should extend an existing durable page or become a new auto topic page.\n\n"
        "Return JSON only with this schema:\n"
        "{\n"
        '  "cluster_title": "Human readable title",\n'
        '  "slug": "auto-kebab-case-slug",\n'
        '  "summary": "1-2 factual sentences",\n'
        '  "bullets": ["2-4 concise bullets"],\n'
        '  "themes": "comma-separated themes",\n'
        '  "related": ["existing/page.md"],\n'
        '  "target_path": "existing/page.md or null"\n'
        "}\n\n"
        "Rules:\n"
        "- target_path must be null or one of the candidate page paths.\n"
        "- Prefer an existing page when the cluster clearly deepens it.\n"
        "- If target_path is null, the cluster will become a new auto topic page.\n"
        "- related must contain only candidate page paths.\n"
        "- Do not invent facts beyond the provided source digests.\n\n"
        f"Candidate pages:\n{catalog_lines}\n\n"
        f"Cluster id guess: {cluster_id}\n\n"
        f"Raw source digests:\n{digest}\n"
    )

    response = llm.complete(prompt)
    return sanitize_cluster_plan(first_json_object(response), cluster_id, source_paths, catalog)


def upsert_entry(entries: list[dict], entry: dict) -> None:
    entry_id = str(entry.get("id", ""))
    entry_path = str(entry.get("path", ""))
    for index, current in enumerate(entries):
        if current.get("id") == entry_id or current.get("path") == entry_path:
            entries[index] = entry
            return
    entries.append(entry)


def remove_entry(entries: list[dict], entry_id: str) -> list[dict]:
    return [entry for entry in entries if str(entry.get("id", "")) != entry_id]


def apply_cluster_plan(
    manifest: dict[str, list[dict]],
    cluster_id: str,
    source_refs: list[str],
    plan: dict[str, Any],
    catalog: list[dict[str, str]],
) -> dict[str, Any]:
    valid_paths = {item["path"] for item in catalog}
    existing_topic = next((entry for entry in manifest["topics"] if str(entry.get("id", "")) == cluster_id), None)
    existing_source_pack = next(
        (entry for entry in manifest["source_packs"] if str(entry.get("id", "")) == cluster_id),
        None,
    )
    source_pack_path = (
        str(existing_source_pack.get("path", ""))
        if existing_source_pack
        else f"sources/{plan['slug']}-source-pack.md"
    )
    related = dedupe([path for path in plan["related"] if path in valid_paths])
    target_path = plan["target_path"] if plan["target_path"] in valid_paths else None

    source_pack_title = (
        f"{plan['cluster_title']} incremental source pack" if target_path else f"{plan['cluster_title']} source pack"
    )
    source_pack_entry = {
        "id": cluster_id,
        "path": source_pack_path,
        "title": source_pack_title,
        "tags": dedupe(["source-pack", "auto-ingest", slugify(plan["cluster_title"])]),
        "sources": source_refs,
        "summary": plan["summary"],
        "themes": plan["themes"],
        "related": dedupe(([target_path] if target_path else []) + related),
    }
    upsert_entry(manifest["source_packs"], source_pack_entry)

    touched_paths = [source_pack_path]
    removed_paths: list[str] = []
    if target_path:
        if existing_topic:
            removed_paths.append(str(existing_topic.get("path", "")))
        manifest["topics"] = remove_entry(manifest["topics"], cluster_id)
        overlay_entry = {
            "id": cluster_id,
            "path": target_path,
            "sources": source_refs,
            "bullets": plan["bullets"],
            "related": dedupe(related + [source_pack_path]),
            "extra_source_packs": [source_pack_path],
        }
        upsert_entry(manifest["overlays"], overlay_entry)
        touched_paths.append(target_path)
    else:
        manifest["overlays"] = remove_entry(manifest["overlays"], cluster_id)
        topic_path = str(existing_topic.get("path", "")) if existing_topic else f"topics/{plan['slug']}.md"
        topic_entry = {
            "id": cluster_id,
            "path": topic_path,
            "title": plan["cluster_title"],
            "tags": dedupe(["topic", "auto-ingest", slugify(plan["cluster_title"])]),
            "sources": source_refs,
            "summary": plan["summary"],
            "bullets": plan["bullets"],
            "related": dedupe(related + [source_pack_path]),
            "source_packs": [source_pack_path],
        }
        upsert_entry(manifest["topics"], topic_entry)
        touched_paths.append(topic_path)

    return {
        "cluster_id": cluster_id,
        "source_pack_path": source_pack_path,
        "target_path": target_path,
        "touched_paths": touched_paths,
        "removed_paths": [path for path in removed_paths if path],
        "plan": plan,
    }


def build_ingest_history_details(touched_pages: list[str], source_refs: list[str]) -> str:
    page_links = ", ".join(build_wiki_link(path) for path in dedupe(touched_pages))
    lines = [f"Pages created/updated: {page_links}.", "", "Sources covered:"]
    lines.extend(f"- `{raw_ref}`" for raw_ref in dedupe(source_refs))
    return "\n".join(lines)


def load_llm_config(args: argparse.Namespace) -> LLMConfig | None:
    config_path = Path(getattr(args, "config", DEFAULT_CONFIG_PATH))
    config_data = read_json_file(config_path, {}) if config_path.exists() else {}
    if not isinstance(config_data, dict):
        raise SystemExit(f"Config file is not a JSON object: {config_path}")

    provider = getattr(args, "provider", None) or config_data.get("provider")
    if not provider:
        return None
    if provider not in SUPPORTED_PROVIDERS:
        raise SystemExit(f"Unsupported provider '{provider}'. Use one of: {', '.join(SUPPORTED_PROVIDERS)}")

    base_url = getattr(args, "base_url", None) or config_data.get("base_url", "")
    model = getattr(args, "model", None) or config_data.get("model", "")
    api_key = getattr(args, "api_key", None)
    if api_key is None:
        api_key = config_data.get("api_key")
    timeout_seconds = getattr(args, "timeout_seconds", None)
    if timeout_seconds is None:
        timeout_seconds = int(config_data.get("timeout_seconds", 120))
    temperature = getattr(args, "temperature", None)
    if temperature is None:
        temperature = float(config_data.get("temperature", 0.2))

    headers = config_data.get("headers", {})
    if not isinstance(headers, dict):
        raise SystemExit("Config field 'headers' must be an object.")

    config = LLMConfig(
        provider=provider,
        base_url=str(base_url).strip(),
        model=str(model).strip(),
        api_key=str(api_key).strip() if api_key else None,
        timeout_seconds=int(timeout_seconds),
        temperature=float(temperature),
        headers={str(key): str(value) for key, value in headers.items()},
        request_template=config_data.get("request_template"),
        response_path=config_data.get("response_path"),
        chat_path=str(config_data.get("chat_path", "/v1/chat/completions")),
        generate_path=str(config_data.get("generate_path", "/api/generate")),
    )

    if config.provider in {"openai-chat", "ollama-generate"} and (not config.base_url or not config.model):
        raise SystemExit("LLM config requires both base_url and model for this provider.")
    if config.provider == "http-json" and (
        not config.base_url or config.request_template is None or not config.response_path
    ):
        raise SystemExit(
            "http-json config requires base_url, request_template, and response_path."
        )

    return config


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def complete(self, prompt: str, system: str | None = None) -> str:
        if self.config.provider == "openai-chat":
            return self._complete_openai_chat(prompt, system=system)
        if self.config.provider == "ollama-generate":
            return self._complete_ollama_generate(prompt, system=system)
        if self.config.provider == "http-json":
            return self._complete_http_json(prompt, system=system)
        raise RuntimeError(f"Unsupported provider: {self.config.provider}")

    def _post_json(self, url: str, payload: Any) -> Any:
        headers = {"Content-Type": "application/json", **self.config.headers}
        if self.config.api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)

    def _resolve_endpoint(self, suffix: str) -> str:
        if self.config.base_url.rstrip("/").endswith(suffix):
            return self.config.base_url.rstrip("/")
        return self.config.base_url.rstrip("/") + suffix

    def _complete_openai_chat(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": False,
        }
        body = self._post_json(self._resolve_endpoint(self.config.chat_path), payload)
        choices = body.get("choices", [])
        if not choices:
            raise RuntimeError(f"Unexpected response schema: {body}")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content).strip()

    def _complete_ollama_generate(self, prompt: str, system: str | None = None) -> str:
        full_prompt = prompt if not system else f"{system}\n\n{prompt}"
        payload = {
            "model": self.config.model,
            "prompt": full_prompt,
            "stream": False,
            "options": {"temperature": self.config.temperature},
        }
        body = self._post_json(self._resolve_endpoint(self.config.generate_path), payload)
        if "response" in body:
            return str(body["response"]).strip()
        if "message" in body and isinstance(body["message"], dict):
            return str(body["message"].get("content", "")).strip()
        raise RuntimeError(f"Unexpected response schema: {body}")

    def _complete_http_json(self, prompt: str, system: str | None = None) -> str:
        payload = interpolate_template(
            self.config.request_template,
            {
                "prompt": prompt,
                "system": system or "",
                "model": self.config.model,
                "temperature": self.config.temperature,
            },
        )
        body = self._post_json(self.config.base_url, payload)
        value = extract_response_path(body, self.config.response_path or "")
        if isinstance(value, str):
            return value.strip()
        return json.dumps(value, ensure_ascii=False)


def interpolate_template(template: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {key: interpolate_template(value, replacements) for key, value in template.items()}
    if isinstance(template, list):
        return [interpolate_template(value, replacements) for value in template]
    if isinstance(template, str):
        for key, value in replacements.items():
            placeholder = f"{{{{{key}}}}}"
            if template == placeholder:
                return value
        rendered = template
        for key, value in replacements.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        return rendered
    return template


def extract_response_path(data: Any, path: str) -> Any:
    current = data
    for segment in path.split("."):
        if not segment:
            continue
        if isinstance(current, list):
            current = current[int(segment)]
        else:
            current = current[segment]
    return current


def cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing config: {path}")

    if args.provider == "openai-chat":
        payload = {
            "provider": "openai-chat",
            "base_url": "http://127.0.0.1:1234",
            "model": "google/gemma-3-27b-it",
            "timeout_seconds": 120,
            "temperature": 0.2,
            "headers": {},
        }
    elif args.provider == "ollama-generate":
        payload = {
            "provider": "ollama-generate",
            "base_url": "http://127.0.0.1:11434",
            "model": "llama3.1:8b",
            "timeout_seconds": 120,
            "temperature": 0.2,
            "headers": {},
        }
    else:
        payload = {
            "provider": "http-json",
            "base_url": "http://127.0.0.1:8080/infer",
            "model": "optional-model-name",
            "timeout_seconds": 120,
            "temperature": 0.2,
            "headers": {},
            "request_template": {
                "model": "{{model}}",
                "input": "{{prompt}}",
                "temperature": "{{temperature}}",
            },
            "response_path": "output.text",
        }

    write_json_file(path, payload)
    print(f"Wrote config template to {path}")
    return 0


def cmd_build(_args: argparse.Namespace) -> int:
    compiled.main()
    print(f"Manifest: {compiled.MANIFEST_PATH}")
    return 0


def cmd_scan(_args: argparse.Namespace) -> int:
    state = load_state()
    current = snapshot_raw_hashes()
    diff = diff_raw_hashes(state["raw_hashes"], current)

    if not state["raw_hashes"]:
        print("No prior scan state found; all current raw markdown files are treated as new.")

    print(f"New files: {len(diff['new'])}")
    for path in diff["new"]:
        print(f"  + {path}")

    print(f"Changed files: {len(diff['changed'])}")
    for path in diff["changed"]:
        print(f"  ~ {path}")

    print(f"Removed files: {len(diff['removed'])}")
    for path in diff["removed"]:
        print(f"  - {path}")

    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    current_hashes = snapshot_raw_hashes()
    state = load_state()
    selected_paths: list[Path]

    if args.paths:
        selected_paths = [resolve_raw_path(value) for value in args.paths]
    else:
        diff = diff_raw_hashes(state["raw_hashes"], current_hashes)
        changed_refs = diff["new"] + diff["changed"]
        selected_paths = [(compiled.REPO / raw_ref).resolve() for raw_ref in changed_refs]

    if not selected_paths:
        print("No new or changed raw markdown files found.")
        return 0

    manifest = load_manifest()
    catalog = build_page_catalog()
    llm_client: LLMClient | None = None

    if not args.no_llm:
        config = load_llm_config(args)
        if config is not None:
            llm_client = LLMClient(config)
        else:
            print("No LLM config found; falling back to heuristic ingest planning.")

    grouped = group_raw_paths(selected_paths)
    touched_pages: list[str] = []
    ingested_sources: list[str] = []
    removed_pages: list[str] = []

    for cluster_id, cluster_paths in grouped.items():
        existing_source_pack = next(
            (
                entry
                for entry in manifest["source_packs"]
                if str(entry.get("id", "")) == cluster_id
                or str(entry.get("path", "")) == f"sources/auto-{cluster_id}-source-pack.md"
            ),
            None,
        )
        source_refs = dedupe(
            list(existing_source_pack.get("sources", [])) if existing_source_pack else []
            + [relative_raw_ref(path) for path in cluster_paths]
        )
        cluster_source_paths = [(compiled.REPO / raw_ref).resolve() for raw_ref in source_refs]

        try:
            plan = plan_cluster_with_llm(llm_client, cluster_id, cluster_source_paths, catalog)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as exc:
            print(f"[{cluster_id}] LLM planning failed; using heuristic fallback: {exc}")
            plan = heuristic_cluster_plan(cluster_id, cluster_source_paths)

        result = apply_cluster_plan(manifest, cluster_id, source_refs, plan, catalog)
        touched_pages.extend(result["touched_paths"])
        ingested_sources.extend(source_refs)
        removed_pages.extend(result["removed_paths"])

        if result["target_path"]:
            print(f"[{cluster_id}] overlay -> {result['target_path']} + {result['source_pack_path']}")
        else:
            topic_path = next(path for path in result["touched_paths"] if path.startswith("topics/"))
            print(f"[{cluster_id}] topic -> {topic_path} + {result['source_pack_path']}")

    if args.dry_run:
        print("\nDry run only — no files written.")
        return 0

    save_manifest(manifest)
    for relative_path in dedupe(removed_pages):
        candidate = compiled.WIKI / relative_path
        if candidate.exists():
            candidate.unlink()
    append_history(
        "ingest",
        f"auto-ingest {len(selected_paths)} raw files",
        build_ingest_history_details(touched_pages, ingested_sources),
    )
    compiled.main()
    save_state(current_hashes)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    pages = load_wiki_pages()
    selected_pages = rank_pages(args.question, pages, top_k=max(args.top_k, 1))

    if args.no_llm:
        print("Top matching pages:")
        for page in selected_pages:
            print(f"- {build_wiki_link(page['path'])} — {page['excerpt']}")
        return 0

    config = load_llm_config(args)
    if config is None:
        print("No LLM config found; showing retrieved pages instead.")
        for page in selected_pages:
            print(f"- {build_wiki_link(page['path'])} — {page['excerpt']}")
        return 0

    llm = LLMClient(config)
    prompt = build_query_prompt(args.question, selected_pages)

    try:
        answer = llm.complete(
            prompt,
            system="You answer questions against a local markdown wiki and cite the relevant wiki pages inline.",
        )
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Query failed: {exc}") from exc

    append_history(
        "query",
        shorten(args.question, limit=90),
        "Answer filed as: not filed\n"
        + "Pages consulted: "
        + ", ".join(build_wiki_link(page["path"]) for page in selected_pages),
    )
    compiled.write(compiled.WIKI / "log.md", compiled.build_log())
    print(answer.strip())
    return 0


def cmd_lint(_args: argparse.Namespace) -> int:
    pages = load_wiki_pages()
    existing_paths = {page["path"] for page in pages}
    inbound_links: Counter[str] = Counter()
    broken_links: list[tuple[str, str]] = []
    missing_sources: list[tuple[str, str]] = []
    referenced_raw_sources: set[str] = set()

    for page in pages:
        for target in extract_wikilinks(page["content"]):
            if target in existing_paths:
                inbound_links[target] += 1
            else:
                broken_links.append((page["path"], target))
        for raw_ref in extract_sources(page["content"]):
            referenced_raw_sources.add(raw_ref)
            if not (compiled.REPO / raw_ref).exists():
                missing_sources.append((page["path"], raw_ref))

    raw_files = {
        path.relative_to(compiled.REPO).as_posix()
        for path in compiled.RAW.rglob("*.md")
        if not any(part.startswith(".") for part in path.parts)
    }
    uncovered_raw = sorted(raw_files - referenced_raw_sources)
    orphan_pages = sorted(
        page["path"]
        for page in pages
        if page["path"] not in {"overview.md", "index.md", "log.md"} and inbound_links[page["path"]] == 0
    )

    print("LLM Wiki lint report")
    print(f"- Broken wikilinks: {len(broken_links)}")
    for source_path, target_path in broken_links[:20]:
        print(f"  - {build_wiki_link(source_path)} -> [[llm-wiki/{target_path[:-3] if target_path.endswith('.md') else target_path}]]")

    print(f"- Missing raw sources: {len(missing_sources)}")
    for page_path, raw_ref in missing_sources[:20]:
        print(f"  - {build_wiki_link(page_path)} -> `{raw_ref}`")

    print(f"- Orphan pages: {len(orphan_pages)}")
    for page_path in orphan_pages[:20]:
        print(f"  - {build_wiki_link(page_path)}")

    print(f"- Uncovered raw markdown files: {len(uncovered_raw)}")
    for raw_ref in uncovered_raw[:20]:
        print(f"  - `{raw_ref}`")

    append_history(
        "lint",
        "wiki health check",
        "\n".join(
            [
                f"Issues found: {len(broken_links) + len(missing_sources) + len(orphan_pages) + len(uncovered_raw)}",
                f"Broken wikilinks: {len(broken_links)}",
                f"Missing raw sources: {len(missing_sources)}",
                f"Orphan pages: {len(orphan_pages)}",
                f"Uncovered raw markdown files: {len(uncovered_raw)}",
            ]
        ),
    )
    compiled.write(compiled.WIKI / "log.md", compiled.build_log())
    return 0


def add_llm_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the JSON LLM config file.")
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, help="Override the provider from the config file.")
    parser.add_argument("--base-url", help="Override the model server base URL from the config file.")
    parser.add_argument("--model", help="Override the model name from the config file.")
    parser.add_argument("--api-key", help="Optional bearer token for OpenAI-compatible endpoints.")
    parser.add_argument("--timeout-seconds", type=int, help="HTTP timeout for model calls.")
    parser.add_argument("--temperature", type=float, help="Sampling temperature for model calls.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the llm-wiki with deterministic builds plus pluggable URL-based LLM backends.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config = subparsers.add_parser("init-config", help="Write a starter JSON config for an LLM endpoint.")
    init_config.add_argument("--path", default=str(DEFAULT_CONFIG_PATH), help="Where to write the config file.")
    init_config.add_argument("--provider", choices=SUPPORTED_PROVIDERS, default="openai-chat")

    subparsers.add_parser("build", help="Rebuild llm-wiki/ from the deterministic compiled builder.")
    subparsers.add_parser("scan", help="Scan raw/ for new, changed, or removed markdown files.")

    ingest = subparsers.add_parser("ingest", help="Ingest raw files into the auto-ingest manifest and rebuild llm-wiki/.")
    add_llm_options(ingest)
    ingest.add_argument("paths", nargs="*", help="Specific raw markdown files to ingest. Defaults to new/changed files from scan state.")
    ingest.add_argument("--dry-run", action="store_true", help="Plan the ingest without writing manifest or wiki files.")
    ingest.add_argument("--no-llm", action="store_true", help="Skip model calls and use heuristic cluster planning.")

    query = subparsers.add_parser("query", help="Answer a question against llm-wiki/.")
    add_llm_options(query)
    query.add_argument("question", help="The question to answer from the wiki.")
    query.add_argument("--top-k", type=int, default=5, help="How many wiki pages to retrieve before answering.")
    query.add_argument("--no-llm", action="store_true", help="Skip model calls and only show retrieved pages.")

    subparsers.add_parser("lint", help="Run deterministic wiki health checks.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-config":
        return cmd_init_config(args)
    if args.command == "build":
        return cmd_build(args)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "ingest":
        return cmd_ingest(args)
    if args.command == "query":
        return cmd_query(args)
    if args.command == "lint":
        return cmd_lint(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
