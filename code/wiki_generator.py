import re
import os
import json
import urllib.request
import hashlib
import subprocess
import sys
from datetime import date
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, "processed")
WIKI_DIR = os.path.join(BASE_DIR, "wiki")
STATE_FILE = os.path.join(BASE_DIR, "wiki_state.json")

os.makedirs(WIKI_DIR, exist_ok=True)

MODEL = "gemma4:26b"
REQUEST_TIMEOUT_SECONDS = int(os.getenv("WIKI_LLM_TIMEOUT_SECONDS", "300"))
MIN_CONCEPT_NAME_LEN = int(os.getenv("WIKI_MIN_CONCEPT_NAME_LEN", "3"))
MAX_TAGS_PER_CONCEPT = int(os.getenv("WIKI_MAX_TAGS_PER_CONCEPT", "5"))
SECTION_TARGET_CHARS = int(os.getenv("WIKI_SECTION_TARGET_CHARS", "6000"))
SECTION_MAX_CHARS = int(os.getenv("WIKI_SECTION_MAX_CHARS", "7000"))
LARGE_DOC_THRESHOLD_CHARS = int(os.getenv("WIKI_LARGE_DOC_THRESHOLD_CHARS", "9000"))
CONCEPT_DEDUPE_SIMILARITY = float(os.getenv("WIKI_CONCEPT_DEDUPE_SIMILARITY", "0.93"))
RUN_AUTO_LINKER = os.getenv("WIKI_RUN_AUTO_LINKER", "true").lower() == "true"
RUN_GHOST_RESOLVER = os.getenv("WIKI_RUN_GHOST_RESOLVER", "true").lower() == "true"
RUN_KNOWLEDGE_LINTER = os.getenv("WIKI_RUN_KNOWLEDGE_LINTER", "false").lower() == "true"
GENERATE_SERVICE_PAGES = os.getenv("WIKI_GENERATE_SERVICE_PAGES", "true").lower() == "true"
POST_STEP_TIMEOUT_SECONDS = int(os.getenv("WIKI_POST_STEP_TIMEOUT_SECONDS", "600"))

TODAY = date.today().isoformat()
LOG_FILE = os.path.join(WIKI_DIR, "log.md")
INDEX_FILE = os.path.join(WIKI_DIR, "index.md")
ALIASES_FILE = os.path.join(WIKI_DIR, "aliases.json")


# -----------------------------
# HASHING
# -----------------------------
def file_hash(filepath):
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# -----------------------------
# STATE MANAGEMENT
# -----------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"concepts": {}, "service_pages": {}}

    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)
        # Migrate old flat format {filename: hash} → nested format
        if raw and not isinstance(next(iter(raw.values()), None), dict):
            return {"concepts": raw, "service_pages": {}}
        if "concepts" not in raw:
            raw["concepts"] = {}
        if "service_pages" not in raw:
            raw["service_pages"] = {}
        return raw
    except Exception:
        return {"concepts": {}, "service_pages": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# -----------------------------
# LLM CALL
# -----------------------------
def ask_llm(prompt):
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False
    }).encode()

    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())["response"]


# -----------------------------
# CLEAN HELPERS
# -----------------------------
def clean_title(title):
    return title.replace("**", "").strip()


def processed_to_raw_path(processed_filename):
    """Convert flat processed filename (uses __ as separator) back to raw/ path.

    e.g. ``mcs-assembly__chapter-3.md`` → ``raw/mcs-assembly/chapter-3.md``
    """
    rel_path = processed_filename.replace("__", "/")
    return f"raw/{rel_path}"


def slugify_tag(text):
    tag = text.lower().strip()
    tag = re.sub(r"\s+", "-", tag)
    tag = re.sub(r"[^a-z0-9_\-/]", "", tag)
    tag = re.sub(r"-+", "-", tag).strip("-")
    return tag or "concept"


TAG_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "under", "within",
    "using", "used", "use", "through", "across", "about", "when", "where", "what", "which",
    "is", "are", "was", "were", "be", "been", "being", "it", "its", "as", "at", "by", "on",
    "of", "to", "in", "or", "an", "a", "can", "could", "should", "may", "might", "will",
    "key", "definition", "explanation", "related", "concepts", "concept", "points"
}


