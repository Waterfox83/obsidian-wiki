import os
import re
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_DIR = os.path.join(BASE_DIR, "wiki")
ALIASES_FILE = os.path.join(WIKI_DIR, "aliases.json")

# Files that should never be treated as linkable concepts or be linked into
EXCLUDED_FILES = {"knowledge_report.md", "log.md", "index.md"}


def load_concepts():
    concepts = set()

    for file in os.listdir(WIKI_DIR):
        if file.endswith(".md") and file not in EXCLUDED_FILES:
            name = file.replace(".md", "")
            concepts.add(name)

    # longest first → avoids partial matches
    return sorted(concepts, key=len, reverse=True)


def load_alias_patterns():
    """Return list of (alias, canonical_name) sorted longest-alias-first."""
    if not os.path.exists(ALIASES_FILE):
        return []
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as f:
            aliases = json.load(f)
    except Exception:
        return []

    pairs = []
    for canonical, variants in aliases.items():
        for variant in variants:
            if variant and variant.strip():
                pairs.append((variant.strip(), canonical))

    # longest alias first → avoid partial matches
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def split_frontmatter(content):
    """Return (frontmatter_block, body). frontmatter_block includes the --- delimiters."""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            return content[:end + 4], content[end + 4:]
    return "", content


def auto_link_text(text, concepts, alias_patterns=None):
    # Protect existing links
    protected = {}

    def protect(match):
        key = f"__LINK_{len(protected)}__"
        protected[key] = match.group(0)
        return key

    # Step 1: protect existing [[...]]
    text = re.sub(r"\[\[.*?\]\]", protect, text)

    # Step 2: replace alias variants → [[canonical]] (longest alias first)
    if alias_patterns:
        for alias, canonical in alias_patterns:
            pattern = r'\b' + re.escape(alias) + r'\b'
            text = re.sub(pattern, f"[[{canonical}]]", text)

    # Step 3: replace canonical concept names (longest first)
    for concept in concepts:
        pattern = r'\b' + re.escape(concept) + r'\b'
        text = re.sub(pattern, f"[[{concept}]]", text)

    # Step 4: restore protected links
    for key, value in protected.items():
        text = text.replace(key, value)

    return text


def auto_link_file(filepath, concepts, alias_patterns=None):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    frontmatter, body = split_frontmatter(content)
    linked_body = auto_link_text(body, concepts, alias_patterns)
    updated = frontmatter + linked_body

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(updated)


def main():
    concepts = load_concepts()
    alias_patterns = load_alias_patterns()
    print(f"Loaded {len(concepts)} concepts, {len(alias_patterns)} alias patterns", flush=True)

    for file in os.listdir(WIKI_DIR):
        if not file.endswith(".md"):
            continue
        if file in EXCLUDED_FILES:
            print(f"Skipping excluded file: {file}", flush=True)
            continue

        path = os.path.join(WIKI_DIR, file)
        print(f"Linking: {file}", flush=True)
        auto_link_file(path, concepts, alias_patterns)


if __name__ == "__main__":
    main()