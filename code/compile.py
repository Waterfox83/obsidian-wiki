import os
import json
import urllib.request
import requests
import re
import mimetypes
from datetime import date
from urllib.parse import urlparse
import browser_cookie3
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE_DIR, "raw")
PROCESSED_DIR = os.path.join(BASE_DIR, "processed")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-26b-a4b")

os.makedirs(PROCESSED_DIR, exist_ok=True)


# -----------------------------
# FRONTMATTER EXTRACTION
# -----------------------------
def extract_from_frontmatter(content, key="source"):
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return None

    frontmatter = match.group(1)

    for line in frontmatter.split("\n"):
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()

    return None


# -----------------------------
# PLAYWRIGHT IMAGE DOWNLOAD
# -----------------------------
def download_images_playwright(page_url, output_dir):
    images = []

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir="playwright-profile",
            headless=True
        )

        page = browser.new_page()
        page.goto(page_url, timeout=60000)

        img_elements = page.query_selector_all("img")

        for i, img in enumerate(img_elements):
            src = img.get_attribute("src")

            if not src or "icon" in src or "logo" in src:
                continue

            try:
                response = page.request.get(src)
                if response.ok:
                    filename = f"image_{i}.png"
                    absolute_path = os.path.join(output_dir, filename)
                    relative_path = os.path.join("images", filename)

                    with open(absolute_path, "wb") as f:
                        f.write(response.body())

                    images.append((src, relative_path))
            except Exception as e:
                print(f"Playwright image failed: {src} ({e})", flush=True)

        browser.close()

    return images


