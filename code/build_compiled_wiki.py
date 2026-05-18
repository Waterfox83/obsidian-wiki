from __future__ import annotations

import json
import os
import re
import textwrap
from collections import defaultdict
from copy import deepcopy
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "raw"
WIKI = REPO / "llm-wiki"
TODAY = date.today().isoformat()
MANIFEST_PATH = REPO / "code" / "wiki_tool_manifest.json"
HISTORY_PATH = REPO / "code" / "wiki_tool_history.json"


def yq(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def wiki_link(rel_path: str) -> str:
    if rel_path.endswith(".md"):
        rel_path = rel_path[:-3]
    return f"[[llm-wiki/{rel_path}]]"


def bullet_links(paths: list[str]) -> str:
    return "\n".join(f"- {wiki_link(path)}" for path in paths)


def render_frontmatter(title: str, tags: list[str], sources: list[str]) -> str:
    lines = [
        "---",
        f"title: {yq(title)}",
        f"tags: [{', '.join(yq(tag) for tag in tags)}]",
    ]
    if sources:
        lines.append("sources:")
        for source in sources:
            lines.append(f"  - {yq(source)}")
    else:
        lines.append("sources: []")
    lines.append(f"updated: {TODAY}")
    lines.append("---")
    return "\n".join(lines)


def render_page(title: str, tags: list[str], sources: list[str], body: str) -> str:
    body = textwrap.dedent(body).strip()
    return f"{render_frontmatter(title, tags, sources)}\n\n# {title}\n\n{body}\n"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def normalize_stem(stem: str) -> str:
    stem = re.sub(r"\s+\d{1,2}[.]\d{2}[.]\d{2}\s*[AP]M", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+\d{2}-\d{2}-\d+-\d+", "", stem)
    stem = re.sub(r"\s+", " ", stem)
    return stem.strip().lower()


def parse_frontmatter_title(lines: list[str]) -> str | None:
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.lower().startswith("title:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


NOISE_PHRASES = (
    "thinking...",
    "...done thinking.",
    "process this document into structured markdown",
    "edit this section",
    "edit source",
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")


def unwrap_embedded_markdown(text: str) -> str:
    lowered_prefix = text[:500].lower()
    if "thinking..." not in lowered_prefix and "process this document into structured markdown" not in lowered_prefix:
        return text

    matches = re.findall(r"```(?:markdown|md)?\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if not matches:
        return text

    return max(matches, key=len).strip()


def strip_frontmatter_text(text: str) -> str:
    return re.sub(r"^---\n.*?\n---\n*", "", text, flags=re.DOTALL, count=1)


def read_source_markdown(path: Path) -> str:
    return unwrap_embedded_markdown(read_text(path)).strip()


def body_text(path: Path) -> str:
    return strip_frontmatter_text(read_source_markdown(path)).strip()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_title(text: str) -> str:
    return normalize_text(text.replace("[[", "").replace("]]", "").strip("# ").strip('"').strip("'"))


def is_noise_text(text: str) -> bool:
    normalized = normalize_text(text).lower()
    if not normalized:
        return True
    return any(phrase in normalized for phrase in NOISE_PHRASES)


def remove_code_blocks(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def clean_excerpt_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^>\s?", "", line)
        if is_noise_text(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def truncate_text(text: str, limit: int) -> str:
    compact = normalize_text(text)
    if len(compact) <= limit:
        return compact
    return compact[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def extract_focus_headings(path: Path, max_items: int = 6) -> list[str]:
    text = body_text(path)
    headings: list[str] = []
    for raw_line in text.splitlines():
        match = re.match(r"^#{2,6}\s+(.+)$", raw_line.strip())
        if not match:
            continue
        heading = clean_title(match.group(1))
        if not heading or is_noise_text(heading) or heading in headings:
            continue
        headings.append(heading)
        if len(headings) >= max_items:
            break
    return headings


def extract_detail_paragraphs(path: Path, max_paragraphs: int = 2, char_limit: int = 450) -> list[str]:
    text = clean_excerpt_text(remove_code_blocks(body_text(path)))
    paragraphs: list[str] = []
    for block in re.split(r"\n{2,}", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if any(line.startswith("#") for line in lines):
            continue
        if all(line.startswith("|") for line in lines):
            continue
        if any(re.match(r"^([-*+]|\d+[.)])\s+", line) for line in lines):
            continue
        paragraph = normalize_text(" ".join(lines))
        if len(paragraph) < 80 or is_noise_text(paragraph):
            continue
        paragraphs.append(truncate_text(paragraph, char_limit))
        if len(paragraphs) >= max_paragraphs:
            break
    return paragraphs


def extract_list_items(path: Path, max_items: int = 4, char_limit: int = 220) -> list[str]:
    text = clean_excerpt_text(remove_code_blocks(body_text(path)))
    items: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", line)
        if not match:
            continue
        item = normalize_text(match.group(2))
        if len(item) < 10 or is_noise_text(item):
            continue
        items.append(truncate_text(item, char_limit))
        if len(items) >= max_items:
            break
    return items


def extract_first_table(path: Path, max_lines: int = 8) -> str:
    text = clean_excerpt_text(body_text(path))
    current: list[str] = []
    tables: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("|"):
            current.append(line)
            continue
        if len(current) >= 2:
            tables.append(current)
        current = []
    if len(current) >= 2:
        tables.append(current)
    if not tables:
        return ""
    return "\n".join(tables[0][:max_lines]).strip()


def raw_link(raw_ref: str, page_path: str) -> str:
    page_dir = (WIKI / page_path).parent
    relative = os.path.relpath(REPO / raw_ref, page_dir)
    return f"[{raw_ref}]({relative})"


def render_raw_source_list(raw_refs: list[str], page_path: str) -> str:
    lines = []
    for raw_ref in raw_refs:
        path = REPO / raw_ref
        heading = extract_heading(path) if path.exists() else Path(raw_ref).stem
        lines.append(f"- {raw_link(raw_ref, page_path)} — {heading}")
    return "\n".join(lines)


def render_source_digest(path: Path, page_path: str, level: int = 3) -> str:
    raw_ref = str(path.relative_to(REPO))
    heading = extract_heading(path)
    focus_headings = extract_focus_headings(path)
    paragraphs = extract_detail_paragraphs(path)
    list_items = extract_list_items(path)
    table = extract_first_table(path)

    parts = [
        f"{'#' * level} {heading}",
        f"**Raw file:** {raw_link(raw_ref, page_path)}",
    ]
    if focus_headings:
        parts.append("**Focus areas**\n" + "\n".join(f"- {item}" for item in focus_headings))
    if paragraphs:
        parts.append("**Preserved detail**\n" + "\n\n".join(paragraphs))
    if list_items:
        parts.append("**Representative points**\n" + "\n".join(f"- {item}" for item in list_items))
    if table:
        parts.append("**Representative table**\n" + table)
    return "\n\n".join(parts)


def render_source_digests(raw_refs: list[str], page_path: str, level: int = 3) -> str:
    digests = []
    for raw_ref in raw_refs:
        path = REPO / raw_ref
        if not path.exists():
            continue
        digests.append(render_source_digest(path, page_path, level=level))
    return "\n\n".join(digests)


def extract_heading(path: Path) -> str:
    text = read_source_markdown(path)
    lines = text.splitlines()
    fm_title = parse_frontmatter_title(lines)
    if fm_title:
        return fm_title

    for raw_line in lines:
        line = raw_line.strip()
        if line.lower().startswith("title:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")

    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()

    in_code = False
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line:
            continue
        if line.lower().startswith("thinking..."):
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("thinking..."):
            continue
        if line.lower().startswith(("we need to", "the user says", "let's ", "also ", "better ", "thus ", "now,")):
            continue
        if line.startswith("```") or line.startswith(">") or line.startswith("|"):
            continue
        if line.startswith("---"):
            continue
        if line.lower().startswith(("title:", "source:", "author:", "published:", "created:", "description:", "tags:")):
            continue
        return line[:120]

    return path.stem


OVERVIEW = {
    "path": "overview.md",
    "title": "LLM wiki overview",
    "tags": ["overview", "navigation", "wiki"],
    "sources": [
        "raw/security-domain-services-knowledge-base.md",
        "raw/LNCHLOG_Home - KnowledgeHub.md",
        "raw/rulebase/chapter-01-overview.md",
    ],
    "summary": "This wiki condenses the recursive raw corpus into a persistent map of Pega Launchpad runtime services, authoring/model-calculation services, security boundaries, and Launchpad Units operational notes.",
}

DOMAIN_PAGES = [
    {
        "path": "domains/runtime-execution-stack.md",
        "title": "Runtime execution stack",
        "tags": ["domain", "runtime", "platform"],
        "sources": [
            "raw/case/chapter-01-overview.md",
            "raw/data/chapter-01-overview.md",
            "raw/igw/chapter-01-overview.md",
            "raw/UAS/chapter-1-overview.md",
            "raw/oprms/chapter-01-overview-and-mental-model.md",
            "raw/oaz/chapter-01-overview.md",
        ],
        "summary": "This page summarizes the runtime request path: identity starts in UAS, runtime user materialization happens in OprMS, execution fans into DX Case, Data Service, and IGW, and outbound trust is delegated through OAZ.",
        "bullets": [
            "Inbound identity and tenant trust start in Unified Authentication Service and flow into runtime claims.",
            "Operator Management Service translates external identity into a runtime user/operator record.",
            "DX Case Service and Data Service execute most tenant-facing work using model context and downstream bundles.",
            "Integration Gateway executes model-driven outbound connectors, while OAZ supplies outbound auth and access decisions.",
        ],
        "related": [
            "concepts/model-driven-services.md",
            "concepts/identity-and-outbound-trust.md",
            "topics/gateway-service-architecture.md",
            "services/unified-authentication-service.md",
            "services/operator-management-service.md",
            "services/dx-case-service.md",
            "services/data-service.md",
            "services/integration-gateway.md",
            "services/outbound-authorization-service.md",
        ],
        "source_packs": [
            "sources/uas-source-pack.md",
            "sources/oprms-source-pack.md",
            "sources/case-source-pack.md",
            "sources/data-source-pack.md",
            "sources/igw-source-pack.md",
            "sources/oaz-source-pack.md",
        ],
    },
    {
        "path": "domains/authoring-and-model-calculation.md",
        "title": "Authoring and model calculation",
        "tags": ["domain", "authoring", "compilation"],
        "sources": [
            "raw/core-auth/chapter-01-overview.md",
            "raw/rulebase/chapter-01-overview.md",
            "raw/mcs-core/chapter-01-overview.md",
            "raw/mcs-assembly/Chapter 1 Mental Model.md",
            "raw/mps/chapter-01-overview.md",
        ],
        "summary": "This page summarizes the design-time stack: Authoring Service Core orchestrates authoring flows, Rulebase stores versioned rules, the MCS family validates and compiles domain artifacts, and Model Producer Service assembles deployable bundles.",
        "bullets": [
            "Authoring Service Core is stateless orchestration around rule CRUD, publish/deploy flows, and downstream service fan-out.",
            "Rulebase acts like a Git-style rule store with repositories, branches, workspaces, commits, tags, and application layering.",
            "MCS-Core resolves hierarchy and rule selection, while domain MCS services compile case, view, security, integration, and app-logic artifacts.",
            "Model Producer Service aggregates bundle outputs into the model ZIP used for preview, publish, and deployment.",
        ],
        "related": [
            "concepts/bundles-and-layering.md",
            "concepts/model-driven-services.md",
            "services/authoring-service-core.md",
            "services/rulebase-service.md",
            "services/model-calculation-service-core.md",
            "services/model-calculation-service-assembly.md",
            "services/model-calculation-service-case.md",
            "services/model-calculation-service-view.md",
            "services/model-calculation-service-security.md",
            "services/model-calculation-service-igw.md",
            "services/model-calculation-service-applogic.md",
            "services/model-producer-service.md",
        ],
        "source_packs": [
            "sources/core-auth-source-pack.md",
            "sources/rulebase-source-pack.md",
            "sources/mcs-core-source-pack.md",
            "sources/mcs-assembly-source-pack.md",
            "sources/mcs-case-source-pack.md",
            "sources/mcs-view-source-pack.md",
            "sources/mcs-security-source-pack.md",
            "sources/mcs-igw-source-pack.md",
            "sources/mcs-applogic-source-pack.md",
            "sources/mps-source-pack.md",
        ],
    },
    {
        "path": "domains/security-domain-services.md",
        "title": "Security domain services",
        "tags": ["domain", "security", "identity"],
        "sources": [
            "raw/security-domain-services-knowledge-base.md",
            "raw/UAS/chapter-1-overview.md",
            "raw/UAS/khub-security.md",
            "raw/oprms/chapter-01-overview-and-mental-model.md",
            "raw/oaz/chapter-01-overview.md",
        ],
        "summary": "This page captures the security split in the corpus: UAS establishes identity and trust, OprMS materializes runtime user state, OAZ brokers outbound trust and access decisions, and CipherHub Key Manager anchors cryptographic trust.",
        "bullets": [
            "UAS owns inbound authentication, federation, token issuance, and shared token validation.",
            "OprMS turns authenticated identities into internal runtime users, operators, personas, and access groups.",
            "OAZ resolves outbound credentials, evaluates access privileges, and distributes CORS/runtime policy data.",
            "CipherHub Key Manager appears as the cryptographic substrate even though the corpus contains only comparative notes, not a dedicated source pack.",
        ],
        "related": [
            "concepts/identity-and-outbound-trust.md",
            "concepts/mcp-authentication-and-authorization.md",
            "domains/runtime-execution-stack.md",
            "services/unified-authentication-service.md",
            "services/operator-management-service.md",
            "services/outbound-authorization-service.md",
        ],
        "source_packs": [
            "sources/security-domain-services-knowledge-base-source.md",
            "sources/uas-source-pack.md",
            "sources/oprms-source-pack.md",
            "sources/oaz-source-pack.md",
            "sources/service-analysis-notes-source.md",
        ],
    },
    {
        "path": "domains/launchpad-units-observability.md",
        "title": "Launchpad Units observability",
        "tags": ["domain", "launchpad-units", "observability"],
        "sources": [
            "raw/LNCHLOG_Home - KnowledgeHub.md",
            "raw/LNCHLOG_Architecture - KnowledgeHub 1.38.01 PM.md",
            "raw/LNCHLOG_LP Units Transformer Lambda Detailed Design - KnowledgeHub.md",
            "raw/LNCHLOG_Usage Metadata - KnowledgeHub.md",
        ],
        "summary": "This page ties together the Launchpad Units notes: the usage model itself, the log-based ETL pipeline, usage metadata, dashboards, preview behavior, and the environment mapping needed to operate them.",
        "bullets": [
            "Launchpad Units combine raw size-based accounting with service-specific metrics and a growing usage-metadata layer.",
            "The pipeline relies on logging outputs, ETL/transformation, and data-lake-style storage before dashboards consume the data.",
            "Preview mode and provider dashboards are downstream experiences with different freshness and scope tradeoffs.",
            "Environment mapping matters because Infinity dashboards and provider portals are cluster-specific.",
        ],
        "related": [
            "topics/launchpad-units-overview.md",
            "topics/launchpad-units-pipeline-architecture.md",
            "topics/usage-metadata.md",
            "topics/launchpad-units-dashboards.md",
            "topics/launchpad-units-in-preview.md",
            "topics/provider-dashboard-with-infinity-insights.md",
            "topics/infinity-environments-for-launchpad-clusters.md",
        ],
        "source_packs": [
            "sources/lnchlog-home-source.md",
            "sources/lnchlog-architecture-source.md",
            "sources/lnchlog-lp-units-transformer-lambda-detailed-design-source.md",
            "sources/lnchlog-usage-metadata-source.md",
            "sources/lnchlog-launchpad-units-data-dashboards-source.md",
            "sources/lnchlog-launchpad-units-in-preview-source.md",
            "sources/lnchlog-provider-dashboard-implementation-using-infinity-insights-source.md",
            "sources/lnchlog-infinity-environments-for-clusters-source.md",
        ],
    },
]

CONCEPT_PAGES = [
    {
        "path": "concepts/model-driven-services.md",
        "title": "Model-driven services",
        "tags": ["concept", "model-driven", "runtime"],
        "sources": [
            "raw/data/chapter-01-overview.md",
            "raw/igw/chapter-01-overview.md",
            "raw/case/chapter-01-overview.md",
            "raw/mcs-core/chapter-01-overview.md",
        ],
        "summary": "Many services in this corpus do not hard-code business schemas; they fetch model bundles or rule definitions at runtime or publish time and execute against those models.",
        "bullets": [
            "Data Service, IGW, and DX Case all depend on externally computed models or bundles rather than locally owned schemas.",
            "Model and tenant isolation often travel separately, so services can reason about both app structure and operational data boundaries.",
            "The MCS family exists largely to validate, upgrade, and compile those models into faster runtime artifacts.",
        ],
        "related": [
            "domains/runtime-execution-stack.md",
            "domains/authoring-and-model-calculation.md",
            "services/data-service.md",
            "services/integration-gateway.md",
            "services/dx-case-service.md",
            "services/model-calculation-service-core.md",
        ],
        "source_packs": [
            "sources/data-source-pack.md",
            "sources/igw-source-pack.md",
            "sources/case-source-pack.md",
            "sources/mcs-core-source-pack.md",
        ],
    },
    {
        "path": "concepts/bundles-and-layering.md",
        "title": "Bundles and layering",
        "tags": ["concept", "bundles", "layering"],
        "sources": [
            "raw/rulebase/chapter-01-overview.md",
            "raw/mcs-core/chapter-01-overview.md",
            "raw/mps/chapter-01-overview.md",
            "raw/mcs-view/chapter-01-overview.md",
        ],
        "summary": "The platform repeatedly compiles raw rules into layered bundles and deployable archives rather than forcing every consumer to interpret raw rule JSON and inheritance on its own.",
        "bullets": [
            "Rulebase stores the versioned raw materials: repositories, branches, workspaces, transactions, and snapshots.",
            "MCS-Core resolves application hierarchy and rule precedence before domain MCS services produce bundle content.",
            "Model Producer Service collects those bundle outputs into the final model ZIP used by runtime consumers and deployment flows.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "services/rulebase-service.md",
            "services/model-calculation-service-core.md",
            "services/model-calculation-service-view.md",
            "services/model-producer-service.md",
        ],
        "source_packs": [
            "sources/rulebase-source-pack.md",
            "sources/mcs-core-source-pack.md",
            "sources/mps-source-pack.md",
            "sources/core-auth-source-pack.md",
        ],
    },
    {
        "path": "concepts/identity-and-outbound-trust.md",
        "title": "Identity and outbound trust",
        "tags": ["concept", "identity", "security"],
        "sources": [
            "raw/security-domain-services-knowledge-base.md",
            "raw/UAS/chapter-1-overview.md",
            "raw/oprms/chapter-01-overview-and-mental-model.md",
            "raw/oaz/chapter-01-overview.md",
        ],
        "summary": "Identity flows split cleanly across inbound and outbound concerns: UAS authenticates and issues tokens, OprMS materializes runtime users, and OAZ resolves outbound auth contexts and related runtime decisions.",
        "bullets": [
            "Inbound trust and token minting belong to UAS, not to the services that consume the tokens.",
            "Runtime user state belongs to OprMS, which translates identity into the operator/user model other services consume.",
            "Outbound credentials and access checks belong to OAZ, which centralizes external auth mechanics and policy decisions.",
        ],
        "related": [
            "domains/security-domain-services.md",
            "domains/runtime-execution-stack.md",
            "services/unified-authentication-service.md",
            "services/operator-management-service.md",
            "services/outbound-authorization-service.md",
            "concepts/mcp-authentication-and-authorization.md",
        ],
        "source_packs": [
            "sources/security-domain-services-knowledge-base-source.md",
            "sources/uas-source-pack.md",
            "sources/oprms-source-pack.md",
            "sources/oaz-source-pack.md",
        ],
    },
    {
        "path": "concepts/mcp-authentication-and-authorization.md",
        "title": "MCP authentication and authorization",
        "tags": ["concept", "security", "protocol"],
        "sources": [
            "raw/MCP authentication and authorization implementation guide.md",
        ],
        "summary": "MCP security reuses familiar OAuth patterns: MCP servers act as protected resources, clients discover authorization metadata dynamically, use PKCE and optionally dynamic client registration, and present resource-scoped tokens with enforceable scopes.",
        "bullets": [
            "OAuth 2.1 style authorization code flow with PKCE is the baseline security model for MCP clients that act on behalf of users.",
            "Protected-resource metadata and authorization-server metadata let MCP clients discover the right endpoints and token rules without hardcoded configuration.",
            "Resource indicators, scopes, and token-format choices determine how safely an MCP server can delegate access to downstream tools and APIs.",
        ],
        "related": [
            "concepts/identity-and-outbound-trust.md",
            "domains/security-domain-services.md",
            "services/unified-authentication-service.md",
            "topics/gateway-service-architecture.md",
        ],
        "source_packs": [
            "sources/mcp-authentication-and-authorization-guide-source.md",
        ],
    },
]

SERVICE_PAGES = [
    {
        "path": "services/authoring-service-core.md",
        "title": "Authoring Service Core",
        "tags": ["service", "authoring", "design-time"],
        "sources": [
            "raw/core-auth/chapter-01-overview.md",
            "raw/core-auth/chapter-02-api-surface.md",
        ],
        "summary": "Authoring Service Core is the stateless design-time orchestrator for AppStudioX, routing rule CRUD, validation, publish, and deploy operations across Rulebase and the model-calculation services.",
        "bullets": [
            "Owns the versioned authoring API surface used by UI and lifecycle flows.",
            "Persists rules through Rulebase while delegating schema-specific work to the MCS family.",
            "Coordinates publish/deploy interactions with registries, lifecycle services, and model production.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
            "services/rulebase-service.md",
            "services/model-producer-service.md",
            "services/model-calculation-service-core.md",
        ],
        "source_pack": "sources/core-auth-source-pack.md",
    },
    {
        "path": "services/dx-case-service.md",
        "title": "DX Case Service",
        "tags": ["service", "runtime", "cases"],
        "sources": [
            "raw/case/chapter-01-overview.md",
            "raw/case/chapter-02-api-surface.md",
        ],
        "summary": "DX Case Service is the runtime workhorse behind /cases and /assignments, assembling model bundles, delegating orchestration to dx-case-lib, persisting case state, and emitting lifecycle events.",
        "bullets": [
            "Acts as the front door for case and assignment APIs.",
            "Loads models from MSS and persists resulting state through Data Service.",
            "Publishes Kafka events for SLA handling, clipboard snapshots, and other runtime side effects.",
        ],
        "related": [
            "domains/runtime-execution-stack.md",
            "concepts/model-driven-services.md",
            "services/data-service.md",
            "services/integration-gateway.md",
            "services/unified-authentication-service.md",
        ],
        "source_pack": "sources/case-source-pack.md",
    },
    {
        "path": "services/data-service.md",
        "title": "Data Service",
        "tags": ["service", "runtime", "data"],
        "sources": [
            "raw/data/chapter-01-overview.md",
            "raw/data/chapter-02-api-surface.md",
        ],
        "summary": "Data Service is the multi-tenant operational data layer for CNR, exposing model-driven persistence and query APIs over MongoDB Atlas.",
        "bullets": [
            "Stores tenant application objects and reads model definitions from upstream services.",
            "Exposes internal CRUD/query surfaces plus the DX-facing data view API.",
            "Enforces RBAC, collection/index behavior, and isolation boundaries from the active model context.",
        ],
        "related": [
            "domains/runtime-execution-stack.md",
            "concepts/model-driven-services.md",
            "services/dx-case-service.md",
            "services/integration-gateway.md",
            "services/model-calculation-service-security.md",
        ],
        "source_pack": "sources/data-source-pack.md",
    },
    {
        "path": "services/integration-gateway.md",
        "title": "Integration Gateway Service",
        "tags": ["service", "runtime", "integration"],
        "sources": [
            "raw/igw/chapter-01-overview.md",
            "raw/igw/chapter-02-api-surface.md",
        ],
        "summary": "Integration Gateway Service is a model-driven HTTP proxy that executes connector definitions pulled from the model layer instead of hard-coded integrations.",
        "bullets": [
            "Looks up connector models by Model-ID and executes native Kotlin connector logic.",
            "Applies transforms, auth, and error handling defined in the model rather than in caller code.",
            "Depends on OAZ for outbound credentials and on MSS for connector configuration.",
        ],
        "related": [
            "domains/runtime-execution-stack.md",
            "concepts/model-driven-services.md",
            "services/data-service.md",
            "services/outbound-authorization-service.md",
            "services/model-calculation-service-igw.md",
        ],
        "source_pack": "sources/igw-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-applogic.md",
        "title": "Model Calculation Service - App Logic",
        "tags": ["service", "model-calculation", "compiler"],
        "sources": [
            "raw/mcs-applogic/chapter-1-overview-and-mental-model.md",
            "raw/mcs-applogic/chapter-01-overview.md",
        ],
        "summary": "MCS-AppLogic acts as the compiler, linter, and upgrader for business rules such as When, Decision, Automation, Validation, Function, and Agent definitions.",
        "bullets": [
            "Validates rule JSON before save and upgrades older schema versions to supported shapes.",
            "Generates executable programs and artifact bundles for runtime consumption.",
            "Depends on MCS-Core, MCS-Assembly, and sibling MCS services for schema-specific work.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
            "services/model-calculation-service-assembly.md",
            "services/model-calculation-service-core.md",
            "services/authoring-service-core.md",
        ],
        "source_pack": "sources/mcs-applogic-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-assembly.md",
        "title": "Model Calculation Service - Assembly",
        "tags": ["service", "model-calculation", "compiler"],
        "sources": [
            "raw/mcs-assembly/Chapter 1 Mental Model.md",
            "raw/mcs-assembly/chapter-3-compilation-pipeline.md",
        ],
        "summary": "MCS-Assembly is the compilation pipeline service that turns parse trees into typed intermediate forms and stack-machine programs for executable rule logic.",
        "bullets": [
            "Runs the Parse Tree -> AST -> IL -> Program pipeline.",
            "Provides semantic validation, expression tooling, and schema helpers used by other services.",
            "Acts as the lower-level compiler dependency for App Logic, Case, and Security flows.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
            "services/model-calculation-service-applogic.md",
            "services/model-calculation-service-case.md",
            "services/model-calculation-service-core.md",
        ],
        "source_pack": "sources/mcs-assembly-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-case.md",
        "title": "Model Calculation Service - Case",
        "tags": ["service", "model-calculation", "cases"],
        "sources": [
            "raw/mcs-case/chapter-01-overview.md",
            "raw/mcs-case/chapter-02-api-surface.md",
        ],
        "summary": "MCS-Case validates, upgrades, and compiles case-related rules into runtime models and artifacts so DX Case does not need to understand authoring schemas directly.",
        "bullets": [
            "Handles CaseType, Stage, Process, Action, SLA, Status, ObjectRecord, and WorkQueue-style rules.",
            "Upgrades authored rules before calculating stable runtime output.",
            "Depends on MCS-Core for raw rule retrieval and MCS-Assembly for expression-related compilation work.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "services/dx-case-service.md",
            "services/model-calculation-service-core.md",
            "services/model-calculation-service-assembly.md",
        ],
        "source_pack": "sources/mcs-case-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-core.md",
        "title": "Model Calculation Service - Core",
        "tags": ["service", "model-calculation", "core"],
        "sources": [
            "raw/mcs-core/chapter-01-overview.md",
            "raw/mcs-core/chapter-02-api-surface.md",
        ],
        "summary": "MCS-Core computes application hierarchy, rule resolution, and rule-schema context, making it the foundational decision engine under much of the authoring and runtime model stack.",
        "bullets": [
            "Builds per-request sessions that carry hierarchy, resolver state, and caches.",
            "Determines which rule definition wins for a given app and context.",
            "Feeds sibling model-calculation services that need resolved rule JSON or schema context.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/model-driven-services.md",
            "concepts/bundles-and-layering.md",
            "services/rulebase-service.md",
            "services/model-calculation-service-assembly.md",
            "services/model-producer-service.md",
        ],
        "source_pack": "sources/mcs-core-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-igw.md",
        "title": "Model Calculation Service - Integration Gateway",
        "tags": ["service", "model-calculation", "integration"],
        "sources": [
            "raw/mcs-igw/chapter-01-overview.md",
            "raw/mcs-igw/chapter-02-api-surface.md",
        ],
        "summary": "MCS-IGW is the integration-domain compiler that validates integration rules, produces integration bundles, and handles custom-function packaging and deployment details.",
        "bullets": [
            "Validates REST connectors, data connections, custom functions, and related integration rule shapes.",
            "Packages integration-domain artifacts into ZIP outputs consumed downstream.",
            "Bridges authoring concerns with AWS Lambda deployment paths for custom functions.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "services/integration-gateway.md",
            "services/model-calculation-service-core.md",
            "services/model-producer-service.md",
        ],
        "source_pack": "sources/mcs-igw-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-security.md",
        "title": "Model Calculation Service - Security",
        "tags": ["service", "model-calculation", "security"],
        "sources": [
            "raw/mcs-security/chapter-01-overview.md",
            "raw/mcs-security/chapter-02-api-surface.md",
        ],
        "summary": "MCS-Security compiles outbound authorization, application authorization, and user-access artifacts into faster runtime bundles and structured components.",
        "bullets": [
            "Transforms authored security rules into bundle content consumed by runtime services.",
            "Covers outbound auth profiles, RBAC/ABAC authorization, and application user-access artifacts.",
            "Depends on MCS-Core, MCS-Assembly, Rulebase, and UAS for source data and security context.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "domains/security-domain-services.md",
            "services/outbound-authorization-service.md",
            "services/operator-management-service.md",
            "services/model-calculation-service-core.md",
        ],
        "source_pack": "sources/mcs-security-source-pack.md",
    },
    {
        "path": "services/model-calculation-service-view.md",
        "title": "Model Calculation Service - View",
        "tags": ["service", "model-calculation", "ui"],
        "sources": [
            "raw/mcs-view/chapter-01-overview.md",
            "raw/mcs-view/chapter-02-api-surface.md",
        ],
        "summary": "MCS-View assembles UI rule definitions into versioned view bundles, localization packs, and dependency-resolved artifacts consumed by the Constellation front end.",
        "bullets": [
            "Builds view, portal, theme, and localization outputs from authored rule data.",
            "Resolves dependencies and metadata needed by front-end rendering layers.",
            "Participates in schema upgrade and validation so runtime consumers see stable artifacts.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
            "services/model-calculation-service-core.md",
            "services/model-producer-service.md",
        ],
        "source_pack": "sources/mcs-view-source-pack.md",
    },
    {
        "path": "services/model-producer-service.md",
        "title": "Model Producer Service",
        "tags": ["service", "model-production", "deployment"],
        "sources": [
            "raw/mps/chapter-01-overview.md",
            "raw/mps/chapter-02-api-surface.md",
        ],
        "summary": "Model Producer Service is the deployment compiler that aggregates bundle outputs from the MCS family into the final model ZIP delivered to preview and deployment pipelines.",
        "bullets": [
            "Computes layering context and bundle strategy for an application request.",
            "Fetches component and bundle outputs from multiple calculator services in parallel.",
            "Packages the final model artifact with metadata that downstream consumers can interpret.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
            "services/model-calculation-service-core.md",
            "services/authoring-service-core.md",
            "services/rulebase-service.md",
        ],
        "source_pack": "sources/mps-source-pack.md",
    },
    {
        "path": "services/operator-management-service.md",
        "title": "Operator Management Service",
        "tags": ["service", "security", "provisioning"],
        "sources": [
            "raw/oprms/chapter-01-overview-and-mental-model.md",
            "raw/oprms/chapter-02-api-surface.md",
            "raw/oprms/OPMANSVCArchitecture - KnowledgeHub.md",
            "raw/oprms/OPMANSVCOperator-management-service--architecture-design - KnowledgeHub.md",
            "raw/oprms/OPMANSVCAccess Resolution Flow.md",
        ],
        "summary": "Operator Management Service (OprMS) translates authenticated external identities into internal runtime users, operators, personas, and access-group state.",
        "bullets": [
            "Resolves access groups and personas from IDP configuration, authentication-service defaults, user records, and model defaults depending on runtime versus preview context.",
            "Fetches app model bundles to understand valid access groups and persona structures.",
            "Reads and writes user records through Data Service instead of owning its own database.",
            "Sits between inbound authentication in UAS and the runtime services that consume user state.",
        ],
        "related": [
            "domains/security-domain-services.md",
            "domains/runtime-execution-stack.md",
            "concepts/identity-and-outbound-trust.md",
            "services/unified-authentication-service.md",
            "services/data-service.md",
        ],
        "source_pack": "sources/oprms-source-pack.md",
    },
    {
        "path": "services/outbound-authorization-service.md",
        "title": "Outbound Authorization Service",
        "tags": ["service", "security", "integration"],
        "sources": [
            "raw/oaz/chapter-01-overview.md",
            "raw/oaz/chapter-02-api-surface.md",
            "raw/oaz/OUTAUTHOutbound-authorization-service - KnowledgeHub.md",
        ],
        "summary": "Outbound Authorization Service (OAZ) is the runtime broker for external credentials, access-privilege decisions, and CORS policy distribution.",
        "bullets": [
            "Fetches AuthenticationProfile-like data and resolves external auth contexts for callers.",
            "Centralizes OAuth2, basic auth, JWT bearer, and AWS-style outbound credential logic, with the current documented scope centered on OAuth2 client credentials and basic-auth profiles.",
            "Also evaluates runtime access privileges and serves adjacent security policy APIs.",
        ],
        "related": [
            "domains/security-domain-services.md",
            "domains/runtime-execution-stack.md",
            "concepts/identity-and-outbound-trust.md",
            "services/integration-gateway.md",
            "services/unified-authentication-service.md",
        ],
        "source_pack": "sources/oaz-source-pack.md",
    },
    {
        "path": "services/rulebase-service.md",
        "title": "Rulebase Service",
        "tags": ["service", "rulebase", "storage"],
        "sources": [
            "raw/rulebase/chapter-01-overview.md",
            "raw/rulebase/chapter-02-api-surface.md",
        ],
        "summary": "Rulebase Service is the Git-like storage and versioning engine for Launchpad rules, exposing repositories, branches, workspaces, commits, tags, and layered application stacks.",
        "bullets": [
            "Stores and versions the raw rule objects that the rest of the platform compiles and resolves.",
            "Supports the authoring model with branch/workspace semantics and immutable commits/tags.",
            "Works alongside ALMS-style registry data to resolve application hierarchy and release context.",
        ],
        "related": [
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
            "services/authoring-service-core.md",
            "services/model-calculation-service-core.md",
            "services/model-producer-service.md",
        ],
        "source_pack": "sources/rulebase-source-pack.md",
    },
    {
        "path": "services/unified-authentication-service.md",
        "title": "Unified Authentication Service",
        "tags": ["service", "security", "identity"],
        "sources": [
            "raw/UAS/chapter-1-overview.md",
            "raw/UAS/khub-home.md",
            "raw/UAS/khub-architecture.md",
            "raw/UAS/khub-apis.md",
            "raw/UAS/khub-security.md",
            "raw/UAS/Adding a SAML authentication service.md",
            "raw/UAS/Adding an OIDC authentication service.md",
            "raw/UAS/Launchpad-provided authentication (Launchpad IDM).md",
            "raw/security-domain-services-knowledge-base.md",
        ],
        "summary": "Unified Authentication Service (UAS) is the platform's central identity broker and token service, handling authentication, federation, token issuance, token exchange, and client registration.",
        "bullets": [
            "Brokers SAML, OIDC, TOTP, OAuth2, and related login or service-auth flows.",
            "Issues and validates JWTs used by runtime and authoring services across the platform.",
            "Supports both third-party identity-provider configuration and the Launchpad-provided IDM path used for subscriber and runtime-user authentication.",
            "Provides the shared trust anchor that downstream services use for token validation and machine-to-machine auth.",
        ],
        "related": [
            "domains/security-domain-services.md",
            "domains/runtime-execution-stack.md",
            "concepts/identity-and-outbound-trust.md",
            "concepts/mcp-authentication-and-authorization.md",
            "services/operator-management-service.md",
            "services/outbound-authorization-service.md",
        ],
        "source_pack": "sources/uas-source-pack.md",
    },
]

TOPIC_PAGES = [
    {
        "path": "topics/launchpad-units-overview.md",
        "title": "Launchpad Units overview",
        "tags": ["topic", "launchpad-units", "pricing"],
        "sources": [
            "raw/LNCHLOG_Home - KnowledgeHub.md",
            "raw/LNCHLOG_Usage Metadata - KnowledgeHub.md",
        ],
        "summary": "Launchpad Units are the platform's usage-accounting abstraction: reads cost one unit per MB, writes cost two, and GenAI calls map model/token cost into unit equivalents.",
        "bullets": [
            "The notes treat Launchpad Units as both a pricing primitive and an internal cost-modeling signal.",
            "GenAI Connect usage adds a model-cost-based formula instead of simple byte accounting.",
            "Usage metadata is the next layer that explains which actions, rules, or data pages generated the units.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/usage-metadata.md",
            "topics/launchpad-units-dashboards.md",
            "topics/launchpad-units-in-preview.md",
            "topics/launchpad-units-pipeline-architecture.md",
        ],
        "source_packs": [
            "sources/lnchlog-home-source.md",
            "sources/lnchlog-usage-metadata-source.md",
        ],
    },
    {
        "path": "topics/launchpad-units-pipeline-architecture.md",
        "title": "Launchpad Units pipeline architecture",
        "tags": ["topic", "launchpad-units", "architecture"],
        "sources": [
            "raw/LNCHLOG_Architecture - KnowledgeHub 1.38.01 PM.md",
            "raw/LNCHLOG_LP Units Transformer Lambda Detailed Design - KnowledgeHub.md",
        ],
        "summary": "These notes describe the log-to-data-lake pipeline for Launchpad Units: services publish logs, an AWS Lambda ETL/transformer reads S3 through scheduling and queueing primitives, and transformed files land in analytics storage.",
        "bullets": [
            "Architecture notes describe S3, SQS, EventBridge, AWS Lambda, and downstream data-lake storage, including a shift toward Databricks.",
            "Detailed design notes add scheduled versus on-demand modes, service strategies, event types, and ETL bottlenecks.",
            "The pipeline is an operational analytics path, not a runtime user-facing service.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/launchpad-units-overview.md",
            "topics/usage-metadata.md",
            "topics/launchpad-units-dashboards.md",
        ],
        "source_packs": [
            "sources/lnchlog-architecture-source.md",
            "sources/lnchlog-lp-units-transformer-lambda-detailed-design-source.md",
        ],
    },
    {
        "path": "topics/launchpad-units-dashboards.md",
        "title": "Launchpad Units dashboards",
        "tags": ["topic", "launchpad-units", "dashboards"],
        "sources": [
            "raw/LNCHLOG_Launchpad Units Data Dashboards - KnowledgeHub.md",
            "raw/LNCHLOG_Provider Dashboard implementation using Infinity Insights - KnowledgeHub 1.39.04 PM.md",
        ],
        "summary": "The corpus describes two main Launchpad Units dashboards: an internal Power BI dashboard and a provider-facing dashboard backed by Infinity Insights and portal navigation.",
        "bullets": [
            "The Power BI dashboard is positioned as an internal analytics surface for support, product, and leadership audiences.",
            "The provider dashboard is a tenant-facing view layered into the Launchpad portal experience.",
            "Both dashboards depend on upstream ETL and analytics pipelines rather than generating units themselves.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/provider-dashboard-with-infinity-insights.md",
            "topics/infinity-environments-for-launchpad-clusters.md",
            "topics/launchpad-units-overview.md",
        ],
        "source_packs": [
            "sources/lnchlog-launchpad-units-data-dashboards-source.md",
            "sources/lnchlog-provider-dashboard-implementation-using-infinity-insights-source.md",
            "sources/lnchlog-infinity-environments-for-clusters-source.md",
        ],
    },
    {
        "path": "topics/launchpad-units-in-preview.md",
        "title": "Launchpad Units in preview",
        "tags": ["topic", "launchpad-units", "preview"],
        "sources": [
            "raw/LNCHLOG_Launchpad units in preview - KnowledgeHub.md",
        ],
        "summary": "Preview mode exposes near-real-time Launchpad Units in the UI by piggybacking on tracing and metrics flows, but the notes call out freshness lag and unsupported background activity.",
        "bullets": [
            "Preview updates are intentionally lighter-weight than the full billing or analytics pipeline.",
            "Background jobs, SLA work, and some GenAI-related behavior are explicitly excluded or only partially supported.",
            "A last-refreshed notion exists because pipeline latency can surface stale values for a few minutes.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/launchpad-units-overview.md",
            "topics/launchpad-units-dashboards.md",
        ],
        "source_packs": [
            "sources/lnchlog-launchpad-units-in-preview-source.md",
        ],
    },
    {
        "path": "topics/provider-dashboard-with-infinity-insights.md",
        "title": "Provider dashboard with Infinity Insights",
        "tags": ["topic", "launchpad-units", "provider-dashboard"],
        "sources": [
            "raw/LNCHLOG_Provider Dashboard implementation using Infinity Insights - KnowledgeHub 1.39.04 PM.md",
        ],
        "summary": "The provider dashboard path maps Athena-backed Launchpad Units data into an Infinity class, refreshes it on a scheduler, and surfaces the result inside provider-facing portal views.",
        "bullets": [
            "The notes emphasize Athena connectivity, Data Designer mapping, and scheduled refresh into a more interactive application-facing data store.",
            "This topic is the bridge between analytics storage and provider-facing portal UX.",
            "It complements the dashboard overview and environment mapping pages.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/launchpad-units-dashboards.md",
            "topics/infinity-environments-for-launchpad-clusters.md",
        ],
        "source_packs": [
            "sources/lnchlog-provider-dashboard-implementation-using-infinity-insights-source.md",
        ],
    },
    {
        "path": "topics/infinity-environments-for-launchpad-clusters.md",
        "title": "Infinity environments for Launchpad clusters",
        "tags": ["topic", "launchpad", "environments"],
        "sources": [
            "raw/LNCHLOG_Infinity Environments for Clusters - KnowledgeHub.md",
        ],
        "summary": "This topic maps Launchpad environments and clusters to their corresponding Infinity URLs and access patterns, which matters when operating dashboards and provider-facing experiences.",
        "bullets": [
            "The source is operational rather than architectural: it is a lookup page for cluster-to-portal mapping.",
            "Lower environments and production environments have different access expectations.",
            "This mapping is especially useful for provider dashboard validation and support workflows.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/launchpad-units-dashboards.md",
            "topics/provider-dashboard-with-infinity-insights.md",
        ],
        "source_packs": [
            "sources/lnchlog-infinity-environments-for-clusters-source.md",
        ],
    },
    {
        "path": "topics/usage-metadata.md",
        "title": "Usage metadata",
        "tags": ["topic", "launchpad-units", "metadata"],
        "sources": [
            "raw/LNCHLOG_Usage Metadata - KnowledgeHub.md",
        ],
        "summary": "Usage metadata enriches raw Launchpad Units with business context such as trace IDs, rule names, data page names, event names, and service-specific event payloads.",
        "bullets": [
            "The notes define a common event envelope, required identifiers, and validation rules.",
            "Data Service, IGW, RES, and DX Case are the named producers in the current material.",
            "This topic is the bridge between low-level metrics and human-meaningful usage analysis.",
        ],
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/launchpad-units-overview.md",
            "topics/launchpad-units-pipeline-architecture.md",
        ],
        "source_packs": [
            "sources/lnchlog-usage-metadata-source.md",
        ],
    },
    {
        "path": "topics/gateway-service-architecture.md",
        "title": "Gateway Service architecture",
        "tags": ["topic", "gateway", "architecture"],
        "sources": [
            "raw/Gateway/learnservice-notes.md",
            "raw/Gateway/learnservice-chapter4.md",
            "raw/Gateway/learnservice-chapter5.md",
        ],
        "summary": "Gateway Service is the KrakenD-based HTTP front door for Hermes, combining custom Go auth plugins with Swagger-to-config generation and maintaining separate single-tenant and multi-tenant deployment modes.",
        "bullets": [
            "KrakenD provides the routing engine while custom Go plugins handle OAuth redirects, cookie extraction, JWT validation, and tenant-context injection.",
            "A Java Swagger/OpenAPI code generator plus Mustache templates assemble KrakenD configuration from API specs instead of hand-maintained JSON.",
            "Single-tenant and multi-tenant modes differ enough in path shaping, claim checks, and packaging that they are maintained as separate code paths.",
        ],
        "related": [
            "domains/runtime-execution-stack.md",
            "services/unified-authentication-service.md",
            "services/integration-gateway.md",
            "topics/gateway-service-high-level-component-diagram.md",
        ],
        "source_packs": [
            "sources/gateway-service-source-pack.md",
            "sources/gateway-service-high-level-component-diagram-source.md",
        ],
    },
    {
        "path": "topics/gateway-service-high-level-component-diagram.md",
        "title": "Gateway Service high-level component diagram",
        "tags": ["topic", "diagram", "gateway"],
        "sources": [
            "raw/GATESERVHigh-level-component-diagram - KnowledgeHub 1.md",
        ],
        "summary": "This page preserves the older diagram-centric Gateway Service note and now complements the fuller Gateway Service architecture material elsewhere in the wiki.",
        "bullets": [
            "The raw note is image-heavy and prose-light, so it works best as supporting evidence rather than a primary explainer.",
            "Use it to cross-check naming and topology against the richer Gateway Service architecture notes, not to reconstruct runtime behavior by itself.",
            "It sits adjacent to the broader Launchpad and gateway-related operational notes.",
        ],
        "related": [
            "topics/gateway-service-architecture.md",
            "services/integration-gateway.md",
        ],
        "source_packs": [
            "sources/gateway-service-source-pack.md",
            "sources/gateway-service-high-level-component-diagram-source.md",
        ],
    },
]

SOURCE_PACKS = [
    {
        "path": "sources/case-source-pack.md",
        "title": "DX Case Service source pack",
        "tags": ["source-pack", "service", "runtime"],
        "kind": "dir",
        "value": "case",
        "summary": "Canonical service corpus for DX Case Service, covering overview, API surface, data model, core logic, infrastructure, and testing.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/dx-case-service.md",
            "domains/runtime-execution-stack.md",
            "concepts/model-driven-services.md",
        ],
    },
    {
        "path": "sources/core-auth-source-pack.md",
        "title": "Authoring Service Core source pack",
        "tags": ["source-pack", "service", "authoring"],
        "kind": "dir",
        "value": "core-auth",
        "summary": "Canonical service corpus for Authoring Service Core, centered on authoring orchestration, rule operations, and design-time lifecycle concerns.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/authoring-service-core.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/data-source-pack.md",
        "title": "Data Service source pack",
        "tags": ["source-pack", "service", "data"],
        "kind": "dir",
        "value": "data",
        "summary": "Canonical service corpus for Data Service, focused on model-driven persistence, query APIs, runtime authorization, and operational concerns.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/data-service.md",
            "domains/runtime-execution-stack.md",
            "concepts/model-driven-services.md",
        ],
    },
    {
        "path": "sources/igw-source-pack.md",
        "title": "Integration Gateway source pack",
        "tags": ["source-pack", "service", "integration"],
        "kind": "dir",
        "value": "igw",
        "summary": "Canonical service corpus for Integration Gateway, focused on model-driven connector execution, transforms, runtime auth, and external API calls.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/integration-gateway.md",
            "domains/runtime-execution-stack.md",
            "concepts/model-driven-services.md",
        ],
    },
    {
        "path": "sources/gateway-service-source-pack.md",
        "title": "Gateway Service source pack",
        "tags": ["source-pack", "topic", "gateway"],
        "kind": "dir",
        "value": "Gateway",
        "summary": "Canonical source pack for Gateway Service, covering the KrakenD mental model, Swagger-to-config generation pipeline, and the single-tenant versus multi-tenant runtime tradeoffs.",
        "themes": "KrakenD gateway mental model, custom auth plugins, Swagger-to-config generation, config fragments, tenant modes, and deployment tradeoffs",
        "related": [
            "topics/gateway-service-architecture.md",
            "topics/gateway-service-high-level-component-diagram.md",
            "services/unified-authentication-service.md",
            "services/integration-gateway.md",
        ],
    },
    {
        "path": "sources/mcs-applogic-source-pack.md",
        "title": "MCS-AppLogic source pack",
        "tags": ["source-pack", "service", "model-calculation"],
        "kind": "dir",
        "value": "mcs-applogic",
        "summary": "Canonical source pack for MCS-AppLogic, including both chapterized overviews and deeper notes on core business logic, schema versioning, and testing.",
        "themes": "overview, API surface, data model, business logic, schema versioning, infrastructure, and testing",
        "related": [
            "services/model-calculation-service-applogic.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/mcs-assembly-source-pack.md",
        "title": "MCS-Assembly source pack",
        "tags": ["source-pack", "service", "model-calculation"],
        "kind": "dir",
        "value": "mcs-assembly",
        "summary": "Canonical source pack for MCS-Assembly, covering the mental model, compilation pipeline, expression processing, schema management, and deep-dive Q&A notes.",
        "themes": "mental model, compilation pipeline, expression processing, schema management, and deep-dive Q&A",
        "related": [
            "services/model-calculation-service-assembly.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/mcs-case-source-pack.md",
        "title": "MCS-Case source pack",
        "tags": ["source-pack", "service", "model-calculation"],
        "kind": "dir",
        "value": "mcs-case",
        "summary": "Canonical source pack for MCS-Case, centered on case rule validation, upgrade, bundle generation, and supporting operational chapters.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/model-calculation-service-case.md",
            "domains/authoring-and-model-calculation.md",
            "services/dx-case-service.md",
        ],
    },
    {
        "path": "sources/mcs-core-source-pack.md",
        "title": "MCS-Core source pack",
        "tags": ["source-pack", "service", "model-calculation"],
        "kind": "dir",
        "value": "mcs-core",
        "summary": "Canonical source pack for MCS-Core, covering application hierarchy, session-based rule resolution, caches, infrastructure, and testing.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/model-calculation-service-core.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/mcs-igw-source-pack.md",
        "title": "MCS-IGW source pack",
        "tags": ["source-pack", "service", "model-calculation"],
        "kind": "dir",
        "value": "mcs-igw",
        "summary": "Canonical source pack for MCS-IGW, focused on integration rule validation, bundle generation, AWS custom-function deployment, and service operations.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/model-calculation-service-igw.md",
            "domains/authoring-and-model-calculation.md",
            "services/integration-gateway.md",
        ],
    },
    {
        "path": "sources/mcs-security-source-pack.md",
        "title": "MCS-Security source pack",
        "tags": ["source-pack", "service", "security"],
        "kind": "dir",
        "value": "mcs-security",
        "summary": "Canonical source pack for MCS-Security, covering outbound auth compilation, authorization artifacts, user access, infrastructure, and test coverage.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/model-calculation-service-security.md",
            "domains/security-domain-services.md",
            "domains/authoring-and-model-calculation.md",
        ],
    },
    {
        "path": "sources/mcs-view-source-pack.md",
        "title": "MCS-View source pack",
        "tags": ["source-pack", "service", "ui"],
        "kind": "dir",
        "value": "mcs-view",
        "summary": "Canonical source pack for MCS-View, focused on UI rule assembly, view dependencies, localization bundles, and related infrastructure/testing.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/model-calculation-service-view.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/mps-source-pack.md",
        "title": "Model Producer Service source pack",
        "tags": ["source-pack", "service", "deployment"],
        "kind": "dir",
        "value": "mps",
        "summary": "Canonical source pack for Model Producer Service, focused on bundle aggregation, model ZIP generation, layering, infrastructure, and testing.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/model-producer-service.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/oaz-source-pack.md",
        "title": "Outbound Authorization Service source pack",
        "tags": ["source-pack", "service", "security"],
        "kind": "dir",
        "value": "oaz",
        "summary": "Canonical source pack for Outbound Authorization Service, covering outbound credential vending, runtime access privileges, KnowledgeHub service framing, and service operations.",
        "themes": "overview, API surface, supported auth types, service dependencies, data model, core logic, infrastructure, and testing",
        "related": [
            "services/outbound-authorization-service.md",
            "domains/security-domain-services.md",
            "concepts/identity-and-outbound-trust.md",
        ],
    },
    {
        "path": "sources/oprms-source-pack.md",
        "title": "Operator Management Service source pack",
        "tags": ["source-pack", "service", "security"],
        "kind": "dir",
        "value": "oprms",
        "summary": "Canonical source pack for Operator Management Service, focused on runtime user provisioning, access-resolution logic, architecture/ADR material, bundle parsing, downstream clients, and service operations.",
        "themes": "overview, API surface, access-resolution flow, architecture diagrams, ADR links, data model, core business logic, and infrastructure/ops",
        "related": [
            "services/operator-management-service.md",
            "domains/security-domain-services.md",
            "concepts/identity-and-outbound-trust.md",
        ],
    },
    {
        "path": "sources/rulebase-source-pack.md",
        "title": "Rulebase Service source pack",
        "tags": ["source-pack", "service", "rulebase"],
        "kind": "dir",
        "value": "rulebase",
        "summary": "Canonical source pack for Rulebase Service, covering Git-like rule storage, branch/workspace operations, hierarchy concepts, infrastructure, and testing.",
        "themes": "overview, API surface, data model, core logic, infrastructure, and testing",
        "related": [
            "services/rulebase-service.md",
            "domains/authoring-and-model-calculation.md",
            "concepts/bundles-and-layering.md",
        ],
    },
    {
        "path": "sources/uas-source-pack.md",
        "title": "Unified Authentication Service source pack",
        "tags": ["source-pack", "service", "identity"],
        "kind": "dir",
        "value": "UAS",
        "summary": "Canonical source pack for Unified Authentication Service, covering service-hub architecture pages plus overview material, OAuth2 flows, authentication modes, subscriber authentication configuration, Launchpad IDM, security, reliability, performance, KT, and review notes.",
        "themes": "service-hub overview, architecture, API references, SAML/OIDC configuration, Launchpad IDM, security, reliability, OAuth2 flows, authentication modes, client registration, data model, performance, KT, and review notes",
        "related": [
            "services/unified-authentication-service.md",
            "domains/security-domain-services.md",
            "concepts/identity-and-outbound-trust.md",
        ],
    },
    {
        "path": "sources/lnchlog-architecture-source.md",
        "title": "Launchpad Units architecture source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_architecture - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports covering the Launchpad Units high-level pipeline, data-lake integration, deployment, and sequence-diagram architecture.",
        "themes": "pipeline overview, data-lake integration, deployment diagrams, and auth flow sequence",
        "related": [
            "topics/launchpad-units-pipeline-architecture.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-home-source.md",
        "title": "Launchpad Units home source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_home - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports describing Launchpad Units fundamentals, pricing math, and the roadmap for usage-metadata-driven analysis.",
        "themes": "unit accounting rules, pricing formulae, supported LLM pricing, and analytics roadmap",
        "related": [
            "topics/launchpad-units-overview.md",
            "topics/usage-metadata.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-infinity-environments-for-clusters-source.md",
        "title": "Infinity environments for clusters source",
        "tags": ["source-pack", "topic", "launchpad"],
        "kind": "norm-title",
        "value": "lnchlog_infinity environments for clusters - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports that map Launchpad environments to Infinity URLs and access expectations.",
        "themes": "environment mapping, cluster URLs, and operational access guidance",
        "related": [
            "topics/infinity-environments-for-launchpad-clusters.md",
            "topics/launchpad-units-dashboards.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-launchpad-units-data-dashboards-source.md",
        "title": "Launchpad Units dashboards source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_launchpad units data dashboards - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports describing the internal Power BI dashboard and the provider-facing dashboard entry points.",
        "themes": "Power BI dashboard, provider dashboard, access, and implementation pointers",
        "related": [
            "topics/launchpad-units-dashboards.md",
            "topics/provider-dashboard-with-infinity-insights.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-launchpad-units-in-preview-source.md",
        "title": "Launchpad Units in preview source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_launchpad units in preview - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports describing how Launchpad Units appear in preview sessions and which limitations still apply.",
        "themes": "preview UX, realtime-ish updates, lag, and unsupported background work",
        "related": [
            "topics/launchpad-units-in-preview.md",
            "topics/launchpad-units-overview.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-lp-units-transformer-lambda-detailed-design-source.md",
        "title": "Launchpad Units transformer lambda source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_lp units transformer lambda detailed design - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports describing the ETL service that reads logs, groups events, writes transformed files, and pushes Launchpad Units outputs to analytics storage.",
        "themes": "ETL flow, on-demand vs scheduled processing, event models, factories, and current bottlenecks",
        "related": [
            "topics/launchpad-units-pipeline-architecture.md",
            "topics/usage-metadata.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-provider-dashboard-implementation-using-infinity-insights-source.md",
        "title": "Provider dashboard implementation source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_provider dashboard implementation using infinity insights - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports describing how provider dashboards use Infinity Insights, Athena-backed classes, scheduled refreshes, and cached Infinity-side data.",
        "themes": "Athena mapping, schedulers, data flows, and provider-facing dashboard implementation",
        "related": [
            "topics/provider-dashboard-with-infinity-insights.md",
            "topics/launchpad-units-dashboards.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/lnchlog-usage-metadata-source.md",
        "title": "Usage metadata source",
        "tags": ["source-pack", "topic", "launchpad-units"],
        "kind": "norm-title",
        "value": "lnchlog_usage metadata - knowledgehub",
        "summary": "Consolidated KnowledgeHub exports that define usage-metadata event structure, validation rules, supported producer services, and file output conventions.",
        "themes": "event envelope, validation, service producers, configuration, and output conventions",
        "related": [
            "topics/usage-metadata.md",
            "topics/launchpad-units-overview.md",
            "domains/launchpad-units-observability.md",
        ],
    },
    {
        "path": "sources/security-domain-services-knowledge-base-source.md",
        "title": "Security domain services knowledge base source",
        "tags": ["source-pack", "topic", "security"],
        "kind": "file",
        "value": "raw/security-domain-services-knowledge-base.md",
        "summary": "Comparative knowledge-base note that frames UAS, OprMS, OAZ, and CipherHub Key Manager as complementary security-domain services rather than overlapping ones.",
        "themes": "service comparison, layered domain model, and end-to-end identity/trust flows",
        "related": [
            "domains/security-domain-services.md",
            "concepts/identity-and-outbound-trust.md",
            "services/unified-authentication-service.md",
            "services/operator-management-service.md",
            "services/outbound-authorization-service.md",
        ],
    },
    {
        "path": "sources/gateway-service-high-level-component-diagram-source.md",
        "title": "Gateway Service diagram source",
        "tags": ["source-pack", "topic", "diagram"],
        "kind": "file",
        "value": "raw/GATESERVHigh-level-component-diagram - KnowledgeHub 1.md",
        "summary": "Image-heavy KnowledgeHub note containing Gateway Service high-level component diagrams with minimal explanatory prose.",
        "themes": "architecture diagrams and naming/topology reference",
        "related": [
            "topics/gateway-service-high-level-component-diagram.md",
            "topics/gateway-service-architecture.md",
            "topics/launchpad-units-pipeline-architecture.md",
        ],
    },
    {
        "path": "sources/mcp-authentication-and-authorization-guide-source.md",
        "title": "MCP authentication and authorization guide source",
        "tags": ["source-pack", "topic", "security"],
        "kind": "file",
        "value": "raw/MCP authentication and authorization implementation guide.md",
        "summary": "External guide that frames MCP servers as OAuth-protected resources and walks through discovery endpoints, PKCE, dynamic client registration, consent, scopes, token validation, and deployment patterns.",
        "themes": "OAuth 2.1, PKCE, dynamic client registration, consent, resource metadata, scopes, token formats, and MCP deployment patterns",
        "related": [
            "concepts/mcp-authentication-and-authorization.md",
            "concepts/identity-and-outbound-trust.md",
            "services/unified-authentication-service.md",
        ],
    },
    {
        "path": "sources/knowledgehub-note-source.md",
        "title": "KnowledgeHub note source",
        "tags": ["source-pack", "topic", "knowledgehub"],
        "kind": "file",
        "value": "raw/KnowledgeHub.md",
        "summary": "Small pre-existing note that treats KnowledgeHub as a concept page and links it to Launchpad dashboards and Infinity environment material.",
        "themes": "KnowledgeHub as a concept node and link hub",
        "related": [
            "domains/launchpad-units-observability.md",
            "topics/launchpad-units-dashboards.md",
        ],
    },
    {
        "path": "sources/service-analysis-notes-source.md",
        "title": "Service analysis notes source",
        "tags": ["source-pack", "topic", "analysis"],
        "kind": "file",
        "value": "raw/service-notes 11.15.20 PM.md",
        "summary": "Generated service-analysis note focused mainly on UAS and adjacent security/runtime APIs, useful as supporting evidence alongside the chapterized service corpora.",
        "themes": "deep-dive API notes, endpoint examples, and UAS-centric analysis",
        "related": [
            "domains/security-domain-services.md",
            "services/unified-authentication-service.md",
            "services/operator-management-service.md",
        ],
    },
]


def dedupe_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def read_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def normalize_manifest_path(path: str | None) -> str:
    cleaned = str(path or "").strip().lstrip("/")
    if not cleaned:
        return ""
    if not cleaned.endswith(".md"):
        cleaned += ".md"
    return cleaned


def normalize_raw_refs(raw_refs: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_ref in raw_refs:
        cleaned = str(raw_ref).strip()
        if not cleaned:
            continue
        if cleaned.startswith("/"):
            cleaned = cleaned[1:]
        normalized.append(cleaned)
    return dedupe_values(normalized)


def normalize_manifest_page_spec(raw_spec: dict) -> dict | None:
    path = normalize_manifest_path(raw_spec.get("path"))
    title = clean_title(str(raw_spec.get("title", "")))
    summary = normalize_text(str(raw_spec.get("summary", "")))
    bullets = dedupe_values([str(item) for item in raw_spec.get("bullets", [])])
    related = dedupe_values(normalize_manifest_path(item) for item in raw_spec.get("related", []))
    source_packs = dedupe_values(normalize_manifest_path(item) for item in raw_spec.get("source_packs", []))
    tags = dedupe_values([str(tag) for tag in raw_spec.get("tags", [])])
    sources = normalize_raw_refs(raw_spec.get("sources", []))
    if not path or not title or not summary or not bullets:
        return None
    return {
        "id": str(raw_spec.get("id", path)).strip(),
        "path": path,
        "title": title,
        "tags": tags or ["topic", "auto-ingest"],
        "sources": sources,
        "summary": summary,
        "bullets": bullets,
        "related": related,
        "source_packs": source_packs,
    }


def normalize_manifest_source_pack_spec(raw_spec: dict) -> dict | None:
    path = normalize_manifest_path(raw_spec.get("path"))
    title = clean_title(str(raw_spec.get("title", "")))
    summary = normalize_text(str(raw_spec.get("summary", "")))
    themes = normalize_text(str(raw_spec.get("themes", "")))
    tags = dedupe_values([str(tag) for tag in raw_spec.get("tags", [])])
    related = dedupe_values(normalize_manifest_path(item) for item in raw_spec.get("related", []))
    sources = normalize_raw_refs(raw_spec.get("sources", []))
    if not path or not title or not summary or not themes or not sources:
        return None
    return {
        "id": str(raw_spec.get("id", path)).strip(),
        "path": path,
        "title": title,
        "tags": tags or ["source-pack", "auto-ingest"],
        "kind": "explicit",
        "sources": sources,
        "summary": summary,
        "themes": themes,
        "related": related,
    }


def normalize_manifest_overlay(raw_spec: dict) -> dict | None:
    path = normalize_manifest_path(raw_spec.get("path"))
    if not path:
        return None
    return {
        "id": str(raw_spec.get("id", path)).strip(),
        "path": path,
        "sources": normalize_raw_refs(raw_spec.get("sources", [])),
        "bullets": dedupe_values([str(item) for item in raw_spec.get("bullets", [])]),
        "related": dedupe_values(normalize_manifest_path(item) for item in raw_spec.get("related", [])),
        "source_packs": dedupe_values(normalize_manifest_path(item) for item in raw_spec.get("source_packs", [])),
        "extra_source_packs": dedupe_values(
            normalize_manifest_path(item) for item in raw_spec.get("extra_source_packs", [])
        ),
    }


def load_dynamic_manifest() -> dict[str, list[dict]]:
    data = read_json_file(MANIFEST_PATH, {})
    if not isinstance(data, dict):
        return {"topics": [], "source_packs": [], "overlays": []}

    topics = [
        spec
        for raw_spec in data.get("topics", [])
        if isinstance(raw_spec, dict)
        for spec in [normalize_manifest_page_spec(raw_spec)]
        if spec
    ]
    source_packs = [
        spec
        for raw_spec in data.get("source_packs", [])
        if isinstance(raw_spec, dict)
        for spec in [normalize_manifest_source_pack_spec(raw_spec)]
        if spec
    ]
    overlays = [
        spec
        for raw_spec in data.get("overlays", [])
        if isinstance(raw_spec, dict)
        for spec in [normalize_manifest_overlay(raw_spec)]
        if spec
    ]

    return {
        "topics": topics,
        "source_packs": source_packs,
        "overlays": overlays,
    }


BASE_DOMAIN_PAGES = deepcopy(DOMAIN_PAGES)
BASE_CONCEPT_PAGES = deepcopy(CONCEPT_PAGES)
BASE_SERVICE_PAGES = deepcopy(SERVICE_PAGES)
BASE_TOPIC_PAGES = deepcopy(TOPIC_PAGES)
BASE_SOURCE_PACKS = deepcopy(SOURCE_PACKS)


def merge_overlay_into_spec(spec: dict, overlay: dict) -> None:
    spec["sources"] = dedupe_values(list(spec.get("sources", [])) + list(overlay.get("sources", [])))
    spec["bullets"] = dedupe_values(list(spec.get("bullets", [])) + list(overlay.get("bullets", [])))
    spec["related"] = dedupe_values(list(spec.get("related", [])) + list(overlay.get("related", [])))

    overlay_source_packs = dedupe_values(
        list(overlay.get("source_packs", [])) + list(overlay.get("extra_source_packs", []))
    )
    if "source_pack" in spec:
        spec["extra_source_packs"] = dedupe_values(
            list(spec.get("extra_source_packs", [])) + overlay_source_packs
        )
    else:
        spec["source_packs"] = dedupe_values(list(spec.get("source_packs", [])) + overlay_source_packs)


def refresh_dynamic_collections() -> None:
    global DOMAIN_PAGES, CONCEPT_PAGES, SERVICE_PAGES, TOPIC_PAGES, SOURCE_PACKS

    manifest = load_dynamic_manifest()

    DOMAIN_PAGES = deepcopy(BASE_DOMAIN_PAGES)
    CONCEPT_PAGES = deepcopy(BASE_CONCEPT_PAGES)
    SERVICE_PAGES = deepcopy(BASE_SERVICE_PAGES)
    TOPIC_PAGES = deepcopy(BASE_TOPIC_PAGES) + manifest["topics"]
    SOURCE_PACKS = deepcopy(BASE_SOURCE_PACKS) + manifest["source_packs"]

    overlays_by_path: dict[str, list[dict]] = defaultdict(list)
    for overlay in manifest["overlays"]:
        overlays_by_path[overlay["path"]].append(overlay)

    for collection in (DOMAIN_PAGES, CONCEPT_PAGES, SERVICE_PAGES, TOPIC_PAGES):
        for spec in collection:
            for overlay in overlays_by_path.get(spec["path"], []):
                merge_overlay_into_spec(spec, overlay)


def collect_sources(spec: dict) -> list[Path]:
    kind = spec["kind"]
    value = spec.get("value")
    if kind == "dir":
        return sorted((RAW / value).glob("*.md"))
    if kind == "file":
        return [REPO / value]
    if kind == "norm-title":
        matched = []
        for path in RAW.rglob("*.md"):
            if path.name == ".DS_Store":
                continue
            if normalize_stem(path.stem) == value:
                matched.append(path)
        return sorted(matched)
    if kind == "explicit":
        return [REPO / raw_ref for raw_ref in spec.get("sources", []) if (REPO / raw_ref).exists()]
    raise ValueError(f"Unsupported source kind: {kind}")


def render_generic_page(spec: dict) -> str:
    bullets = "\n".join(f"- {item}" for item in spec["bullets"])
    related = bullet_links(spec["related"])
    source_packs = bullet_links(spec.get("source_packs", []))
    raw_sources = render_raw_source_list(spec["sources"], spec["path"])
    source_detail = render_source_digests(spec["sources"], spec["path"], level=3)
    body = (
        f"{spec['summary']}\n\n"
        f"## Key threads\n"
        f"{bullets}\n\n"
        f"## Raw sources\n"
        f"{raw_sources}\n\n"
        f"## Source-derived detail\n"
        f"{source_detail}\n\n"
        f"## Related pages\n"
        f"{related}\n"
    )
    if source_packs:
        body += f"\n## Source packs\n{source_packs}\n"
    return render_page(spec["title"], spec["tags"], spec["sources"], body)


def render_service_page(spec: dict) -> str:
    bullets = "\n".join(f"- {item}" for item in spec["bullets"])
    related = bullet_links(spec["related"])
    raw_sources = render_raw_source_list(spec["sources"], spec["path"])
    source_detail = render_source_digests(spec["sources"], spec["path"], level=3)
    source_pack_paths = [spec["source_pack"], *spec.get("extra_source_packs", [])]
    source_pack_links = bullet_links(source_pack_paths)
    body = (
        f"{spec['summary']}\n\n"
        f"## Core role\n"
        f"{bullets}\n\n"
        f"## Raw sources\n"
        f"{raw_sources}\n\n"
        f"## Source-derived detail\n"
        f"{source_detail}\n\n"
        f"## Related pages\n"
        f"{related}\n\n"
        f"## Source packs\n"
        f"{source_pack_links}\n"
    )
    return render_page(spec["title"], spec["tags"], spec["sources"], body)


def render_overview() -> str:
    raw_sources = render_raw_source_list(OVERVIEW["sources"], OVERVIEW["path"])
    source_detail = render_source_digests(OVERVIEW["sources"], OVERVIEW["path"], level=3)
    body = f"""
    {OVERVIEW['summary']}

    ## Start here
    - {wiki_link('domains/runtime-execution-stack.md')} - follow the request path from identity to case, data, and integration execution.
    - {wiki_link('domains/authoring-and-model-calculation.md')} - follow design-time authoring through rule storage, compilation, and bundle production.
    - {wiki_link('domains/security-domain-services.md')} - see how UAS, OprMS, OAZ, and key-management responsibilities divide cleanly.
    - {wiki_link('domains/launchpad-units-observability.md')} - trace Launchpad Units from raw metrics through ETL, dashboards, preview, and environment mapping.

    ## How this wiki is organized
    - **Service pages** capture the stable role, neighbors, and evidence trail for each major service family.
    - **Topic pages** capture Launchpad Units and KnowledgeHub operational material that does not fit neatly into a single service boundary.
    - **Concept pages** capture recurring patterns such as model-driven execution, bundle layering, and security-domain handoffs.
    - **Source packs** preserve traceability back to the raw corpus, including duplicate KnowledgeHub exports consolidated into canonical pages.

    ## Raw sources
    {raw_sources}

    ## Source-derived detail
    {source_detail}

    ## Chronicle
    - {wiki_link('log.md')} - append-only record of setup, ingest, and lint passes.
    """
    return render_page(OVERVIEW["title"], OVERVIEW["tags"], OVERVIEW["sources"], body)


def render_source_pack(spec: dict, source_paths: list[Path]) -> str:
    raw_refs = [str(path.relative_to(REPO)) for path in source_paths]
    evidence = render_raw_source_list(raw_refs, spec["path"])
    duplicate_note = "yes" if spec["kind"] == "norm-title" and len(source_paths) > 1 else "no"
    related = bullet_links(spec["related"])
    detailed_notes = render_source_digests(raw_refs, spec["path"], level=3)
    body = (
        f"{spec['summary']}\n\n"
        f"## Coverage\n"
        f"- Raw files covered: **{len(source_paths)}**\n"
        f"- Main themes: {spec['themes']}\n"
        f"- Duplicate exports consolidated here: **{duplicate_note}**\n\n"
        f"## Evidence map\n"
        f"{evidence}\n\n"
        f"## Detailed source notes\n"
        f"{detailed_notes}\n\n"
        f"## Related pages\n"
        f"{related}\n"
    )
    return render_page(spec["title"], spec["tags"], raw_refs, body)


PAGE_SUMMARIES: dict[str, str] = {}


def register_summary(path: str, summary: str) -> None:
    PAGE_SUMMARIES[path] = summary


def build_index() -> str:
    sections = [
        (
            "Overviews",
            [
                "overview.md",
            ],
        ),
        (
            "Domains",
            [item["path"] for item in DOMAIN_PAGES],
        ),
        (
            "Concepts",
            [item["path"] for item in CONCEPT_PAGES],
        ),
        (
            "Services",
            [item["path"] for item in SERVICE_PAGES],
        ),
        (
            "Topics",
            [item["path"] for item in TOPIC_PAGES],
        ),
        (
            "Sources",
            [item["path"] for item in sorted(SOURCE_PACKS, key=lambda item: item["title"].lower())],
        ),
        (
            "Operations",
            ["log.md"],
        ),
    ]

    parts = [
        "This index is the main entry point for the generated wiki. Start with the overview or the domain pages, then drill into services, topics, or source packs as needed.",
    ]

    for title, paths in sections:
        parts.append(f"\n## {title}")
        for path in paths:
            parts.append(f"- {wiki_link(path)} - {PAGE_SUMMARIES[path]}")

    body = "\n".join(parts)
    return render_page(
        "Wiki index",
        ["index", "navigation"],
        [],
        body,
    )


def load_history_entries() -> list[dict]:
    data = read_json_file(HISTORY_PATH, [])
    if not isinstance(data, list):
        return []

    entries = []
    for raw_entry in data:
        if not isinstance(raw_entry, dict):
            continue
        date_value = normalize_text(str(raw_entry.get("date", TODAY))) or TODAY
        operation = normalize_text(str(raw_entry.get("operation", "update"))) or "update"
        subject = normalize_text(str(raw_entry.get("subject", "wiki tool"))) or "wiki tool"
        details = str(raw_entry.get("details", "")).strip()
        entries.append(
            {
                "date": date_value,
                "operation": operation,
                "subject": subject,
                "details": details,
            }
        )
    return entries


def render_history_entries(entries: list[dict]) -> str:
    blocks = []
    for entry in entries:
        header = f"## [{entry['date']}] {entry['operation']} | {entry['subject']}"
        if entry["details"]:
            blocks.append(f"{header}\n{entry['details']}")
        else:
            blocks.append(header)
    return "\n\n".join(blocks)


def build_log() -> str:
    body = textwrap.dedent(
        f"""
        Append-only record of setup, ingest, and maintenance actions for the generated wiki.

        ## [{TODAY}] setup | llm-wiki bootstrap
        Pages created/updated: {wiki_link('overview.md')}, {wiki_link('index.md')}, {wiki_link('log.md')}, domain pages, concept pages, service pages, topic pages, and canonical source-pack pages.

        Notes:
        - Generated wiki content lives under `llm-wiki/` instead of the existing `wiki/` directory.
        - Root `CLAUDE.md` was created to document the schema and workflow for future sessions.
        - Duplicate KnowledgeHub exports were consolidated into canonical source-pack pages without touching `raw/`.

        ## [{TODAY}] ingest | service corpus
        Pages created/updated: {wiki_link('domains/runtime-execution-stack.md')}, {wiki_link('domains/authoring-and-model-calculation.md')}, {wiki_link('services/unified-authentication-service.md')}, {wiki_link('services/rulebase-service.md')}, {wiki_link('services/model-producer-service.md')} and related source packs.

        Sources covered:
        - Runtime services: `case`, `data`, `igw`
        - Security/runtime identity services: `UAS`, `oprms`, `oaz`
        - Authoring/model services: `core-auth`, `mcs-*`, `mps`, `rulebase`

        ## [{TODAY}] ingest | Launchpad Units and KnowledgeHub corpus
        Pages created/updated: {wiki_link('domains/launchpad-units-observability.md')}, {wiki_link('topics/launchpad-units-overview.md')}, {wiki_link('topics/launchpad-units-pipeline-architecture.md')}, {wiki_link('topics/usage-metadata.md')} and the canonical Launchpad topic source packs.

        Sources covered:
        - Launchpad Units overview, architecture, transformer-lambda design, usage metadata
        - Dashboards, preview, Infinity environments, provider dashboard implementation
        - Supporting diagram and note artifacts

        ## [{TODAY}] ingest | UAS KnowledgeHub architecture docs
        Pages created/updated: {wiki_link('services/unified-authentication-service.md')}, {wiki_link('domains/security-domain-services.md')}, {wiki_link('sources/uas-source-pack.md')}.

        Sources covered:
        - `UNIFAUTH:Home`
        - `UNIFAUTH:Architecture`
        - `UNIFAUTH:Apis`
        - `UNIFAUTH:Security`
        - `UNIFAUTH:Service_Reliability_Questionaire`
        - `UNIFAUTH:Manual-provisioning-of-security-configurations-for-user-login`

        ## [{TODAY}] ingest | experimental new source clusters
        Pages created/updated: {wiki_link('services/unified-authentication-service.md')}, {wiki_link('services/operator-management-service.md')}, {wiki_link('services/outbound-authorization-service.md')}, {wiki_link('topics/gateway-service-architecture.md')}, {wiki_link('concepts/mcp-authentication-and-authorization.md')}, updated source packs for UAS, OprMS, OAZ, Gateway Service, and MCP security guidance.

        Sources covered:
        - New Launchpad auth configuration docs in `raw/UAS/`
        - New OprMS architecture and access-resolution docs in `raw/oprms/`
        - New OAZ KnowledgeHub note in `raw/oaz/`
        - New Gateway Service architecture notes in `raw/Gateway/`
        - Standalone MCP OAuth/MCP security guide in `raw/MCP authentication and authorization implementation guide.md`

        ## [{TODAY}] lint | initial health check
        Issues found: duplicate raw exports for the Launchpad KnowledgeHub topics; sparse diagram-only notes with limited prose context.

        Fixed:
        - Consolidated duplicate source exports into canonical source-pack pages.
        - Linked every durable page from the index and connected domains, services, topics, and concepts with wikilinks.
        - Standardized frontmatter across all generated pages.
        - Preserved richer source-derived detail inside generated pages and added explicit markdown links back to the underlying `raw/` files.

        Suggestions:
        - Add filed query-answer pages when recurring questions produce reusable synthesis.
        - Ingest new raw sources incrementally rather than replacing the whole wiki.
        """
    ).strip()

    history_entries = load_history_entries()
    if history_entries:
        body += "\n\n" + render_history_entries(history_entries)
    return render_page("Wiki log", ["log", "operations"], [], body)


def build_claude() -> str:
    return textwrap.dedent(
        f"""
        # CLAUDE.md

        This repository uses an LLM-maintained markdown wiki rooted at `llm-wiki/`.

        ## Directory layout

        ```
        {REPO}/
          raw/        # immutable source corpus; never edit in-place
          llm-wiki/   # generated wiki pages; the LLM owns this tree
          wiki/       # pre-existing directory; leave untouched unless explicitly asked
          CLAUDE.md   # schema and workflow rules for future sessions
        ```

        ## Domain context

        The source corpus is centered on Pega Launchpad / InfinityX architecture. It contains:

        - runtime service documentation (`case`, `data`, `igw`, `UAS`, `oprms`, `oaz`)
        - design-time and model-calculation service documentation (`core-auth`, `mcs-*`, `mps`, `rulebase`)
        - comparative security-domain notes
        - Launchpad Units / KnowledgeHub operational notes, dashboards, diagrams, and ETL design material

        ## Page conventions

        Every generated page must start with YAML frontmatter:

        ```yaml
        ---
        title: "Page Title"
        tags: ["tag1", "tag2"]
        sources:
          - "raw/path.md"
        updated: {TODAY}
        ---
        ```

        Use Obsidian wikilinks with the `llm-wiki/` prefix, for example:

        - `[[llm-wiki/services/unified-authentication-service]]`
        - `[[llm-wiki/domains/runtime-execution-stack]]`

        Filenames must be kebab-case. Prefer these categories:

        - `llm-wiki/overview.md`
        - `llm-wiki/domains/*.md`
        - `llm-wiki/concepts/*.md`
        - `llm-wiki/services/*.md`
        - `llm-wiki/topics/*.md`
        - `llm-wiki/sources/*.md`
        - `llm-wiki/index.md`
        - `llm-wiki/log.md`

        ## Canonicalization rules

        - Treat `raw/` as immutable.
        - When the raw corpus contains duplicate KnowledgeHub exports of the same topic, consolidate them into a single canonical source-pack page under `llm-wiki/sources/`.
        - Keep source traceability by listing every contributing raw file in the page frontmatter and evidence section.
        - Prefer durable synthesis pages (domains, concepts, services, topics) over one-page-per-file duplication.

        ## Workflow: ingest

        1. Read new raw material.
        2. Decide whether it belongs to an existing service/topic/source-pack page or needs a new page.
        3. Update durable synthesis pages first, then the relevant source pack, then `llm-wiki/index.md`, then `llm-wiki/log.md`.
        4. Preserve cross-links between domains, services, topics, concepts, and sources.

        ## Workflow: query

        1. Read `llm-wiki/index.md` first.
        2. Follow the relevant domain/service/topic pages.
        3. Cite wiki pages, not raw files, in the response.
        4. If the answer is reusable, file it as a new page under `llm-wiki/topics/` or `llm-wiki/concepts/` and append a log entry.

        ## Workflow: lint

        Check for:

        - orphan pages not linked from other durable pages or the index
        - stale claims that should be updated as new raw sources arrive
        - missing cross-links between services that depend on each other
        - duplicate topic pages that should be merged
        - new raw source clusters that deserve canonical source-pack pages

        Always update `llm-wiki/log.md` after ingest, query filing, or lint work.
        """
    ).strip() + "\n"


def main() -> None:
    refresh_dynamic_collections()
    PAGE_SUMMARIES.clear()
    write(REPO / "CLAUDE.md", build_claude())

    write(WIKI / OVERVIEW["path"], render_overview())
    register_summary(OVERVIEW["path"], OVERVIEW["summary"])

    for spec in DOMAIN_PAGES:
        write(WIKI / spec["path"], render_generic_page(spec))
        register_summary(spec["path"], spec["summary"])

    for spec in CONCEPT_PAGES:
        write(WIKI / spec["path"], render_generic_page(spec))
        register_summary(spec["path"], spec["summary"])

    for spec in SERVICE_PAGES:
        write(WIKI / spec["path"], render_service_page(spec))
        register_summary(spec["path"], spec["summary"])

    for spec in TOPIC_PAGES:
        write(WIKI / spec["path"], render_generic_page(spec))
        register_summary(spec["path"], spec["summary"])

    for spec in SOURCE_PACKS:
        sources = collect_sources(spec)
        write(WIKI / spec["path"], render_source_pack(spec, sources))
        register_summary(spec["path"], spec["summary"])

    register_summary("log.md", "Append-only record of setup, ingest, and lint activity for the generated wiki.")
    write(WIKI / "log.md", build_log())
    write(WIKI / "index.md", build_index())
    page_count = len(list(WIKI.rglob("*.md")))
    print(f"Compiled wiki written to {WIKI} ({page_count} markdown files)", flush=True)


refresh_dynamic_collections()


if __name__ == "__main__":
    main()
