"""Authenticated remote provenance index in a private GitHub repository.

After evidence is saved locally, its metadata (NEVER the artifact bytes) is
written as one JSON file, records/<evidence_id>.json, using the GitHub REST
API "Create or update file contents" endpoint:
https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents

Authentication: a fine-grained personal access token read from .env and sent
as "Authorization: Bearer <token>", as described in
https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api
The token only needs access to the one evidence-index repository, with the
repository permission "Contents: write".

Records are write-once: we never send a `sha`, so GitHub refuses to overwrite
an existing record, and we report REMOTE_RECORD_CONFLICT.

The token is never logged, never returned to the MCP client, and never sent
anywhere except api.github.com.
"""

import base64
import json
import logging
from typing import Any

import httpx

from evidence_mcp import USER_AGENT
from evidence_mcp.config import Settings
from evidence_mcp.errors import (
    REMOTE_AUTH_FAILED,
    REMOTE_RECORD_CONFLICT,
    REMOTE_SYNC_FAILED,
    EvidenceError,
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
# Latest REST API version listed at https://docs.github.com/en/rest/about-the-rest-api/api-versions
API_VERSION = "2026-03-10"
RECORDS_DIR = "records"


def put_record(
    record: dict[str, Any], settings: Settings, transport: httpx.BaseTransport | None = None
) -> str:
    """Create records/<evidence_id>.json in the private index repo and return that path.

    Raises EvidenceError with a REMOTE_* code if GitHub refuses or is unreachable.
    """
    evidence_id = record["evidence_id"]
    path = f"{RECORDS_DIR}/{evidence_id}.json"
    url = f"{GITHUB_API}/repos/{settings.github_owner}/{settings.github_repo}/contents/{path}"
    payload = json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8")
    body = {
        "message": f"Add provenance record {evidence_id}",
        "content": base64.b64encode(payload).decode("ascii"),
        "branch": settings.github_branch,
        # No "sha" field: without it GitHub will not overwrite an existing
        # file, which keeps every remote record write-once.
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,  # GitHub rejects API requests without a User-Agent
    }

    try:
        # follow_redirects=False: the token must never be forwarded to another host.
        # trust_env=False: ignore proxy environment variables and ~/.netrc.
        with httpx.Client(
            transport=transport,
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.put(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        # Log the exception only; request headers (and so the token) are never logged.
        logger.warning("GitHub sync of %s failed: %r", evidence_id, exc)
        raise EvidenceError(
            REMOTE_SYNC_FAILED, "The provenance record could not be synced to GitHub."
        ) from None

    if response.status_code in (200, 201):
        logger.info("Synced %s to the GitHub index as %s", evidence_id, path)
        return path

    message = _github_message(response)
    logger.warning("GitHub refused %s: HTTP %s %s", evidence_id, response.status_code, message)
    if response.status_code in (401, 403, 404):
        # 401: missing, wrong or expired token. 403: token lacks permission.
        # 404: GitHub hides private repositories a token cannot see, so "no
        # access" and "wrong owner/repo name" look the same.
        raise EvidenceError(
            REMOTE_AUTH_FAILED,
            "GitHub rejected the credentials or denied access to the evidence-index repository.",
        )
    # "Record already exists". 409 is GitHub's documented conflict status, but
    # for a file that exists when no sha is sent GitHub has been reported to
    # answer 422 'Invalid request. "sha" wasn't supplied.' (for example
    # https://community.n8n.io/t/error-github-error-response-422-invalid-request-sha-wasnt-supplied/5298),
    # so that specific 422 is treated as a conflict too.
    if response.status_code == 409 or (response.status_code == 422 and "sha" in message):
        raise EvidenceError(
            REMOTE_RECORD_CONFLICT,
            "A remote record for this evidence ID already exists and was not overwritten.",
        )
    raise EvidenceError(REMOTE_SYNC_FAILED, "The provenance record could not be synced to GitHub.")


def _github_message(response: httpx.Response) -> str:
    """GitHub's short error message, for the local log only."""
    try:
        return str(response.json().get("message", ""))[:200]
    except ValueError:
        return ""