def extract_content_tags(concept_name, concept_md):
    concept_slug = slugify_tag(concept_name)
    tags = []

    related_links = re.findall(r"\[\[([^\]]+)\]\]", concept_md)
    for link in related_links:
        candidate = slugify_tag(link)
        if candidate and candidate != concept_slug and candidate not in tags:
            tags.append(candidate)

    text = re.sub(r"\[\[[^\]]+\]\]", " ", concept_md)
    text = re.sub(r"[#*`>\-]", " ", text)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-/]{2,}", text.lower())

    freq = {}
    for word in words:
        if word in TAG_STOPWORDS:
            continue
        if word == concept_slug:
            continue
        freq[word] = freq.get(word, 0) + 1

    ranked_words = sorted(freq.items(), key=lambda item: item[1], reverse=True)
    for word, _ in ranked_words:
        candidate = slugify_tag(word)
        if candidate and candidate != concept_slug and candidate not in tags:
            tags.append(candidate)
        if len(tags) >= MAX_TAGS_PER_CONCEPT:
            break

    if not tags:
        tags = ["knowledge"]

    return tags[:MAX_TAGS_PER_CONCEPT]


# -----------------------------
# CONCEPT EXTRACTION
# -----------------------------
def extract_concepts(content):
    prompt = f"""Extract only high-level, durable concepts from the document.

A concept is DURABLE if it describes something that would remain architecturally or
domain-level true 6+ months from now, regardless of implementation detail changes
(e.g., a naming convention change, a library upgrade, or a new endpoint added).

Focus on architecture-level and business/domain-level concepts.
Do NOT create standalone concepts for low-level implementation details such as:
- individual methods/functions/classes
- single API calls or endpoints unless central to the document
- specific variable names, config keys, or data page names
- one-off technical terms that are only minor details
- tool internals that should be covered under a broader concept

Produce a comprehensive set of high-value concepts without forcing unnecessary breadth.
It is okay to return many concepts for large, dense documents if each concept is meaningful and distinct.
Choose the number of concepts dynamically based on actual importance and scope in the document.
Do NOT force a fixed number: include all high-value concepts, and omit low-value or redundant ones.
Order concepts from most important to least.

If this is a deep-dive service document (controllers, endpoints, ZIP/component assembly, auth checks),
consolidate into parent concepts such as:
- API Surface and Controller Responsibilities
- Authentication and Tenant/Isolation Validation
- Artifact Discovery and Capability Advertisement
- Bundle vs Component Retrieval Model
- Assembly Pipeline and Context Construction
- Version-Gated Behavior and Compatibility Rules

Extraction behavior for deep-dive docs:
- Keep endpoint lists/tables as Key Points under a broader concept, not separate concepts
- Preserve critical contracts (auth required, claim/path cross-checks, response type like ZIP/JSON)
- Preserve durable behavioral rules (e.g., version threshold changes naming strategy)
- Summarize implementation details as architecture flow (context creation -> factory/delegation)
- Exclude noisy route-by-route repetition unless required to explain architecture

For each concept, create a separate markdown section formatted EXACTLY as:

## <Concept Name>

### Definition
One sentence: what this concept IS (not how it works).

### Explanation
2-5 sentences: how and why it works architecturally in this codebase.

### Key Points
- ...

### Related Concepts
- [[Concept A]]
- [[Concept B]]

Rules:
- Do NOT use bold (**)
- Use Obsidian [[Concept Name]] links (bare concept names, no path prefix)
- Concept names must be human-readable and broad (2-6 words)
- Merge closely related details into one parent concept section
- Prefer 4-7 key points per concept, each concise and durable
- Related Concepts should reference any concept that is meaningfully related,
  across ALL documents in the knowledge base — not just siblings from this document.
  Use the exact concept name as it would appear as a wiki page title.
- Output ONLY markdown

Document:
{content}
"""
    return ask_llm(prompt)


