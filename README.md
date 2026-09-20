# Evidence MCP

> **One MCP tool turns one transient public web source into one durable local
> evidence artifact and one authenticated remote provenance record.**

A deliberately small Python MCP server for a solo researcher who does web
research with an AI assistant (Claude Desktop). When the assistant finds a
source worth keeping, it calls **one tool**, `archive_source(url, note)`,
which:

- downloads the original bytes safely;
- stores them locally with a SHA-256 hash and provenance metadata;
- writes that metadata as one JSON record to a **private GitHub repository**.

---

## 1. The problem

Useful public evidence found during AI-assisted research is easy to lose (pages
change or disappear), to rename inconsistently, or to separate from its
provenance (where it came from, and when). This server provides **one safe,
predictable action**: archive this one source and record where it came from.

It does *not* search, rank, summarize, cite, or crawl. The LLM decides *whether*
a source is worth keeping; this server makes keeping it deterministic.

## 2. Why an MCP server, not only a script or a skill

A CLI script is the right design when the human is the natural caller. Here
the natural caller is the AI assistant: it's already reading the source, has
the URL, and knows why it matters. A script would turn the human into a
copy-paste courier between the chat and a terminal.

A Claude skill could tell the model to "preserve sources carefully", but that's
guidance, not a guarantee. Nothing ensures the bytes were really downloaded,
hashed and stored, and a skill can't hold a credential safely.

An MCP server gives the assistant a typed capability to call. The procedure is
deterministic code in a separate local process that owns the file system access
and the GitHub credential; the model only decides whether to call it and with
which URL.

## 3. Architecture

Claude Desktop is the MCP host: it runs the LLM. Inside it, an MCP client
starts this server as a local subprocess and talks to it with JSON-RPC over
stdin/stdout (stdio). The server (`evidence_mcp`) exposes:
- the tool `archive_source`, the only operation with side effects;
- two read-only resources, `evidence://recent` and
  `evidence://artifact/{evidence_id}`;
- the prompt `archive_research_evidence`.

When the tool is called, the server:
1. validates the URL (SSRF checks);
2. downloads it with httpx under timeouts, a redirect limit and a size cap;
3. hashes it with SHA-256;
4. saves it atomically to the local evidence store, which is the canonical copy;
5. writes the provenance metadata (never the file) to a private GitHub
   repository, and records the outcome of that sync locally.

So the side effects are an outbound GET to the public web, local file writes,
and an authenticated PUT to GitHub. State lives outside the LLM, in
`evidence_store/` and the GitHub index repo. The trust boundary is the server
process: the URL and note from the model, and everything returned by the web,
are treated as untrusted.

**Credential boundary.** The GitHub token lives only in `.env` (gitignored). The
server process loads it and sends it only to `api.github.com`. Claude Desktop
and the LLM never receive it: it isn't in tool schemas, results, resources,
prompts, logs or `claude_desktop_config.json`.

### Code layout

```text
evidence-mcp/
├── src/evidence_mcp/
│   ├── server.py        MCP surface: tool, resources, prompt; error-disclosure boundary
│   ├── archive.py       the archive_source procedure (download -> hash -> store -> sync)
│   ├── security.py      SSRF checks (URL validation)
│   ├── storage.py       local evidence store (atomic writes, safe file names)
│   ├── github_index.py  authenticated GitHub provenance record
│   ├── config.py        settings from .env
│   └── errors.py        safe error codes
├── tests/               pytest suite (offline: fake web, fake DNS, fake GitHub)
├── demo_exploits.py     live exploit demonstration
├── claude_desktop_config.example.json
├── .env.example
├── pyproject.toml / uv.lock / requirements.txt
└── README.md
```

## 4. MCP tool, resources and prompt

### Tool: `archive_source(url: str, note: str = "") -> dict`

Archives **one** public http(s) source. Steps: validate the URL, download
(re-validating redirects), enforce the size limit, hash with SHA-256, save the
artifact and `metadata.json` atomically, sync the metadata to GitHub, record
the sync outcome locally.

Full success:

```json
{
  "status": "archived",
  "evidence_id": "ev_ff67a9d764d6a236",
  "sha256": "ff67a9d764d6a2367a187734e697f6a53217db9a21c101d410a113ca871a299d",
  "filename": "source.html",
  "content_type": "text/html",
  "size_bytes": 559,
  "retrieved_at": "2026-09-15T03:25:19+00:00",
  "original_url": "https://example.com/",
  "already_archived": false,
  "local_saved": true,
  "remote_metadata_synced": true,
  "error_code": null
}
```

