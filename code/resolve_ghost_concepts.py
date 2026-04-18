import json
import os
import re
import urllib.request
import logging
from collections import Counter, defaultdict
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_DIR = os.path.join(BASE_DIR, "wiki")

MODEL = os.getenv("WIKI_GHOST_MODEL", "gemma4:26b")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("WIKI_GHOST_TIMEOUT_SECONDS", "300"))
MIN_GHOST_REFERENCES = int(os.getenv("WIKI_GHOST_MIN_REFERENCES", "2"))
MAX_CANDIDATES = int(os.getenv("WIKI_GHOST_MAX_CANDIDATES", "120"))
MAX_KNOWLEDGE_CHARS = int(os.getenv("WIKI_GHOST_MAX_KNOWLEDGE_CHARS", "50000"))
SIMILARITY_THRESHOLD = float(os.getenv("WIKI_GHOST_SIMILARITY", "0.9"))
MAX_SOURCE_FILES = int(os.getenv("WIKI_GHOST_MAX_SOURCE_FILES", "12"))
LOG_FILE = os.path.join(WIKI_DIR, "ghost_resolution_report.json")
LOG_PATH = os.path.join(WIKI_DIR, "ghost_resolution.log")
LOG_LEVEL = os.getenv("WIKI_GHOST_LOG_LEVEL", "INFO").upper()

GENERIC_SKIP = {
    "system", "process", "service", "services", "action", "data", "details",
    "api", "apis", "module", "components", "component", "model", "models",
    "workflow", "flow", "overview", "notes", "summary", "design", "architecture"
}


logger = logging.getLogger("ghost_resolver")


def setup_logging():
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.handlers = []

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def ask_llm(prompt):
    logger.debug("LLM request: prompt chars=%s", len(prompt))
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())["response"]


def normalize_concept_name(name):
    name = re.sub(r"\[+", "", name)
    name = re.sub(r"\]+", "", name)
    name = name.split("|", 1)[0]
    name = name.split("#", 1)[0]
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^[^A-Za-z0-9]+", "", name)
    name = re.sub(r"[^A-Za-z0-9\s\-/()]+", "", name)
    return name.strip()


def concept_key(name):
    return normalize_concept_name(name).lower()


def is_generic(name):
    key = concept_key(name)
    if not key:
        return True
    if key in GENERIC_SKIP:
        return True
    if len(key) < 3:
        return True
    if key.startswith("http") or key.startswith("www"):
        return True
    if key.startswith("@"):
        return True
    return False


def list_wiki_files():
    return sorted([f for f in os.listdir(WIKI_DIR) if f.endswith(".md")])