def split_document_sections(content):
    heading_pattern = re.compile(r"^#{1,3}\s+.+")
    lines = content.splitlines()
    sections = []

    current_title = "Document Overview"
    current_lines = []

    for line in lines:
        if heading_pattern.match(line) and current_lines:
            section_text = "\n".join(current_lines).strip()
            if section_text:
                sections.append((current_title, section_text))
            current_title = re.sub(r"^#{1,3}\s+", "", line).strip() or "Untitled Section"
            current_lines = [line]
        else:
            if heading_pattern.match(line):
                current_title = re.sub(r"^#{1,3}\s+", "", line).strip() or "Untitled Section"
            current_lines.append(line)

    if current_lines:
        section_text = "\n".join(current_lines).strip()
        if section_text:
            sections.append((current_title, section_text))

    if not sections:
        return [("Document", content)]

    packed = []
    chunk_title = None
    chunk_parts = []
    chunk_len = 0

    for title, section_text in sections:
        section_len = len(section_text)
        if section_len > SECTION_MAX_CHARS:
            start = 0
            while start < section_len:
                part = section_text[start:start + SECTION_MAX_CHARS]
                suffix = "" if start == 0 else " (cont.)"
                packed.append((f"{title}{suffix}", part))
                start += SECTION_MAX_CHARS
            continue

        if chunk_len + section_len > SECTION_TARGET_CHARS and chunk_parts:
            packed.append((chunk_title or "Document Chunk", "\n\n".join(chunk_parts)))
            chunk_title = title
            chunk_parts = [section_text]
            chunk_len = section_len
        else:
            if not chunk_parts:
                chunk_title = title
            chunk_parts.append(section_text)
            chunk_len += section_len

    if chunk_parts:
        packed.append((chunk_title or "Document Chunk", "\n\n".join(chunk_parts)))

    return packed


def extract_concepts_for_section(section_title, section_content, section_index, total_sections):
    prompt = f"""You are extracting concepts from one section of a larger document.

Section: {section_title}
Section index: {section_index} of {total_sections}

A concept is DURABLE if it describes something that would remain architecturally or
domain-level true 6+ months from now, regardless of implementation detail changes.

Extract only high-level, durable concepts that are clearly supported by this section.
Do NOT create standalone concepts for low-level implementation details.
Keep concepts distinct; do not over-merge unrelated areas.

For each concept, format EXACTLY as:

## <Concept Name>

### Definition
One sentence: what this concept IS (not how it works).

### Explanation
2-5 sentences: how and why it works architecturally in this codebase.

### Key Points
- ...

### Related Concepts
- [[Concept A]]
- [[Concept B]]

Rules:
- Do NOT use bold (**)
- Use Obsidian [[Concept Name]] links (bare concept names, no path prefix)
- Concept names must be human-readable and broad (2-6 words)
- Prefer 4-6 key points per concept
- Related Concepts should reference any concept that is meaningfully related,
  across ALL documents in the knowledge base — not just siblings from this section.
- Output ONLY markdown

Section content:
{section_content}
"""
    return ask_llm(prompt)


def concept_name_from_md(concept_md):
    first_line = concept_md.split("\n", 1)[0]
    raw_name = clean_title(first_line.replace("##", "").strip())
    return normalize_concept_name(raw_name)


def dedupe_concepts(concepts):
    deduped = []
    deduped_names = []

    for concept_md in concepts:
        name = concept_name_from_md(concept_md)
        if not name:
            continue

        found_index = None
        for idx, existing_name in enumerate(deduped_names):
            similarity = SequenceMatcher(None, name.lower(), existing_name.lower()).ratio()
            if similarity >= CONCEPT_DEDUPE_SIMILARITY:
                found_index = idx
                break

        if found_index is None:
            deduped.append(concept_md)
            deduped_names.append(name)
        else:
            if len(concept_md) > len(deduped[found_index]):
                deduped[found_index] = concept_md
                deduped_names[found_index] = name

    return deduped


def extract_concepts_section_aware(content):
    if len(content) <= LARGE_DOC_THRESHOLD_CHARS:
        return extract_concepts(content)

    sections = split_document_sections(content)
    total_sections = len(sections)
    all_concepts = []

    for index, (title, section_content) in enumerate(sections, start=1):
        try:
            print(f"Section extraction {index}/{total_sections}: {title}", flush=True)
            section_output = extract_concepts_for_section(title, section_content, index, total_sections)
            all_concepts.extend(split_concepts(section_output))
        except Exception as e:
            print(f"Section concept extraction failed ({title}): {e}", flush=True)

    if not all_concepts:
        return extract_concepts(content)

    deduped = dedupe_concepts(all_concepts)
    return "\n\n".join(deduped)