Saved locally but the GitHub sync failed. This is **not** reported as full
success, and the local evidence is **not** deleted:

```json
{ "status": "archived_with_sync_warning", "local_saved": true,
  "remote_metadata_synced": false, "error_code": "REMOTE_AUTH_FAILED", "...": "..." }
```

If identical bytes were archived before, the existing record is returned
(`already_archived: true`) and no duplicate copy is written.

### Resources (read-only)

| URI | Returns |
|---|---|
| `evidence://recent` | `{"count": n, "items": [metadata, ...]}`, newest first (max 20) |
| `evidence://artifact/{evidence_id}` | one metadata record, or `EVIDENCE_NOT_FOUND` |

Metadata fields: `evidence_id`, `original_url`, `final_url`, `retrieved_at`,
`sha256`, `filename`, `original_filename`, `content_type`, `size_bytes`,
`note`, `remote_sync` (`status`, `error_code`, `remote_path`, `attempted_at`).

### Prompt: `archive_research_evidence(source_url="", research_question="")`

The prompt tells the assistant to:
- decide whether the source is worth preserving;
- prefer primary sources;
- archive only that one source with `archive_source`;
- report the `evidence_id`;
- never claim success unless the tool succeeded.

### Error codes (the only error text the client ever sees)

| Code | Meaning |
|---|---|
| `UNSAFE_URL` | Blocked by the SSRF checks; no request was sent |
| `DOWNLOAD_FAILED` | Network error, timeout, HTTP 4xx/5xx, too many redirects |
| `FILE_TOO_LARGE` | Over `MAX_FILE_SIZE_MB`; the download was stopped and nothing was saved |
| `UNSUPPORTED_RESPONSE` | Empty body, a non-200 success status, a redirect without a target |
| `STORAGE_ERROR` | The local disk write/read failed; nothing half-written is left as evidence |
| `INTERNAL_ERROR` | An unexpected bug; details are in the local log only |
| `EVIDENCE_NOT_FOUND` | Resource read for an unknown ID |
| `REMOTE_AUTH_FAILED` | GitHub answered 401/403/404 (bad token, missing permission, or no repo access) |
| `REMOTE_RECORD_CONFLICT` | A remote record for this ID already exists; it isn't overwritten |
| `REMOTE_SYNC_FAILED` | GitHub unreachable or other GitHub error |
| `REMOTE_NOT_CONFIGURED` | The GitHub variables aren't set |

## 5. Setup with uv

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.11+ (uv can
install Python for you).

```bash
git clone <your-assignment-repo-url> evidence-mcp
cd evidence-mcp
uv sync
```

`uv sync` creates `.venv/` and installs the exact versions from `uv.lock`.

Without uv, `requirements.txt` works with plain pip. It installs the
dependencies, pytest, and the project itself in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 6. Configuration (`.env`)

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|---|---|---|
| `EVIDENCE_ROOT` | `./evidence_store` | Local evidence folder (relative to where the server starts) |
| `MAX_FILE_SIZE_MB` | `20` | Largest download allowed |
| `HTTP_TIMEOUT_SECONDS` | `10` | Timeout for each connect/read operation |
| `MAX_REDIRECTS` | `3` | Redirects followed (each one re-validated) |
| `GITHUB_TOKEN` | *(empty)* | Fine-grained personal access token (**secret**) |
| `GITHUB_OWNER` | *(empty)* | Owner of the evidence-index repo |
| `GITHUB_REPO` | *(empty)* | Name of the evidence-index repo |
| `GITHUB_BRANCH` | `main` | Branch to write records to |

`.env` is in `.gitignore`. Only `.env.example`, with empty values, is committed.

### Authentication and the private GitHub index

The public sources are fetched **without any credentials**. The one
authenticated external service is the private GitHub **provenance index**. It
receives metadata only, never the archived files.

1. On GitHub, create a **private** repository, e.g. `private-evidence-index`.
   Tick *Add a README file* so that the `main` branch exists.
2. Create a **fine-grained personal access token**: *Settings → Developer
   settings → Personal access tokens → Fine-grained tokens → Generate new token*.
   - Expiration: short (e.g. 30 days).
   - Repository access: **Only select repositories →** `private-evidence-index`.
   - Repository permissions: **Contents → Read and write**. GitHub's permission
     table lists `Contents: write` as what `PUT /repos/{owner}/{repo}/contents/{path}` needs.
