import os
import json
import sys
import urllib.request
import re
from datetime import date

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_DIR = os.path.join(BASE_DIR, "wiki")
LOG_FILE = os.path.join(WIKI_DIR, "log.md")

LINTER_MODEL = "gemma4:26b"
LINTER_CHUNK_CHARS = int(os.getenv("WIKI_LINTER_CHUNK_CHARS", "10000"))
EXCLUDED_FILES = {"knowledge_report.md", "log.md", "index.md"}


def ask_llm(prompt):
    payload = json.dumps({
        "model": LINTER_MODEL,
        "prompt": prompt,
        "stream": False
    }).encode()

    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["response"]


def load_wiki():
    data = {}

    for file in sorted(os.listdir(WIKI_DIR)):
        if file.endswith(".md") and file not in EXCLUDED_FILES:
            path = os.path.join(WIKI_DIR, file)
            with open(path, "r", encoding="utf-8") as f:
                data[file] = f.read()

    return data


def chunk_wiki(wiki_data, chunk_size=LINTER_CHUNK_CHARS):
    """Split wiki content into chunks for analysis, keeping file boundaries intact."""
    chunks = []
    current_chunk = []
    current_len = 0

    for filename, content in wiki_data.items():
        entry = f"=== {filename} ===\n{content}\n"
        entry_len = len(entry)

        if current_len + entry_len > chunk_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [entry]
            current_len = entry_len
        else:
            current_chunk.append(entry)
            current_len += entry_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def lint_chunk(chunk_text, chunk_index, total_chunks):
    prompt = f"""You are analyzing part {chunk_index} of {total_chunks} of a knowledge base.

Tasks — for this chunk only:
1. Missing concepts: concepts mentioned in text or Related Concepts but with no wiki page
2. Weak concepts: pages that are too short, vague, or lack a proper Definition/Explanation/Key Points structure
3. Duplicate concepts: pages that appear to cover the same idea under slightly different names
4. Suggested new concepts: important ideas clearly present in the text but not captured as a concept page
5. Broken links: concept names referenced in "Related Concepts" sections that have no corresponding wiki page file
6. Disconnected service pages: pages that appear to be service/topic summaries but have no links to any concept pages

Return ONLY in this format (use "None found" if a section is empty):

## Missing Concepts
- ...

## Weak Concepts
- ...

## Duplicate Concepts
- ...

## New Concept Suggestions
- ...

## Broken Links
- ...

## Disconnected Service Pages
- ...

Knowledge Base (chunk {chunk_index}/{total_chunks}):
{chunk_text}
"""
    return ask_llm(prompt)


def consolidate_findings(chunk_reports):
    if len(chunk_reports) == 1:
        return chunk_reports[0]

    combined = "\n\n---\n\n".join(
        f"### Chunk {i+1} findings\n{r}" for i, r in enumerate(chunk_reports)
    )

    prompt = f"""You have linting reports from {len(chunk_reports)} chunks of a knowledge base.
Consolidate them into one clean, deduplicated report.

Merge items that refer to the same concept across chunks.
Remove exact duplicates.

Output format:

## Missing Concepts
- ...

## Weak Concepts
- ...

## Duplicate Concepts
- ...

## New Concept Suggestions
- ...

## Broken Links
- ...

## Disconnected Service Pages
- ...

Chunk reports:
{combined[:12000]}
"""
    return ask_llm(prompt)


def save_report(report):
    path = os.path.join(WIKI_DIR, "knowledge_report.md")

    with open(path, "w", encoding="utf-8") as f:
        f.write("# Knowledge Base Lint Report\n\n")
        f.write(report)


# -----------------------------
# FIX HELPERS
# -----------------------------
STUB_TEMPLATE = """\
---
title: "{title}"
tags:
  - concept
sources: []
updated: "{today}"
---

## {title}

### Definition
{definition}

### Explanation
{explanation}

### Key Points
{key_points}

### Related Concepts
"""

TODAY = date.today().isoformat()


def parse_report_section(report_text, section_name):
    """Extract bullet items from a named ## section in the report."""
    pattern = rf"## {re.escape(section_name)}\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, report_text, re.DOTALL)
    if not m:
        return []
    block = m.group(1).strip()
    items = []
    for line in block.splitlines():
        line = line.strip().lstrip("-").strip()
        if line and line.lower() != "none found":
            items.append(line)
    return items


def patch_weak_concept(filepath, concept_name):
    """Ask the LLM to improve a weak concept page in-place."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    prompt = f"""The following wiki concept page is weak or incomplete. It may be missing
a proper Definition, Explanation, or Key Points section.

Improve it: fill in any missing sections, expand thin content, and ensure all four
sections (Definition, Explanation, Key Points, Related Concepts) are present and useful.
Keep the YAML frontmatter exactly as-is (do NOT change title, tags, sources, updated).
Output ONLY the complete improved markdown page.