def split_concepts(markdown):
    concepts = re.split(r"\n## ", markdown)
    cleaned = []

    for c in concepts:
        if not c.strip():
            continue
        if not c.startswith("##"):
            c = "## " + c
        first_line = c.split("\n", 1)[0].replace("##", "").strip()
        if len(first_line) < MIN_CONCEPT_NAME_LEN:
            continue
        cleaned.append(c)

    return cleaned

def normalize_concept_name(name):
    # Convert camelCase / PascalCase → spaced words
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)

    # Normalize whitespace
    name = re.sub(r'\s+', ' ', name)

    return name.strip()


def find_similar_concept(concept_name):
    for file in os.listdir(WIKI_DIR):
        if not file.endswith(".md"):
            continue

        existing_name = file.replace(".md", "")

        similarity = SequenceMatcher(None, concept_name.lower(), existing_name.lower()).ratio()

        if similarity > 0.9:
            return existing_name

    return None
# -----------------------------
# MERGE LOGIC
# -----------------------------
def merge_concepts(old, new):
    prompt = f"""You are merging two versions of the same concept.

OLD VERSION:
{old}

NEW VERSION:
{new}

Merge them into ONE clean document.

Rules:
- Keep the best definition (most precise one-sentence form)
- Merge explanations (combine insights, remove redundancy)
- Combine key points (no duplicates, keep all unique points)
- Preserve structure: Definition, Explanation, Key Points, Related Concepts
- Keep it concise but complete
- Do NOT alter the sources: or updated: frontmatter fields — the caller will handle those
- Do NOT change the frontmatter structure or add new frontmatter fields
- Use the title from whichever version has the most readable concept name

Output ONLY markdown.
"""
    return ask_llm(prompt)


# -----------------------------
# FRONTMATTER HELPERS
# -----------------------------
def parse_sources_from_frontmatter(md_content):
    """Extract the sources list from YAML frontmatter. Returns list of strings."""
    match = re.match(r"^---\n(.*?)\n---", md_content, re.DOTALL)
    if not match:
        return []
    fm = match.group(1)
    # Match: sources: ["a", "b"] or sources:\n  - a\n  - b
    # Try inline list first
    inline = re.search(r'^sources:\s*\[([^\]]*)\]', fm, re.MULTILINE)
    if inline:
        raw = inline.group(1)
        return [s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()]
    # Try block list
    block = re.search(r'^sources:\s*\n((?:\s+-\s+.+\n?)+)', fm, re.MULTILINE)
    if block:
        return [re.sub(r'^\s+-\s+', '', line).strip() for line in block.group(1).splitlines() if line.strip()]
    # Try single value
    single = re.search(r'^sources:\s*(.+)$', fm, re.MULTILINE)
    if single:
        val = single.group(1).strip().strip('"').strip("'")
        if val:
            return [val]
    return []


def inject_sources_into_frontmatter(md_content, sources):
    """Replace the sources: field in frontmatter with the given list."""
    if not sources:
        return md_content
    sources_yaml = "sources:\n" + "\n".join(f'  - "{s}"' for s in sources)

    def replace_sources(m):
        fm = m.group(1)
        # Remove existing sources block (inline or block)
        fm = re.sub(r'^sources:\s*\[[^\]]*\]\s*\n?', '', fm, flags=re.MULTILINE)
        fm = re.sub(r'^sources:\s*\n(?:\s+-\s+.+\n?)+', '', fm, flags=re.MULTILINE)
        fm = re.sub(r'^sources:\s*.+\n?', '', fm, flags=re.MULTILINE)
        fm = fm.rstrip('\n') + "\n" + sources_yaml
        return f"---\n{fm}\n---"

    return re.sub(r"^---\n(.*?)\n---", replace_sources, md_content, flags=re.DOTALL, count=1)