3. Paste the values into **`.env`** (never into `.env.example`, code, the README
   or the Claude Desktop config):

   ```text
   GITHUB_TOKEN=<paste the token here, in .env only>
   GITHUB_OWNER=<your GitHub username>
   GITHUB_REPO=private-evidence-index
   GITHUB_BRANCH=main
   ```

Each archived item becomes one file, `records/ev_<id>.json`, in that repository.
If the GitHub variables are left empty, the server still works locally and
reports `REMOTE_NOT_CONFIGURED`.

**Two separate repositories:** this assignment code repo (shared with
instructors) and the private evidence-index repo (runtime records only). No
generated evidence ever goes into the code repo: `evidence_store/` is gitignored.

## 7. Running locally

```bash
uv run evidence-mcp
```

The server speaks MCP over stdin/stdout, so it will sit waiting for a client;
that's normal (stop it with Ctrl+C). Logs go to stderr. To see it do something
without Claude Desktop, run the demo (section 11) or the tests (section 13).

## 8. Configuring Claude Desktop

1. Find the absolute path of `uv` and of this project:

   ```bash
   which uv
   ```

   ```bash
   pwd
   ```

2. Open the config file. In Claude Desktop, open the **Claude** menu (the menu
   bar, not the chat window) → **Settings… → Developer → Edit Config**. That
   opens:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
3. Paste this (also in
   [claude_desktop_config.example.json](claude_desktop_config.example.json)),
   replacing the two placeholders with the paths from step 1. If the file
   already has other servers, add `"evidence"` inside the existing
   `"mcpServers"` object.

   ```json
   {
     "mcpServers": {
       "evidence": {
         "command": "/ABSOLUTE/PATH/TO/uv",
         "args": [
           "--directory",
           "/ABSOLUTE/PATH/TO/evidence-mcp",
           "run",
           "evidence-mcp"
         ]
       }
     }
   }
   ```

   - `--directory` makes uv start in the project folder, so the server finds
     `.env` and `./evidence_store` there.
   - There's no `env` block: the token stays in `.env` and never appears in
     this file.
4. **Fully quit** Claude Desktop (Cmd+Q on macOS) and reopen it.
5. Check: click the **+** button in the chat box → **Connectors**. `evidence`
   should be listed, with the `archive_source` tool.
6. If it doesn't appear, check the logs: the server's stderr is in
   `~/Library/Logs/Claude/mcp-server-evidence.log` (macOS) or
   `%APPDATA%\Claude\logs` (Windows).

## 9. Example usage

In Claude Desktop:

> I'm researching transformer architectures. Please archive
> https://arxiv.org/pdf/1706.03762 as evidence. It's the original
> "Attention Is All You Need" paper.

Claude asks for approval to call `archive_source`, then reports something like:

> Archived. Evidence ID `ev_…`, SHA-256 `…`, saved locally as `1706.03762v7.pdf`
> and recorded in the private evidence index.

(arXiv redirects to the latest version, so the saved name comes from the final
URL and `metadata.json` records both `original_url` and `final_url`.)

- **The prompt:** choose `archive_research_evidence` from the evidence server
  (under the **+** menu) and fill in `source_url` / `research_question`.
- **The resources:** attach "Recently archived evidence" (`evidence://recent`)
  to let Claude see what's already been saved.
- **On disk:** `evidence_store/artifacts/ev_…/` holds the original file and
  `metadata.json`.
- **On GitHub:** `records/ev_….json` appears in the private index repo.

## 10. Security design

The two security best practices claimed for the rubric are **(1) input
sanitization / SSRF protection** and **(2) safe error disclosure**.

### 10.1 Input sanitization: SSRF protection

The URL comes from the model, so it's **untrusted**. The model may have been
prompt-injected by a page it just read. The server runs inside your network,
so without checks it could be made to fetch `localhost`, your router, or cloud
metadata endpoints.