def load_wiki_contents(files):
    data = {}
    for file in files:
        path = os.path.join(WIKI_DIR, file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data[file] = f.read()
        except Exception:
            continue
    return data


def extract_links(content):
    matches = re.findall(r"\[\[(.*?)\]\]", content)
    out = []
    for m in matches:
        name = normalize_concept_name(m)
        if name:
            out.append(name)
    return out


def build_existing_concepts(files):
    existing = {}
    for file in files:
        concept = normalize_concept_name(file.replace(".md", ""))
        if concept:
            existing[concept.lower()] = concept
    return existing


def find_similar_existing(name, existing_names):
    low = name.lower()
    best = None
    best_score = 0.0
    for existing in existing_names:
        score = SequenceMatcher(None, low, existing.lower()).ratio()
        if score > best_score:
            best_score = score
            best = existing
    if best and best_score >= SIMILARITY_THRESHOLD:
        return best
    return None


def merge_concepts(old_text, new_text):
    prompt = f"""
You are merging two versions of the same wiki concept.

OLD VERSION:
{old_text}

NEW VERSION:
{new_text}

Merge into ONE clean markdown entry.

Rules:
- Preserve factual details from both
- Remove duplicates and contradictions
- Keep structure exactly:
## <Concept Name>
### Definition
### Explanation
### Key Points
### Related Concepts
- Use Obsidian links in related concepts
- Output ONLY markdown
"""
    return ask_llm(prompt)


def build_prompt(concept_name, knowledge_text):
    return f"""
You are generating a knowledge base entry.

Concept: {concept_name}

Use ONLY the knowledge provided below.
If the concept is not sufficiently described in the knowledge,
return EXACTLY: INSUFFICIENT DATA

Knowledge:
{knowledge_text}

Output format:

## {concept_name}

### Definition
...

### Explanation
...

### Key Points
- ...

### Related Concepts
- [[...]]
"""


def valid_generated_concept(text):
    stripped = text.strip()
    if stripped == "INSUFFICIENT DATA":
        return False
    required = ["## ", "### Definition", "### Explanation", "### Key Points", "### Related Concepts"]
    if not all(token in stripped for token in required):
        return False
    bullets = re.findall(r"^\s*-\s+.+", stripped, flags=re.MULTILINE)
    return len(bullets) >= 3


def heading_name(markdown):
    first = markdown.split("\n", 1)[0].strip()
    return normalize_concept_name(first.replace("##", "").strip())


def concept_to_filename(name):
    safe = name.replace("/", "-").replace("\\", "-")
    safe = re.sub(r"[:*?\"<>|]", "-", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    return f"{safe}.md"


def delete_concept_file_if_exists(concept_name):
    filepath = os.path.join(WIKI_DIR, concept_to_filename(concept_name))
    if not os.path.exists(filepath):
        return False

    try:
        os.remove(filepath)
        logger.info("Deleted skipped concept file: %s", filepath)
        return True
    except Exception as e:
        logger.warning("Failed deleting skipped concept file '%s': %s", filepath, e)
        return False


def sanitize_links_to_known(markdown, existing_map, extra_allowed=None):
    extra_allowed = extra_allowed or []

    allowed = {}
    for canonical in existing_map.values():
        allowed[normalize_concept_name(canonical).lower()] = canonical

    for name in extra_allowed:
        normalized = normalize_concept_name(name)
        if normalized:
            allowed[normalized.lower()] = normalized

    def _replace(match):
        raw = match.group(1)
        target = normalize_concept_name(raw)
        if not target:
            return ""
        canonical = allowed.get(target.lower())
        if canonical:
            return f"[[{canonical}]]"
        return target

    return re.sub(r"\[\[(.*?)\]\]", _replace, markdown)


def gather_knowledge_for_concept(concept_name, wiki_data, file_hits):
    chosen_files = sorted(file_hits.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SOURCE_FILES]

    chunks = []
    total = 0
    for file, _ in chosen_files:
        text = wiki_data.get(file, "")
        if not text:
            continue
        block = f"\n\n# SOURCE: {file}\n{text}"
        if total + len(block) > MAX_KNOWLEDGE_CHARS:
            remain = MAX_KNOWLEDGE_CHARS - total
            if remain <= 0:
                break
            block = block[:remain]
        chunks.append(block)
        total += len(block)
        if total >= MAX_KNOWLEDGE_CHARS:
            break

    return "".join(chunks)


def enforce_heading(markdown, target_name):
    lines = markdown.strip().split("\n")
    if not lines:
        return f"## {target_name}\n"

    if lines[0].strip().startswith("## "):
        lines[0] = f"## {target_name}"
    else:
        lines.insert(0, f"## {target_name}")

    return "\n".join(lines).strip() + "\n"


def save_or_merge_concept(concept_markdown, target_name, existing_map, link_counts):
    normalized_target = normalize_concept_name(target_name)
    if not normalized_target:
        return "skipped_invalid", None

    if link_counts.get(normalized_target.lower(), 0) == 0:
        return "skipped_unlinked_target", normalized_target

    concept_markdown = enforce_heading(concept_markdown, normalized_target)
    concept_markdown = sanitize_links_to_known(
        concept_markdown,
        existing_map,
        extra_allowed=[normalized_target],
    )

    similar = find_similar_existing(normalized_target, list(existing_map.values()))

    if similar:
        filepath = os.path.join(WIKI_DIR, concept_to_filename(similar))
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                old_text = f.read()
            merged = merge_concepts(old_text, concept_markdown)
            merged = enforce_heading(merged, similar)
            merged = sanitize_links_to_known(merged, existing_map, extra_allowed=[similar])
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(merged)
            return "merged", similar
        except Exception:
            return "skipped_merge_error", similar

    filepath = os.path.join(WIKI_DIR, concept_to_filename(normalized_target))
    if os.path.exists(filepath):
        return "skipped_exists", normalized_target

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(concept_markdown.strip() + "\n")

    existing_map[normalized_target.lower()] = normalized_target
    return "created", normalized_target


def main():
    setup_logging()
    logger.info("Ghost resolver started (model=%s, min_refs=%s, max_candidates=%s)", MODEL, MIN_GHOST_REFERENCES, MAX_CANDIDATES)

    files = list_wiki_files()
    wiki_data = load_wiki_contents(files)
    existing = build_existing_concepts(files)
    logger.info("Loaded wiki files=%s existing_concepts=%s", len(files), len(existing))

    link_counts = Counter()
    refs_by_file = defaultdict(lambda: defaultdict(int))

    for file, content in wiki_data.items():
        links = extract_links(content)
        for link in links:
            key = link.lower()
            link_counts[key] += 1
            refs_by_file[key][file] += 1

    ghosts = [
        (concept, count)
        for concept, count in link_counts.items()
        if concept not in existing
    ]

    ghosts.sort(key=lambda item: item[1], reverse=True)
    logger.info("Ghost candidates discovered=%s", len(ghosts))

    report = {
        "total_files": len(files),
        "total_ghost_candidates": len(ghosts),
        "processed_candidates": 0,
        "created": [],
        "merged": [],
        "deleted": [],
        "skipped": [],
    }

    candidates = []
    for concept, count in ghosts:
        normalized = normalize_concept_name(concept)
        if not normalized:
            report["skipped"].append({"concept": concept, "reason": "empty_after_normalize"})
            continue
        if is_generic(normalized):
            report["skipped"].append({"concept": normalized, "reason": "generic_or_invalid"})
            continue
        if count < MIN_GHOST_REFERENCES:
            report["skipped"].append({"concept": normalized, "reason": "low_reference_count", "references": count})
            continue
        candidates.append((normalized, count))

    if MAX_CANDIDATES > 0:
        candidates = candidates[:MAX_CANDIDATES]

    logger.info("Candidates after filtering=%s", len(candidates))

    total = len(candidates)
    for index, (concept_name, count) in enumerate(candidates, start=1):
        report["processed_candidates"] += 1
        logger.info("[%s/%s] Processing ghost concept='%s' refs=%s", index, total, concept_name, count)

        knowledge = gather_knowledge_for_concept(
            concept_name,
            wiki_data,
            refs_by_file[concept_name.lower()]
        )
        logger.debug("Knowledge chars for '%s' = %s", concept_name, len(knowledge))

        if not knowledge.strip():
            report["skipped"].append({"concept": concept_name, "reason": "no_knowledge"})
            logger.info("Skipped '%s' reason=no_knowledge", concept_name)
            if delete_concept_file_if_exists(concept_name):
                report["deleted"].append({"concept": concept_name, "reason": "no_knowledge"})
            continue

        prompt = build_prompt(concept_name, knowledge)

        try:
            generated = ask_llm(prompt)
        except Exception as e:
            report["skipped"].append({"concept": concept_name, "reason": f"llm_error:{e}"})
            logger.warning("Skipped '%s' reason=llm_error error=%s", concept_name, e)
            continue

        if generated.strip() == "INSUFFICIENT DATA":
            report["skipped"].append({"concept": concept_name, "reason": "insufficient_data"})
            logger.info("Skipped '%s' reason=insufficient_data", concept_name)
            if delete_concept_file_if_exists(concept_name):
                report["deleted"].append({"concept": concept_name, "reason": "insufficient_data"})
            continue

        if not valid_generated_concept(generated):
            report["skipped"].append({"concept": concept_name, "reason": "invalid_generated_format"})
            logger.info("Skipped '%s' reason=invalid_generated_format", concept_name)
            if delete_concept_file_if_exists(concept_name):
                report["deleted"].append({"concept": concept_name, "reason": "invalid_generated_format"})
            continue

        status, final_name = save_or_merge_concept(generated, concept_name, existing, link_counts)
        if status == "created":
            report["created"].append({"concept": final_name, "references": count})
            logger.info("Created concept file: %s", final_name)
        elif status == "merged":
            report["merged"].append({"concept": final_name, "references": count})
            logger.info("Merged into existing concept: %s", final_name)
        else:
            report["skipped"].append({"concept": concept_name, "reason": status})
            logger.info("Skipped '%s' reason=%s", concept_name, status)

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"Ghost candidates: {report['total_ghost_candidates']}")
    print(f"Processed candidates: {report['processed_candidates']}")
    print(f"Created: {len(report['created'])}")
    print(f"Merged: {len(report['merged'])}")
    print(f"Deleted: {len(report['deleted'])}")
    print(f"Skipped: {len(report['skipped'])}")
    print(f"Report: {LOG_FILE}")
    print(f"Log: {LOG_PATH}")

    logger.info(
        "Ghost resolver completed: candidates=%s created=%s merged=%s deleted=%s skipped=%s",
        report["processed_candidates"],
        len(report["created"]),
        len(report["merged"]),
        len(report["deleted"]),
        len(report["skipped"]),
    )


if __name__ == "__main__":
    main()