# -----------------------------
# LOG + INDEX HELPERS
# -----------------------------
def append_to_log(operation, subject, details=""):
    """Append an entry to wiki/log.md."""
    entry = f"\n## [{TODAY}] {operation} | {subject}\n"
    if details:
        entry += f"{details}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


def parse_tags_from_frontmatter(md_content):
    """Extract tags list from YAML frontmatter. Returns list of strings."""
    match = re.match(r"^---\n(.*?)\n---", md_content, re.DOTALL)
    if not match:
        return []
    fm = match.group(1)
    block = re.search(r'^tags:\s*\n((?:\s+-\s+.+\n?)+)', fm, re.MULTILINE)
    if block:
        return [re.sub(r'^\s+-\s+', '', line).strip() for line in block.group(1).splitlines() if line.strip()]
    inline = re.search(r'^tags:\s*\[([^\]]*)\]', fm, re.MULTILINE)
    if inline:
        return [s.strip().strip('"') for s in inline.group(1).split(",") if s.strip()]
    return []


def extract_one_liner(md_content):
    """Extract first sentence from Definition or Overview section."""
    m = re.search(r"### Definition\n(.+?)(?:\n|$)", md_content)
    if m:
        return m.group(1).strip()
    m = re.search(r"## Overview\n(.+?)(?:\n|$)", md_content)
    if m:
        return m.group(1).strip()
    return ""


def generate_index():
    """Regenerate wiki/index.md as a catalog of all pages, organized by category."""
    SKIP = {"log.md", "index.md", "knowledge_report.md"}
    concept_pages = []
    service_pages = []
    other_pages = []

    for file in sorted(os.listdir(WIKI_DIR)):
        if not file.endswith(".md") or file in SKIP:
            continue
        filepath = os.path.join(WIKI_DIR, file)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        # Extract title from frontmatter
        title = file.replace(".md", "")
        m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"')
                    break

        tags = parse_tags_from_frontmatter(content)
        summary = extract_one_liner(content)
        page_name = file.replace(".md", "")
        entry = (title, page_name, summary)

        if "service" in tags:
            service_pages.append(entry)
        elif "concept" in tags:
            concept_pages.append(entry)
        else:
            other_pages.append(entry)

    total = len(concept_pages) + len(service_pages) + len(other_pages)
    lines = [
        "---",
        'title: "Wiki Index"',
        "tags:",
        "  - index",
        f'updated: "{TODAY}"',
        "---",
        "",
        "# Wiki Index",
        "",
        f"*Auto-generated — {total} pages total.*",
        "",
    ]

    if concept_pages:
        lines += ["## Concepts", ""]
        for _title, page_name, summary in concept_pages:
            suffix = f" — {summary}" if summary else ""
            lines.append(f"- [[{page_name}]]{suffix}")
        lines.append("")

    if service_pages:
        lines += ["## Service / Topic Pages", ""]
        for _title, page_name, summary in service_pages:
            suffix = f" — {summary}" if summary else ""
            lines.append(f"- [[{page_name}]]{suffix}")
        lines.append("")

    if other_pages:
        lines += ["## Other", ""]
        for _title, page_name, summary in other_pages:
            suffix = f" — {summary}" if summary else ""
            lines.append(f"- [[{page_name}]]{suffix}")
        lines.append("")

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(
        f"Index generated: {len(concept_pages)} concepts, {len(service_pages)} service pages, {len(other_pages)} other",
        flush=True,
    )


# -----------------------------
# ALIAS HELPERS
# -----------------------------
def load_aliases():
    """Load aliases.json → dict {canonical_name: [alias, ...]}."""
    if not os.path.exists(ALIASES_FILE):
        return {}
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_aliases(aliases):
    with open(ALIASES_FILE, "w", encoding="utf-8") as f:
        json.dump(aliases, f, indent=2, ensure_ascii=False, sort_keys=True)