# -----------------------------
# REQUESTS IMAGE DOWNLOAD (fallback)
# -----------------------------
def download_images(md_content, output_dir):
    image_urls = re.findall(r'!\[.*?\]\((.*?)\)', md_content)

    try:
        cj = browser_cookie3.edge()
    except Exception as e:
        cj = None
        print(f"Cookie load failed: {e}", flush=True)

    local_paths = []

    for i, url in enumerate(image_urls):
        try:
            parsed_url = urlparse(url)
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/*"
            }

            if cj:
                response = requests.get(url, cookies=cj, headers=headers, timeout=10)
            else:
                response = requests.get(url, headers=headers, timeout=10)

            if response.status_code != 200:
                continue

            content_type = response.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                continue

            ext = mimetypes.guess_extension(content_type.split(";")[0]) or ".png"

            filename = f"image_{i}{ext}"
            absolute_path = os.path.join(output_dir, filename)
            relative_path = os.path.join("images", filename)

            with open(absolute_path, "wb") as f:
                f.write(response.content)

            local_paths.append((url, relative_path))

        except Exception as e:
            print(f"Requests image failed: {url} ({e})", flush=True)

    return local_paths


# -----------------------------
# IMAGE REPLACEMENT
# -----------------------------
def replace_image_links(md_content, mappings):
    for url, path in mappings:
        md_content = md_content.replace(url, path)
    return md_content


# -----------------------------
# IMAGE DESCRIPTION
# -----------------------------
def describe_image(image_path, context):
    prompt = f"""
    You are given an image from a document.

    Context:
    {context[:1000]}

    Image path: {image_path}

    Describe:
    - What it shows
    - Key elements
    - Why it matters

    Keep it concise.
    """
    return ask_llm(prompt)


def generate_image_descriptions(mappings, content):
    results = {}

    for _, path in mappings:
        try:
            results[path] = describe_image(path, content)
        except:
            results[path] = "Description unavailable"

    return results


def inject_image_descriptions(md_content, descriptions):
    for path, desc in descriptions.items():
        md_content += f"\n\n## Image Analysis\n\n![Image]({path})\n\n{desc}\n"
    return md_content


# -----------------------------
# LLM CALL
# -----------------------------
def ask_llm(prompt):
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]


def to_flat_processed_filename(rel_path):
    normalized = rel_path.replace(os.sep, "__")
    return normalized


# -----------------------------
# MAIN PIPELINE
# -----------------------------
for root, dirs, files in os.walk(RAW_DIR):
    dirs[:] = [d for d in dirs if not d.startswith(".")]

    for file in files:
        if file.startswith("."):
            continue

        raw_path = os.path.join(root, file)
        rel_path = os.path.relpath(raw_path, RAW_DIR)
        processed_file = to_flat_processed_filename(rel_path)
        processed_path = os.path.join(PROCESSED_DIR, processed_file)

        print(f"Processing: {rel_path}", flush=True)

        if not os.path.isfile(raw_path):
            continue

        if os.path.exists(processed_path):
            print(f"Skipping already processed file: {rel_path}", flush=True)
            continue

        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"Read failed: {rel_path} ({e})", flush=True)
            continue

        image_dir = os.path.join(PROCESSED_DIR, "images")
        os.makedirs(image_dir, exist_ok=True)

        mappings = []

        # 🔥 Extract URL
        page_url = extract_from_frontmatter(content)

        # 🔥 Try Playwright first
        if page_url:
            try:
                print(f"Using Playwright for: {page_url}", flush=True)
                mappings = download_images_playwright(page_url, image_dir)
            except Exception as e:
                print(f"Playwright failed: {e}", flush=True)

        # # 🔥 Fallback to requests
        # if not mappings:
        #     try:
        #         print("Falling back to requests", flush=True)
        #         mappings = download_images_requests(content, image_dir)
        #     except Exception as e:
        #         print(f"Fallback failed: {e}", flush=True)

        # Replace links
        content = replace_image_links(content, mappings)

        # Describe images
        descriptions = generate_image_descriptions(mappings, content)

        # Inject descriptions
        content = inject_image_descriptions(content, descriptions)

        # LLM processing
        prompt = f"""You are a technical knowledge engineer. Convert this raw document into a clean,
structured Markdown file for a knowledge base.

Output format EXACTLY:

---
title: "<inferred title from document>"
source: "<URL from frontmatter if present, else omit this field>"
updated: "<today's date as YYYY-MM-DD>"
---

## Overview
2-4 sentences: what this document is about and why it matters.

## Key Concepts
Bullet list of the most important ideas (no sub-bullets, no elaboration here).

## Architecture / Design
If present: describe the system structure, pipeline stages, or design patterns. Use a table or bullets.
Omit this section entirely if no architectural content exists.

## API / Interface
If present: table or bullet list of main endpoints, operations, or integration points.
Omit this section entirely if no API content exists.

## Infrastructure
If present: deployment topology, key dependencies, cloud services used.
Omit this section entirely if no infrastructure content exists.

## Notes
Any important caveats, limitations, known issues, or operational gotchas.
Omit this section entirely if nothing notable.

Rules:
- Do NOT invent information not present in the source document
- Omit any section that has no relevant content
- Keep each section factual and concise
- Preserve all technically important details from the source
- Output ONLY the markdown (no preamble or explanation)

Document:
{content[:8000]}
"""

        try:
            output = ask_llm(prompt)
        except Exception as e:
            print(f"LLM failed: {rel_path} ({e})", flush=True)
            continue

        try:
            with open(processed_path, "w", encoding="utf-8") as f:
                f.write(output)
        except Exception as e:
            print(f"Write failed: {rel_path} ({e})", flush=True)
            continue

        print(f"Done: {rel_path}", flush=True)

# Append run entry to wiki/log.md (if wiki dir exists)
_wiki_dir = os.path.join(BASE_DIR, "wiki")
if os.path.isdir(_wiki_dir):
    _today = date.today().isoformat()
    _log_file = os.path.join(_wiki_dir, "log.md")
    with open(_log_file, "a", encoding="utf-8") as _lf:
        _lf.write(f"\n## [{_today}] ingest | compile.py batch run\n")