| Check | What it blocks | Where |
|---|---|---|
| Only `http`/`https` | `file:///etc/passwd`, `ftp://`, `gopher://`, `javascript:` | `security.validate_url` |
| No embedded credentials | `http://user:pass@host/`, `http://evil.com\@127.0.0.1/` | `security.validate_url` |
| URL parsed by **httpx itself** | parser mismatches between checker and client | `security.validate_url` |
| Resolve the hostname; **every** IP must be public | `localhost`, `127.0.0.1`, `10.x`, `172.16.x`, `192.168.x`, `169.254.169.254`, `::1`, `::ffff:127.0.0.1`, `0.0.0.0`, `100.64.x`, `http://2130706433/` | `security.resolve_host`, `security.is_public_ip` |
| Redirects followed **manually**, each target re-validated | a public page that redirects to `169.254.169.254` | `archive.fetch` |
| Redirect limit | redirect loops | `archive.fetch` (`MAX_REDIRECTS`) |
| Timeouts | hanging servers | httpx `timeout` (`HTTP_TIMEOUT_SECONDS`) |
| Size cap: `Content-Length` check **and** a streamed byte count | huge files, missing or lying headers | `archive._read_body` (`MAX_FILE_SIZE_MB`) |
| Input length limits | oversized URL (2048) / note (2000) | tool schema (`Field(max_length=…)`) |
| `trust_env=False` | proxy variables / `~/.netrc` credentials being used | `archive.fetch`, `github_index.put_record` |
| Safe file names, strict evidence-ID pattern | `../../` path traversal via headers or resource URIs | `storage.safe_filename`, `storage.is_valid_evidence_id` |

### 10.2 Error disclosure

- Every expected failure is an `EvidenceError(code, message)`. The **message is
  a fixed sentence in our code**, never text copied from an exception.
- `server.py` is the boundary. `EvidenceError` becomes an MCP tool error with
  that safe text. **Any other exception** becomes the generic
  `INTERNAL_ERROR: The source could not be archived because of an internal error.`
- The traceback goes to the **local log** (stderr, which Claude Desktop writes
  to its MCP log files), never to the client.
- GitHub responses are never passed through: only the `REMOTE_*` codes. The
  token and `Authorization` header are never logged, and `Settings` hides the
  token from `repr()`.
- A second layer: the MCP SDK itself hides the text of unexpected exceptions.
  We still sanitize explicitly rather than rely on that.

### 10.3 Other guardrails (not claimed as the two rubric practices)

- **Failure-safe storage:** each evidence folder is built in `tmp/` and moved
  into `artifacts/` with one atomic rename, so a folder that exists is complete.
- **Secret management:** see section 6 and the credential boundary.

## 11. Exploit demonstrations

```bash
uv run python demo_exploits.py
```

It runs offline, uses a throwaway evidence folder, never contacts GitHub, and
talks to the real server through a real MCP client.

**Exploit A (SSRF):**
- The demo starts a fake private service on `http://127.0.0.1:8000/private`
  (or a free port if 8000 is busy) and shows that it *does* hand out a secret
  when fetched directly.
- Then it asks `archive_source` to archive it, plus `localhost`, the decimal-IP
  trick, IPv4-mapped IPv6, `file:///etc/passwd`, `169.254.169.254`,
  `192.168.1.1` and a URL with credentials.

Excerpt of the real output:

```text
A private service is running at http://127.0.0.1:8000/private
Fetched directly (not through MCP), it returns: 'INTERNAL_ADMIN_TOKEN=do-not-leak'

  BLOCKED      http://127.0.0.1:8000/private
               -> Error executing tool archive_source: UNSAFE_URL: The URL points to a local, private, or reserved network address.
  BLOCKED      file:///etc/passwd
               -> Error executing tool archive_source: UNSAFE_URL: Only http:// and https:// URLs can be archived.
  BLOCKED      http://169.254.169.254/latest/meta-data/
  ...
Requests that reached the private service: 0
```

**Exploit B (error disclosure):** internal failures are forced whose exception
messages contain `/Users/example/private/evidence` and `SUPER_SECRET_TEST_VALUE`:

```text
Case 1: an unexpected crash inside the tool
  What the MCP client receives: Error executing tool archive_source: INTERNAL_ERROR: The source could not be archived because of an internal error.
    [PASS] absolute path hidden
    [PASS] secret hidden
    [PASS] no traceback
    [PASS] no raw exception message
```

The demo then prints the local log lines, which *do* contain the details.

The same exploits as automated tests:

```bash
uv run pytest tests/test_security.py tests/test_error_handling.py -v
```

Tests of note:
- `test_redirect_to_cloud_metadata_endpoint_is_blocked` covers the redirect
  attack, a case the offline demo can't show.
- `test_unsafe_url_is_refused_through_mcp_without_any_request` makes any
  network call fail the test.

## 12. Limitations (honest list)

- **DNS rebinding is not fully prevented.** We resolve the hostname and check
  the IPs, then httpx resolves it again when connecting. A hostile DNS server
  could answer differently the second time. Fully closing this means pinning
  the checked IP inside the connection, which is out of scope for v1.