def generate_aliases(concept_name):
    """Ask the LLM for common variants of a concept name. Returns list of strings."""
    prompt = f"""Given this concept name from a Pega / enterprise software knowledge base:

"{concept_name}"

List all common textual variants that a reader might write when referring to this concept.
Include: abbreviations, acronyms, hyphenated forms, plurals, alternate casing, and common synonyms.
Do NOT include the original name itself.
Do NOT include variants that are too generic (e.g. "system", "service", "component").
Return ONLY a JSON array of strings, no explanation.

Example output: ["IGW", "integration-gateway", "Integration Gateways", "the gateway"]

Output:"""
    try:
        raw = ask_llm(prompt).strip()
        # Extract JSON array from response
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            candidates = json.loads(match.group(0))
            # Filter: non-empty strings, not same as canonical (case-insensitive)
            return [
                v for v in candidates
                if isinstance(v, str) and v.strip()
                and v.strip().lower() != concept_name.lower()
                and len(v.strip()) >= 2
            ]
    except Exception as e:
        print(f"Alias generation failed for '{concept_name}': {e}", flush=True)
    return []


def save_concept(concept_md, source_file):
    first_line = concept_md.split("\n")[0]

    concept_name = normalize_concept_name(
        clean_title(first_line.replace("##", "").strip())
    )
    similar = find_similar_concept(concept_name)
    if similar and similar.lower() != concept_name.lower():
        print(f"Found similar concept: '{concept_name}' ~ '{similar}'", flush=True)
        concept_name = similar

    safe_name = concept_name.replace("/", "-").strip()
    filename = safe_name + ".md"
    filepath = os.path.join(WIKI_DIR, filename)

    # Normalise the heading
    lines = concept_md.split("\n")
    lines[0] = f"## {concept_name}"
    new_body = "\n".join(lines)

    # Build frontmatter
    new_source = processed_to_raw_path(source_file)
    concept_tags = extract_content_tags(concept_name, concept_md)
    # Always put 'concept' as first tag
    if "concept" not in concept_tags:
        concept_tags = ["concept"] + concept_tags[:MAX_TAGS_PER_CONCEPT - 1]
    yaml_title = json.dumps(concept_name, ensure_ascii=False)
    yaml_tags = "\n".join([f"  - {tag}" for tag in concept_tags])
    sources_yaml = f'  - "{new_source}"'

    frontmatter = f"""---
title: {yaml_title}
tags:
{yaml_tags}
sources:
{sources_yaml}
updated: "{TODAY}"
---"""

    new_md = frontmatter + "\n" + new_body

    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                old_md = f.read()

            # Collect existing sources before merging (LLM may drop them)
            old_sources = parse_sources_from_frontmatter(old_md)

            merged = merge_concepts(old_md, new_md)

            # Python-level source list union (LLM must not lose old sources)
            all_sources = old_sources.copy()
            if new_source not in all_sources:
                all_sources.append(new_source)
            merged = inject_sources_into_frontmatter(merged, all_sources)

            final_md = merged
        except Exception as e:
            print(f"Merge failed for {concept_name}: {e}", flush=True)
            final_md = new_md
    else:
        final_md = new_md

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(final_md)

    # Generate and cache aliases for this concept
    aliases = load_aliases()
    if concept_name not in aliases:
        variants = generate_aliases(concept_name)
        if variants:
            aliases[concept_name] = variants
            save_aliases(aliases)
            print(f"Aliases cached for '{concept_name}': {variants}", flush=True)


# -----------------------------
# SERVICE PAGE GENERATION
# -----------------------------
def generate_service_page(content, source_file):
    prompt = f"""You are writing a service/topic summary page for a knowledge base.

Given the processed document below, write a wiki summary page.

Output format EXACTLY:

---
title: "<service or topic name, human-readable>"
tags:
  - service
sources:
  - "processed/{source_file}"
updated: "{TODAY}"
---

## Overview
3-5 sentences: purpose of this service/topic, where it sits in the system, and why it exists.

## Key Responsibilities
Bullet list of what this service owns or does (be specific, no vague bullet points).

## API / Interface
If present: table or list of main endpoints or operations. Omit if not relevant.

## Data Model
If present: key entities, collections, or data structures. Omit if not relevant.

## Infrastructure
If present: deployment details, runtime dependencies, cloud services. Omit if not relevant.

## Related Concepts
- [[Concept Name]]   (link to concepts that are central to understanding this service)

Rules:
- Do NOT invent information not in the source
- Omit any section that has no relevant content
- Keep it factual and concise
- Related Concepts must be real concept names from the knowledge base (use exact names)
- Output ONLY the markdown

Document:
{content[:8000]}
"""
    return ask_llm(prompt)


