"""The MCP server: one tool, two read-only resources and one prompt, over stdio.

API usage follows the official MCP Python SDK v2 documentation. In v2 the
high-level server class formerly called FastMCP is named MCPServer
(https://py.sdk.modelcontextprotocol.io/migration/).
  tools:          https://py.sdk.modelcontextprotocol.io/servers/tools/
  resources:      https://py.sdk.modelcontextprotocol.io/servers/resources/
  prompts:        https://py.sdk.modelcontextprotocol.io/servers/prompts/
  error handling: https://py.sdk.modelcontextprotocol.io/servers/handling-errors/

This module is also the error-disclosure boundary: nothing leaves it except
our own fixed error codes and messages.
"""

import logging
import sys
from functools import lru_cache
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from evidence_mcp import archive, storage
from evidence_mcp.config import Settings, load_settings
from evidence_mcp.errors import EVIDENCE_NOT_FOUND, INTERNAL_ERROR, STORAGE_ERROR, EvidenceError

logger = logging.getLogger(__name__)

RECENT_LIMIT = 20

mcp = MCPServer(
    "evidence-mcp",
    instructions=(
        "Archives one public web source as durable research evidence with provenance. "
        "Call archive_source only when a specific source should be preserved."
    ),
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


# --- Tool: the ONLY operation with side effects ------------------------------------


@mcp.tool(
    title="Archive a public web source as evidence",
    annotations=ToolAnnotations(
        read_only_hint=False,  # it writes files and a remote record
        destructive_hint=False,  # it never deletes or overwrites evidence
        idempotent_hint=True,  # same bytes -> same evidence ID, no duplicate copy
        open_world_hint=True,  # it talks to the public web and GitHub
    ),
)
def archive_source(
    url: Annotated[
        str, Field(max_length=2048, description="Public http(s) URL of ONE source to preserve.")
    ],
    note: Annotated[
        str, Field(max_length=2000, description="Optional: why this source matters.")
    ] = "",
) -> archive.ArchiveResult:
    """Archive ONE public http(s) web source as durable research evidence.

    Downloads the original bytes, stores them locally with SHA-256 and
    provenance metadata, and records the metadata in the private GitHub
    evidence index. Returns the evidence ID. It does not search, summarize,
    or cite anything.
    """
    try:
        return archive.archive_source(url, note, get_settings())
    except EvidenceError as exc:
        # Expected failure: the code and message were written by us, safe to show.
        logger.info("archive_source failed: %s", exc.code)
        raise ToolError(str(exc)) from None
    except Exception:
        # Unexpected failure: the traceback goes to the local log (stderr) only;
        # the client receives a fixed, generic message.
        logger.exception("Unexpected error in archive_source")
        raise ToolError(
            f"{INTERNAL_ERROR}: The source could not be archived because of an internal error."
        ) from None


# --- Resources: read existing state, never change it ------------------------------------


@mcp.resource(
    "evidence://recent",
    name="recent_evidence",
    title="Recently archived evidence",
    mime_type="application/json",
)
def recent_evidence() -> dict[str, Any]:
    """Provenance metadata of the most recently archived evidence, newest first."""
    try:
        records = storage.list_recent(get_settings().evidence_root, limit=RECENT_LIMIT)
    except Exception:
        logger.exception("Could not list evidence")
        raise ResourceError(f"{STORAGE_ERROR}: The local evidence store could not be read.") from None
    return {"count": len(records), "items": records}


@mcp.resource(
    "evidence://artifact/{evidence_id}",
    name="evidence_metadata",
    title="Metadata for one evidence item",
    mime_type="application/json",
)
def evidence_metadata(evidence_id: str) -> dict[str, Any]:
    """Provenance metadata for one archived evidence item."""
    try:
        record = storage.load_metadata(get_settings().evidence_root, evidence_id)
    except Exception:
        logger.exception("Could not read evidence metadata")
        raise ResourceError(f"{STORAGE_ERROR}: The local evidence store could not be read.") from None
    if record is None:
        raise ResourceNotFoundError(f"{EVIDENCE_NOT_FOUND}: No archived evidence has this ID.")
    return record


# --- Prompt: a reusable instruction template the user can pick ----------------------------


@mcp.prompt(name="archive_research_evidence", title="Archive research evidence")
def archive_research_evidence(
    source_url: Annotated[str, Field(description="The source to consider (optional).")] = "",
    research_question: Annotated[str, Field(description="What the research is about (optional).")] = "",
) -> str:
    """Decide whether a source is worth preserving and, if so, archive it."""
    source = source_url.strip() or "the source we are currently discussing"
    question = research_question.strip() or "the current research topic"
    return f"""You are helping a solo researcher preserve public web evidence.

Source: {source}
Research question: {question}

1. Decide whether this source is worth preserving as evidence for the research
   question. If it is not, say so briefly and stop.
2. Prefer the primary or original source (for example the official PDF, dataset,
   or publisher page) over summaries, reposts, or search-result pages.
3. Archive only this one relevant source: call the `archive_source` tool once
   with its URL and a one-sentence note on why it matters.
4. Report the `evidence_id` and `sha256` returned by the tool.
5. Describe the outcome accurately:
   - status "archived": saved locally and recorded in the remote provenance index;
   - status "archived_with_sync_warning": saved locally, but the remote record
     was not written; mention the error_code;
   - a tool error (for example UNSAFE_URL or FILE_TOO_LARGE): nothing was archived.
   Never claim a source was archived unless the tool call succeeded."""


def main() -> None:
    """Entry point for `uv run evidence-mcp`: serve MCP over stdio."""
    # stdout carries the MCP protocol messages, so all logging goes to stderr
    # (guidance from https://modelcontextprotocol.io/docs/develop/build-server).
    # force=True replaces any handler a library may already have installed.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    settings = get_settings()
    logger.info(
        "GitHub evidence index: %s",
        f"{settings.github_owner}/{settings.github_repo}" if settings.github_configured else "not configured",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