- **Other SSRF edge cases:** IPv6 transition formats other than IPv4-mapped
  (e.g. NAT64, 6to4) aren't unwrapped. The timeouts apply per connect/read
  operation, so a very slow "drip" server isn't cut off by a total deadline;
  the size cap still bounds it.
- **Two systems, no transaction.** Local storage and GitHub can disagree. That
  is reported (`archived_with_sync_warning`) rather than prevented. There's no
  retry queue; calling `archive_source` again for the same content re-attempts
  only a sync that hasn't succeeded. Two *simultaneous* calls for identical
  content may report a spurious `REMOTE_RECORD_CONFLICT`.
- **Write-once remote records:** if you delete your local store and archive the
  same bytes again, GitHub already has the record, so you get
  `REMOTE_RECORD_CONFLICT`.
- **Raw responses only:** a web page is saved as the HTML the server returned,
  not a rendered snapshot with images and scripts.
- **Dedup is by exact bytes:** a different URL serving identical bytes returns
  the existing record, and the second URL isn't added to it.
- **Evidence IDs** use the first 64 bits of SHA-256; the full hash is always
  stored, and a (practically impossible) collision is refused.
- **Memory:** a download is held in memory, bounded by `MAX_FILE_SIZE_MB`.
- **Housekeeping:** a crash mid-write can leave a folder in
  `evidence_store/tmp/`. It's never treated as evidence and is safe to delete.
- **Local security:** the evidence store isn't encrypted. The `note` text is
  sent to the private GitHub repo.
- **Token lifecycle:** expiry and rotation of the personal access token are
  manual.

## 13. Tests

```bash
uv run pytest
```

All tests are offline. They use a fake web and a fake GitHub
(`httpx.MockTransport`) plus a fake DNS resolver, and never a real token or
repository.

| File | Covers |
|---|---|
| `tests/test_security.py` | SSRF rules: schemes, credentials, private/loopback/link-local/reserved IPs, DNS answers |
| `tests/test_archive.py` | download, redirects (incl. the redirect SSRF), size limit (declared, lying, streamed), dedup, file names, atomic storage |
| `tests/test_github_sync.py` | authenticated write, 401/403/404/409/422/5xx, network failure, token never in logs, partial-failure semantics |
| `tests/test_error_handling.py` | Exploit B through a real MCP client; sanitized tool and resource errors |
| `tests/test_server.py` | MCP surface: one tool, read-only resources, prompt, round trip |

## 14. Code citations and references

| Source | Used for | Where |
|---|---|---|
| MCP Python SDK v2 docs: [tools](https://py.sdk.modelcontextprotocol.io/servers/tools/), [resources](https://py.sdk.modelcontextprotocol.io/servers/resources/), [prompts](https://py.sdk.modelcontextprotocol.io/servers/prompts/), [errors](https://py.sdk.modelcontextprotocol.io/servers/handling-errors/), [migration (FastMCP → MCPServer)](https://py.sdk.modelcontextprotocol.io/migration/) | `MCPServer`, decorators, `ToolAnnotations`, `ToolError`/`ResourceError` | `server.py` |
| [MCP SDK testing docs](https://py.sdk.modelcontextprotocol.io/get-started/testing/) | in-memory `Client(server)` tests, `anyio_backend` fixture | `tests/conftest.py` |
| [MCP "Build an MCP server"](https://modelcontextprotocol.io/docs/develop/build-server) | stdio logging to stderr; Claude Desktop config with `uv --directory … run` | `server.py`, `claude_desktop_config.example.json` |
| [MCP "Connect to local MCP servers"](https://modelcontextprotocol.io/docs/develop/connect-local-servers) | config file location, restart, log locations | README section 8 |
| [GitHub REST: create or update file contents](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents) | the PUT request, base64 content, `sha` semantics | `github_index.py` |
| [GitHub REST: authentication](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api), [API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions), [fine-grained token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) | Bearer header, `X-GitHub-Api-Version: 2026-03-10`, Contents: write | `github_index.py`, section 6 |
| [n8n community report of 422 "sha wasn't supplied"](https://community.n8n.io/t/error-github-error-response-422-invalid-request-sha-wasnt-supplied/5298) | treating that 422 as a conflict | `github_index.py` |
| [httpx extensions](https://www.python-httpx.org/advanced/extensions/) / [transports](https://www.python-httpx.org/advanced/transports/#mock-transports) | `MockTransport` fake web and GitHub | `tests/helpers.py` |
| Python standard library: `ipaddress`, `socket.getaddrinfo`, `email.message` | IP classification, DNS resolution, `Content-Disposition` parsing | `security.py`, `archive.py` |