def source_to_wiki_name(source_file):
    """Convert processed filename to a kebab-case wiki page name."""
    name = source_file.replace(".md", "").replace("_", "-").replace(" ", "-")
    name = re.sub(r"-+", "-", name).lower().strip("-")
    return name


def save_service_page(page_md, source_file):
    wiki_name = source_to_wiki_name(source_file)
    filename = wiki_name + ".md"
    filepath = os.path.join(WIKI_DIR, filename)

    # Never overwrite an existing service page on the same hash — callers gate this
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(page_md)

    print(f"Service page saved: {filename}", flush=True)





def process_file(filepath, state):
    filename = os.path.basename(filepath)
    current_hash = file_hash(filepath)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Read failed: {filepath} ({e})", flush=True)
        return

    # --- Concept extraction (hash-gated) ---
    if filename not in state["concepts"] or state["concepts"][filename] != current_hash:
        print(f"Extracting concepts: {filename}", flush=True)
        try:
            output = extract_concepts_section_aware(content)
            concepts = split_concepts(output)
        except Exception as e:
            print(f"Concept extraction failed: {filepath} ({e})", flush=True)
            concepts = []

        for c in concepts:
            try:
                save_concept(c, filename)
            except Exception as e:
                print(f"Save concept failed: {filename} ({e})", flush=True)

        state["concepts"][filename] = current_hash
    else:
        print(f"Concepts up-to-date, skipping: {filename}", flush=True)

    # --- Service page generation (hash-gated) ---
    if GENERATE_SERVICE_PAGES:
        if filename not in state["service_pages"] or state["service_pages"][filename] != current_hash:
            print(f"Generating service page: {filename}", flush=True)
            try:
                page_md = generate_service_page(content, filename)
                save_service_page(page_md, filename)
                state["service_pages"][filename] = current_hash
            except Exception as e:
                print(f"Service page generation failed: {filename} ({e})", flush=True)
        else:
            print(f"Service page up-to-date, skipping: {filename}", flush=True)



def run_post_step(script_name, step_name):
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)

    if not os.path.exists(script_path):
        print(f"Skipping {step_name}: missing script {script_name}", flush=True)
        return

    try:
        print(f"Running {step_name}...", flush=True)
        subprocess.run(
            [sys.executable, script_path],
            check=True,
            timeout=POST_STEP_TIMEOUT_SECONDS
        )
        print(f"{step_name} completed", flush=True)
    except Exception as e:
        print(f"{step_name} failed: {e}", flush=True)


# -----------------------------
# MAIN (HASH-BASED)
# -----------------------------
def main():
    state = load_state()

    for file in os.listdir(PROCESSED_DIR):
        path = os.path.join(PROCESSED_DIR, file)

        if not os.path.isfile(path):
            continue
        if file.startswith("."):
            continue
        if not file.endswith(".md"):
            continue

        print(f"Checking: {file}", flush=True)
        process_file(path, state)

    print("Saving state...", flush=True)
    save_state(state)

    # Stage 1: link known concepts across wiki pages
    if RUN_AUTO_LINKER:
        run_post_step("auto_linker.py", "auto-linking")

    # Stage 2: resolve ghost links into grounded concepts and cleanup weak ones
    if RUN_GHOST_RESOLVER:
        run_post_step("resolve_ghost_concepts.py", "ghost concept resolution")

    # Stage 3: re-link after ghost resolution so new concept pages are wired in
    if RUN_AUTO_LINKER and RUN_GHOST_RESOLVER:
        run_post_step("auto_linker.py", "auto-linking (post-ghost-resolution)")

    # Stage 4: optional quality audit
    if RUN_KNOWLEDGE_LINTER:
        run_post_step("knowledge_linter.py", "knowledge linting")

    # Generate index and write log entry
    generate_index()
    page_count = len([f for f in os.listdir(WIKI_DIR) if f.endswith(".md") and f not in {"log.md", "index.md", "knowledge_report.md"}])
    append_to_log("wiki-gen", "wiki_generator.py run complete", f"Pages in wiki: {page_count}")


if __name__ == "__main__":
    main()