Current page:
{content}
"""
    try:
        improved = ask_llm(prompt).strip()
        if improved:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(improved)
            return True
    except Exception as e:
        print(f"Patch failed for '{concept_name}': {e}", flush=True)
    return False


def create_stub_concept(concept_name):
    """Ask the LLM to generate a stub page for a missing or suggested concept."""
    safe_name = concept_name.replace("/", "-").strip()
    filepath = os.path.join(WIKI_DIR, safe_name + ".md")

    if os.path.exists(filepath):
        print(f"Stub skipped (already exists): {concept_name}", flush=True)
        return False

    prompt = f"""Write a stub wiki page for this concept from a Pega/enterprise-software knowledge base:

"{concept_name}"

Format EXACTLY:

---
title: "{concept_name}"
tags:
  - concept
sources: []
updated: "{TODAY}"
---

## {concept_name}

### Definition
One sentence: what this concept IS.

### Explanation
2-4 sentences: how and why it matters architecturally.

### Key Points
- (key point 1)
- (key point 2)
- (key point 3)

### Related Concepts
- [[related concept]]

Rules:
- Base content on the concept name alone; do not invent specific facts
- Keep it honest: use hedged language ("typically", "generally") where uncertain
- Output ONLY the markdown
"""
    try:
        stub_md = ask_llm(prompt).strip()
        if stub_md:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(stub_md)
            return True
    except Exception as e:
        print(f"Stub creation failed for '{concept_name}': {e}", flush=True)
    return False


def apply_fixes(report_text):
    """Apply all actionable fixes from the lint report. Returns fix summary dict."""
    summary = {"weak_patched": [], "stubs_created": [], "skipped": []}

    # --- Fix weak concepts ---
    weak_items = parse_report_section(report_text, "Weak Concepts")
    print(f"Weak concepts to patch: {len(weak_items)}", flush=True)
    for item in weak_items:
        # item may be "Concept Name - reason" or just "Concept Name"
        concept_name = item.split(" - ")[0].strip().strip("`").strip('"')
        safe_name = concept_name.replace("/", "-").strip()
        filepath = os.path.join(WIKI_DIR, safe_name + ".md")
        if not os.path.exists(filepath):
            print(f"Weak concept file not found, will stub: {concept_name}", flush=True)
            if create_stub_concept(concept_name):
                summary["stubs_created"].append(concept_name)
            continue
        print(f"Patching weak concept: {concept_name}", flush=True)
        if patch_weak_concept(filepath, concept_name):
            summary["weak_patched"].append(concept_name)
        else:
            summary["skipped"].append(concept_name)

    # --- Create stubs for missing concepts and new suggestions ---
    for section in ("Missing Concepts", "New Concept Suggestions"):
        items = parse_report_section(report_text, section)
        print(f"{section} to stub: {len(items)}", flush=True)
        for item in items:
            concept_name = item.split(" - ")[0].strip().strip("`").strip('"')
            if not concept_name:
                continue
            print(f"Creating stub: {concept_name}", flush=True)
            if create_stub_concept(concept_name):
                summary["stubs_created"].append(concept_name)
            else:
                summary["skipped"].append(concept_name)

    return summary


def main():
    fix_mode = "--fix" in sys.argv
    wiki_data = load_wiki()

    if not wiki_data:
        print("No wiki files found.", flush=True)
        return

    # --fix without a fresh lint: read existing report and apply fixes
    if fix_mode:
        report_path = os.path.join(WIKI_DIR, "knowledge_report.md")
        if not os.path.exists(report_path):
            print("No knowledge_report.md found. Run without --fix first to generate one.", flush=True)
            return
        with open(report_path, "r", encoding="utf-8") as f:
            report_text = f.read()
        print("Applying fixes from knowledge_report.md...", flush=True)
        summary = apply_fixes(report_text)
        today = date.today().isoformat()
        with open(LOG_FILE, "a", encoding="utf-8") as lf:
            lf.write(f"\n## [{today}] lint-fix | Applied fixes from report\n")
            lf.write(f"Weak concepts patched: {len(summary['weak_patched'])}\n")
            lf.write(f"Stubs created: {len(summary['stubs_created'])}\n")
            lf.write(f"Skipped: {len(summary['skipped'])}\n")
        print(
            f"Fix pass complete ✅ — patched: {len(summary['weak_patched'])}, "
            f"stubs: {len(summary['stubs_created'])}, skipped: {len(summary['skipped'])}",
            flush=True,
        )
        return

    chunks = chunk_wiki(wiki_data)
    print(f"Linting {len(wiki_data)} pages in {len(chunks)} chunk(s)...", flush=True)

    chunk_reports = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"Linting chunk {i}/{len(chunks)}...", flush=True)
        try:
            report = lint_chunk(chunk, i, len(chunks))
            chunk_reports.append(report)
        except Exception as e:
            print(f"Chunk {i} linting failed: {e}", flush=True)

    if not chunk_reports:
        print("All chunks failed — no report generated.", flush=True)
        return

    final_report = consolidate_findings(chunk_reports)
    save_report(final_report)

    # Append log entry
    today = date.today().isoformat()
    with open(LOG_FILE, "a", encoding="utf-8") as lf:
        lf.write(f"\n## [{today}] lint | Health check\n")
        lf.write(f"Pages linted: {len(wiki_data)}\n")

    print("Knowledge report generated ✅", flush=True)


if __name__ == "__main__":
    main()