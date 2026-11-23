# Security

Threat model, secrets management, and supply-chain considerations for Machina deployments.

## Threat Model

### Trust Boundaries

```
┌─────────────────────────────────────────────────────┐
│  Machina Process                                     │
│  ┌──────────┐  ┌───────────┐  ┌──────────────────┐ │
│  │ MCP      │  │ Connector │  │ DocumentStore    │ │
│  │ Transport│──│ Layer     │──│ (RAG)            │ │
│  └────┬─────┘  └─────┬─────┘  └────────┬─────────┘ │
└───────┼──────────────┼────────────────┼─────────────┘
        │              │                │
   MCP Client      CMMS / IoT     Ingested Documents
   (trusted?)      (trusted)      (untrusted content)
```

### Transport: stdio

**Trust level:** The MCP client is a local process on the same machine.

**Risk:** Any local process can connect. There is no authentication —
stdio mode grants full CMMS write access to whoever invokes the process.

!!! danger "Single-user environments only"
    stdio mode is designed for local development and single-user IDE integrations
    (Claude Desktop, Cursor). Do not use it in multi-user or server environments.
    Use streamable-http with bearer token auth instead.

### Transport: streamable-http

**Trust level:** The MCP client authenticates with a static bearer token.

**Risk profile:**

- **Token compromise** gives the holder every registered tool, writes included —
  there are no per-token permissions. Rotate tokens immediately if leaked.
  `MACHINA_MCP_TOKENS_JSON` maps each token to a client ID, which is attached to
  the verified request; v0.4 does not yet write it to logs, traces or CMMS
  records, so it is not an audit trail on its own.
- **Network exposure:** Bind to `127.0.0.1` unless behind a reverse proxy with TLS.
  Machina does not terminate TLS itself.

**Mitigations:**

- Generate strong tokens: `openssl rand -hex 32`. The server refuses to start
  with any token shorter than 32 characters.
- Use `MACHINA_MCP_TOKENS_JSON` (not the legacy comma-separated format), one
  token per client, so a single client can be revoked.
- Keep `mcp.allowed_hosts` / `mcp.allowed_origins` to the names clients really
  use (loopback by default): requests with another `Host` or `Origin` are
  rejected, which blocks DNS-rebinding attacks.
- Place behind a reverse proxy (nginx, Caddy, Envoy) for TLS termination.
- Restrict network access via firewall rules to known MCP client IPs.

### DocumentStore (RAG Ingestion)

**Trust level:** Ingested documents are untrusted content.

**Risk:** A malicious document could contain prompt-injection payloads that
attempt to manipulate the LLM into unauthorized actions (e.g., "ignore previous
instructions and create a work order for...").

**Mitigations:**

- Machina's prompt templates include injection-defense preambles.
- Sandbox mode (`sandbox: true` in the config; the shipped deploy configs read
  it from `MACHINA_SANDBOX_MODE`) prevents any write from executing — writes
  are logged but not sent to the CMMS.
- Outside sandbox, the agent asks for confirmation before every write by
  default (`confirmations: true`). This is an `Agent` feature: the MCP server
  has no confirmation step — the MCP client and its user decide which tool
  calls run — so keep an MCP server in sandbox mode until you trust that
  client.
- Review ingested documents before adding them to the vector store in production.

#### Source-Path Sanitisation at the LLM Boundary

**Risk:** The raw `DocumentChunk.source` returned by a connector is typically an
absolute filesystem path (e.g. `C:\Users\foo\bar\manual.md` or
`/var/data/manuals/pump.md`). Passing it verbatim into the LLM-visible context
or the `search_documents` tool result exposes the host's filesystem layout —
any model asked to cite a source will repeat the path, sometimes prefixed with
the user account name. This is a deterministic leak rooted in the framework's
own payload, not an LLM hallucination.

**Mitigations (always-on):**

- The two runtime boundaries that build LLM-visible document payloads
  (`Agent._gather_context` and the `search_documents` tool result) pass every
  source through `agent.prompts.safe_source`, which strips directory
  components from path-like strings and passes through opaque IDs and URLs.
- `format_document_results` invokes `safe_source` again as defence in depth,
  so any future call site that forgets the sanitisation upstream is still
  protected at the prompt-formatting layer.
- **Beyond the `source` field (v0.3.1):** absolute paths can also be embedded
  in the chunk **content** text itself and in **workflow error / output
  strings** that flow back to the LLM. `agent.prompts.safe_text` scrubs those.
  It is applied to chunk content in both runtime boundaries, to tool errors
  and to the `execute_workflow` step `error` payloads. It reduces each path
  that names a user or a file-share host to its file name:

    - user homes: `C:\Users\<user>\…` and `C:\Documents and Settings\<user>\…`
      (backslashes or slashes, `\\?\` and `//?/` long-path prefixes,
      `file:///C:/Users/…`), WSL, Cygwin and Git Bash
      `/mnt/<drive>/Users/<user>/…` and `/<drive>/Users/<user>/…`,
      `/home/<user>/…`, `/Users/<user>/…`, the same below a macOS volume
      (`/Volumes/<volume>/Users/<user>/…`, `/System/Volumes/Data/Users/<user>/…`)
      or a mount point (`/mnt/<disk>/Users/<user>/…`), and `/home/<user>/…`
      below other folders of an absolute path (`/srv/backup/home/<user>/…`),
      also behind `file:///` or `file://localhost/`, the four-slash URI form
      of other schemes (`sqlite:////home/<user>/…`) or an option letter
      (`-I/home/<user>/…`);
    - shares: `\\host\share\…` (WebDAV `\\host@SSL\DavWWWRoot\…` included,
      and a slash after the host, `\\host/share/…`), `\\?\UNC\host\…`,
      `//host/share/…` and `//host\share\…`, and `file:`, `smb:`, `cifs:`,
      `nfs:`, `sftp:`, `scp:`, `ssh:`, `afp:`, `dav:` and `davs:` URIs, user,
      password, port and bracketed IPv6 hosts included
      (`smb://user:pass@host:445/…`, `smb://[fe80::1]/…`,
      `file:////host/share/…`).

    A path whose last component names an account becomes `<path>`: a bare home
    directory, a share with nothing below it, or a folder right under `Users`,
    `home`, `homes` or `profiles` (`\\PC01\C$\Users\<user>`,
    `\\NAS01\homes\<user>`, `/home/<user>/backup/home/<other>`); below a
    profile or home, such a folder followed by a file name with an extension
    is an ordinary folder (`/home/<user>/proj/users/models.py`). Escaped text
    is matched too: the doubled backslashes of `repr()` and JSON
    (`'C:\\Users\\<user>\\…'`), and text escaped twice. Paths joined without
    whitespace (`a.pdf;C:\Users\…`, `PATH` lists) are found one by one, and a
    closing bracket the path did not open stays in the prose
    (`(C:\Users\<user>)` becomes `(<path>)`). In
    quotes (`repr()`, JSON, `str(OSError)`, Markdown code) the user or share
    name, a `OneDrive - …` folder and the file name may contain spaces, and
    another folder may hold words as in unquoted paths; a quoted command or
    sentence that only starts with a path and a lowercase word keeps its
    words. Unquoted Windows paths and shares accept a user name of up to four
    words (`Maria de la Cruz`), also right under a share's accounts folder
    (`\\PC01\C$\Users\john smith\…`, `smb://nas01/homes/john smith/…`), a deeper
    folder of up to four words that starts with a capitalised word
    (`New folder`, `Documenti tecnici`) or whose later words are capitalised
    or digits (`x64 Release`), and eight words after `OneDrive -`, each when a
    separator follows.

    Deliberately preserved, so manuals and messages keep full fidelity:
    instructional system paths (`/etc`, `/usr/bin`, `C:\Program Files`,
    `C:\Windows\System32`), relative and menu paths (`Settings/Users/Roles`),
    REST paths (`/api/users/42`, `/api/Users/42`), `\\.\pipe\…` device paths,
    web URLs (`http(s)`, `s3`, `gs`, `ftp(s)`, kept whole), service URIs, also
    on localhost (`mqtt://broker/topic`, `mqtt://localhost/home/…`,
    `opc.tcp://plc:4840/…`), and relative database URLs
    (`sqlite:///home/data.db`). Known limits:

    - an unquoted profile path keeps the later words of a user name with more
      than four words, and of a bare spaced name at the end of a sentence
      (`C:\Users\Mario Rossi`);
    - prose within four words after a user name or a capitalised folder, or a
      capitalised word or digit before a later slash or backslash, can be
      taken for a folder (`C:\Users\me\x.csv Line 2\P-202` keeps only
      `P-202`);
    - a POSIX path below a folder whose name holds a space
      (`/srv/my backup/home/<user>`) is not matched, nor is `/Users/<user>`
      below folders other than those listed above (`/srv/backup/Users/<user>`)
      or a Linux removable-media mount (`/media/<user>/…`,
      `/run/media/<user>/…`);
    - a relative path into a home (`../../home/<user>/…`) is kept, like any
      relative path;
    - Windows profiles below an NT device or volume path
      (`\Device\HarddiskVolume3\Users\<user>`, `\\?\Volume{…}\Users\<user>`)
      or a drive folder (`E:\Backup\Users\<user>`), JSON with escaped slashes
      (`"\/home\/<user>\/…"`) and a user name holding a non-breaking space are
      not matched;
    - an absolute path with a `/home/` segment is treated as a home
      (`/api/home/dashboard`), and a lowercase `/users/<user>/` (a
      case-insensitive macOS volume) is not matched;
    - a web URL whose own path names a user (`http://intranet/home/<user>/…`)
      is kept, and a protocol-relative URL (`//cdn.example.com/lib.js`) reads
      as a share;
    - a bare `\\host` with no share is not matched, because it cannot be told
      apart from escaped regex text such as `\\d+`.
- **Workflow outputs:** an `agent.reason` step's prompt goes to the model
  provider, and so does the `output_summary` the agent's `execute_workflow`
  tool returns for each step. Both are rendered by
  `agent.prompts.render_for_llm`:

    - Document chunks from a `docs.search_documents` step become a `Source:`
      line with the file name (`safe_source`), page and section, then the text
      (`safe_text`). Chunks nested in dicts, lists and tuples are rendered the
      same way.
    - A value read from a `source` field (`{find_doc.source}`,
      `{trigger.source}`), and a quoted `source` value in a rendered dict,
      dataclass repr or JSON string, keeps only its file name when it is an
      absolute path or a file-share URI. Any other source, such as an alarm's
      `mqtt://` origin, an OPC-UA node id or a relative path, keeps its text.
    - Any other value passes through `safe_text`. A value that starts with a
      home or share path is scanned as one quoted value, so a value that is
      exactly a home directory or share becomes `<path>` whatever words the
      user name holds; a value that starts with a bare home directory and
      continues with prose (`/home/<user> has 2 GB free`) also becomes
      `<path>`.
- **Not yet covered:** the MCP server's `machina_search_manuals` chunk
  content does not pass through `safe_text`; only the `source` field is
  sanitised there. MCP errors and `error` entries do (see
  [Exception text at the boundaries](#exception-text-at-the-model-mcp-and-workflow-boundaries)).
- The system prompt's Guideline 8 explicitly forbids the LLM from disclosing
  absolute file paths, directory structures, database schemas, or system
  architecture. This is a backstop, not the primary defence — the primary
  defence is to never hand those values to the LLM in the first place.
- The raw `chunk.source` remains available for non-LLM consumers (logs,
  trace files protected separately by `ActionTracer.redacting_dump_json`).
  See [Traces / Redaction](../observability/traces.md#redaction) for the
  parallel mechanism on the trace export side.

### Exception Text at the Model, MCP and Workflow Boundaries

**Risk:** a library exception's message can carry the request URL, the backend
host and port, a token in a query string, SQL driver text naming the server
and the user, a file path, or a backend value
(`invalid literal for int() with base 10: '12.000'`). Passed on, it reaches the
model and its provider, an MCP client, or a workflow result.

**Rule (always-on):** an exception crosses these boundaries only as
`machina.agent.tool_errors.public_error_text(exc)`:

- a `MachinaError` shows its message, scrubbed by `safe_text`;
- any other exception shows its type only:
  `Unexpected error (<Type>). The details are in the server log.` The wording
  never reads as an outage, since a bug is not "the backend is down".

The boundaries:

- the agent's read tools, including a failed read replayed later in the turn,
  and `get_maintenance_schedule`;
- `StepResult.error` in every `WorkflowResult` (a Machina error as
  `<Type>: <message>`, a raising guard prefixed `Guard raised`), and the
  agent's `execute_workflow` tool, for a step and for the workflow's own
  failure;
- every MCP tool, domain and vendor, and the two CMMS resource templates. The
  server registers each one wrapped by `machina.mcp.errors.guard_tool` /
  `guard_resource`. A failure stays an MCP error result (`isError: true`) or
  a JSON-RPC error; a write tool's foreign exception adds that the write may
  have been applied, except in sandbox mode, where no write goes out;
- the `error` entries that `machina_list_assets`,
  `machina_get_maintenance_history` and `machina_send_message` return for a
  connector error.

Every failure stays a failure: an `error` result, an MCP error, or a step with
`success=False` under its policy. Only the text changes.

**Diagnostics stay in the server log.** The boundary's event
(`read_tool_failed`, `mcp_tool_failed`, `mcp_resource_failed`, `step_error`,
`workflow_execution_failed`) records the message, and so does
`runtime_connector_failed` for a connector that fails to connect. For a
foreign exception it also records the plain-text traceback (`exception`, no
frame variables). For a Machina error raised from one it records the cause's
type (`cause_type`) only: the REST connectors and the mappers chain httpx
and pydantic errors whose text is the request URL or a record value, which
the log leaves out as the message does. Tool arguments are not logged.

**Message contract for connector authors:** a `MachinaError` message is shown,
so it names what failed and, at most, the library exception's type:

```python
try:
    rows = cursor.execute(query).fetchall()
except pyodbc.Error as exc:
    raise ConnectorError(f"SQL read failed ({type(exc).__name__})") from exc
```

Never put the library exception's text (`{exc}`, `{exc!r}`, `str(exc)`), a
URL, a host, a credential, a connection string or a backend value in the
message. The chained exception carries the detail to the log.
`tests/unit/test_error_message_contract.py` fails on a new raise site that
embeds an exception's text.

**Not covered:**

- result content, such as `machina_search_manuals` chunk text and vendor tool
  response bodies;
- a "not found" or refusal message that repeats the caller's own value;
- errors raised while a connector connects, which reach the caller of
  `Agent.start()` and `MachinaRuntime.failed_connectors`, not the model; the
  SQL drivers' connect errors keep their driver text, with the DSN password
  redacted;
- the library text a connector's message no longer carries is not logged by
  the boundary either (only its type); connectors that log it themselves
  (SQL timeouts and retries, DocumentStore search failures, unreadable
  spreadsheet cells) still do;
- `LLMError`, raised to the agent's caller when the model call fails;
- logs, which keep exception text by design (see [Log Output](#log-output)).
  FastMCP also logs a failed resource read through its own logger.

### Sandbox Enforcement (Layer-Wide)

**Trust level:** Sandbox mode is the safety boundary for evaluation, demos, and
initial deployment — no real external side effect must execute while it is on.

**Guarantee (v0.3.1):** sandbox enforcement is a *layer-wide invariant*, not a
per-connector choice. Every external-mutation path is guarded by the
`@sandbox_aware` decorator, which raises `SandboxViolationError` before the
method body runs:

- CMMS `create_work_order` / `update_work_order` (Generic, SAP PM, Maximo, UpKeep, Excel/CSV, SQL)
- Comms `send_message` (Telegram, Slack, Email)
- MQTT `publish`
- Calendar `create_event` / `delete_event`

`CliChannel.send_message` is intentionally **exempt** — it only prints to
stdout and must keep working in sandbox so the agent can still reply.

**MCP request tasks:** each MCP tool call runs in its own asyncio task that does
**not** inherit the `_sandbox_mode` contextvar set once at server startup. Both
the domain (`mcp.tools._runtime`) and vendor (`mcp.tools_vendor._runtime`)
helpers re-establish it on every request, and the vendor tools read
`get_sandbox_mode()` only *after* that — otherwise a raw vendor write (e.g. the
Maximo attribute update) would get past the tool's own check in sandbox mode.
Each vendor tool also writes through a connector helper with its own
`@sandbox_aware` guard (SAP PM's `_write_with_csrf`, Maximo's
`_patch_resource`, which `update_work_order` also uses), so that check is not
the only gate.

**Companion invariant — write integrity:** the write paths also guard
against duplicates. Auto-generated work-order IDs are a deterministic content
hash (`auto_work_order_id`); the Excel/CSV, SQL and local Generic CMMS
connectors return the existing work order when its ID is already there, and
the Generic CMMS REST mode sends the ID to the backend. The agent loop
memoises side-effecting tools per turn. The vendor connectors' HTTP retries
are method-aware: POST and PATCH are *not* retried on network errors,
timeouts or 503 answers, since any of those can follow a write the server
already made; 429 answers are retried for every method because the server
refused the request without processing it. SAP PM, Maximo and UpKeep let the
CMMS number new work orders, so these guards do not deduplicate a create
retried later against them (see [Uptime](uptime.md#transient-failure-handling)).

### Trace JSONL Files

**Trust level:** Internal diagnostic data.

**Risk:** Trace files (written only when you attach a `JSONLExporter`) may
contain:

- The first 200 characters of each tool result and workflow step output
  (asset names, work-order descriptions, …)
- Asset IDs, connector names and LLM token/cost data

They do **not** contain prompts or message text, and metadata values under
secret-like keys (`token`, `password`, `secret`, `api_key`, `client_secret`,
`authorization`) are redacted on export.

**Mitigations:**

- The exporter creates its directory with mode `0700` and files with `0600`
  (POSIX); keep them there.
- Review what tool results reveal before shipping trace files to external
  systems.
- Rotate and archive traces periodically (the exporter starts a new file each
  UTC day).

### Log Output

**Trust level:** Internal diagnostic data.

**Risk:** a traceback rendered with each frame's local variables exposes
whatever those frames held, and a REST connector's frames hold its auth
headers. structlog's console renderer prints frame locals by default when
`rich` is installed (the `litellm` and `docs-rag` extras pull it in), and
`structlog.processors.dict_tracebacks` includes them in JSON output by
default.

**What Machina logs:**

- After `configure_logging()`, every traceback that reaches its handler, from
  Machina, your application or a third-party library, is rendered as plain
  text in an `exception` field, in console and JSON output. The text has the
  exception type, message and chained causes, and each frame's file, line and
  function, but no frame variables.
- Machina's own exception logs write that text themselves
  (`exception=traceback_text(exc)`), so they carry no frame variables under any
  structlog configuration, including none at all.
- Exception messages are logged verbatim, and the traceback shows each frame's
  source line.
- When `configure_logging()` runs, it raises the `httpx` and `httpcore`
  loggers to WARNING. A stricter level the application already set (ERROR,
  say) is kept, never lowered. Two kinds of line are therefore not logged:
  httpx's per-request INFO lines, which carry each request's full URL (the
  CMMS host and the record IDs and filter values of the read: UpKeep part
  IDs, SKUs in SAP PM's `$filter` and Maximo's `oslc.where`), and httpcore's
  DEBUG lines, which carry the host and port and each response's headers.
  Limitations:
  - The levels are set once, when `configure_logging()` runs. An application
    that does not call it, calls it and then lowers the two loggers, or
    reconfigures logging afterwards (`logging.config.dictConfig`, its own
    handlers on those loggers) gets those lines in its logs again. Logging
    systems configured independently of `configure_logging()` are not
    protected by it.
  - Only the `httpx` and `httpcore` loggers are covered. Other libraries'
    records reach the log as those libraries write them, without Machina's
    redaction: for example urllib3 and the Google API client at DEBUG.
  - Machina's own retry warnings (`http_rate_limit_retry`,
    `http_network_retry`, `http_retry_after_exceeds_max_backoff`) log the
    method, the status or the error's type (and, for a retry, the attempt),
    and the request URL reduced to its scheme, host, port and path. The query
    (filter values, page tokens, and the whole query a Maximo or SAP PM
    next-page link repeats), user info and fragment are left out, an OData
    key predicate is logged as `(<key>)`, and a matrix parameter's value
    (such as a `;jsessionid=`) as `<value>`. A request for one record logs
    its endpoint, not the record: `<host>/api/v2/assets/<asset>`,
    `<host>/api/v2/work-orders/<work order>` and
    `<host>/api/v2/parts/<listed part>` for UpKeep,
    `<host>/maximo/oslc/os/<object structure>/<resource id>` for Maximo, and
    `MaintenanceOrder(<key>)` for SAP PM. A network error is logged by its
    type only, since httpx's message can name the address it could not
    reach. A record ID in a plain path segment of a link the CMMS sends back
    (rather than one the connector builds) is not recognized.
- The Telegram bot token is kept out of log output:
  - The Bot API carries the token in every URL. httpx logs request URLs at INFO, and python-telegram-bot logs its API URLs at DEBUG. `TelegramConnector` replaces its token with `<redacted>` in the records of those loggers. The redaction is in place from `connect()` until the process ends.
  - A token the server rejects surfaces as `ConnectorAuthError("Telegram rejected the bot token")`, without python-telegram-bot's message, which contains the token.
  - A startup error whose message names the token is withheld the same way.
  - Not covered: a host renderer that prints frame locals on a python-telegram-bot frame, since `ExtBot`'s repr includes the token. Use `configure_logging()`.
- Other credentials that a library puts into an exception message or a request URL are not scrubbed. Keep them out of URLs, and treat log output as sensitive.

**Mitigations:**

- Call `configure_logging()` at startup. An application that configures
  structlog itself, or logs exceptions without calling `configure_logging()`,
  gets structlog's defaults for its own `exc_info` logs, and those print frame
  locals. Give `ConsoleRenderer` `exception_formatter=structlog.dev.plain_traceback`,
  and put `structlog.processors.format_exc_info`, not `dict_tracebacks`,
  before a JSON renderer.
- A logging handler you add after `configure_logging()` formats third-party
  records itself: give it `format_exc_info` too.

## Secrets Management

### Baseline: Environment Variables

The simplest approach — secrets live in an environment file, and the YAML
config references them as `${VAR}` placeholders (a missing variable fails the
start instead of falling back):

```bash
# /etc/machina/machina.env (systemd)
# chmod 600, owned by root
MACHINA_MCP_TOKENS_JSON={"<64-hex token>": "client-a", "<64-hex token>": "client-b"}
MACHINA_CMMS_API_KEY=...
```

For Docker, use `.env` with `docker compose` (`deploy/docker/.env`, never
committed). The MCP server needs no LLM key; an agent you run needs its
provider's key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …), read by LiteLLM.

### Advanced: External Secret Stores

For production deployments with stricter compliance requirements:

**HashiCorp Vault / Azure Key Vault:**
Wrap the Machina start command in a script that fetches secrets and injects
them as environment variables:

```bash
#!/bin/bash
export MACHINA_MCP_TOKENS_JSON=$(vault kv get -field=tokens secret/machina/mcp)
export MACHINA_CMMS_API_KEY=$(vault kv get -field=api_key secret/machina/cmms)
exec /opt/machina-venv/bin/machina mcp serve \
    --transport streamable-http \
    --host 127.0.0.1 \
    --config /etc/machina/config.yaml
```

Update the systemd unit's `ExecStart` to point to this wrapper script.

**SOPS for GitOps:**
Encrypt your `.env` or `config.yaml` with [SOPS](https://github.com/getsops/sops)
and decrypt at deploy time:

```bash
sops --decrypt machina.env.enc > /etc/machina/machina.env
chmod 600 /etc/machina/machina.env
```

### What to Protect

| Secret | Where used | Rotation impact |
|--------|-----------|-----------------|
| `MACHINA_MCP_TOKENS_JSON` | MCP client auth | Restart required; coordinate with MCP clients |
| CMMS credentials (e.g. `MACHINA_CMMS_API_KEY`) | CMMS connector, via `${VAR}` in the config | Restart required |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | LLM calls of agents you run (not the MCP server) | Restart required |
| OPC-UA certificates | IoT connector | Restart required; re-establish subscriptions |

## Supply Chain

### Dependencies

Machina's Python dependencies are declared in `pyproject.toml` with version constraints.
Review the dependency tree before deploying:

```bash
pip install pipdeptree
pipdeptree --packages machina-ai
```

### Container Image

The Docker image (`deploy/docker/Dockerfile`) uses a multi-stage build with
`python:3.11-slim-bookworm` as the base. To pin the base image digest, pass
it as the `PYTHON_BASE` build argument. It sets the base of both the build and
the runtime stage, and the mock CMMS image takes the same argument:

```bash
docker buildx imagetools inspect python:3.11-slim-bookworm   # prints the current digest
docker build -f deploy/docker/Dockerfile \
  --build-arg PYTHON_BASE=python:3.11-slim-bookworm@sha256:<digest> -t machina:latest .
```

See [Build Arguments](docker.md#build-arguments) for the other build-time
pins.

### Vulnerability Scanning

```bash
# Scan the Python dependencies installed in the Machina environment
/opt/machina-venv/bin/python -m pip install pip-audit
/opt/machina-venv/bin/python -m pip_audit

# Scan container image
docker scout cves machina:latest
# or
trivy image machina:latest
```

#### Unresolved Advisories: chromadb 1.5.9

A scan of an environment with the `docs-rag` extra (which `docs-rag-pro` and
`all` include) reports four advisories against chromadb 1.5.9, the version
`deploy/docker/constraints.txt` pins. They are unresolved: as of 22 November
2026, OSV lists no fixed version for any of them, and 1.5.9 is the latest
chromadb release.

| Advisory | Where it applies |
| --- | --- |
| [GHSA-f4j7-r4q5-qw2c](https://osv.dev/vulnerability/GHSA-f4j7-r4q5-qw2c) (CVE-2026-45829) | A Chroma server: code execution, without authentication, through its collections endpoint |
| [GHSA-36p7-vc44-83pf](https://osv.dev/vulnerability/GHSA-36p7-vc44-83pf) (CVE-2026-45833) | A Chroma server: code execution by an authenticated user who holds `UPDATE_COLLECTION` |
| [GHSA-2wm9-hf6c-p5cr](https://osv.dev/vulnerability/GHSA-2wm9-hf6c-p5cr) (CVE-2026-45830) | A Chroma server: any authenticated user can read, change and delete data in every tenant's collections |
| [GHSA-xph7-9rjv-w5fr](https://osv.dev/vulnerability/GHSA-xph7-9rjv-w5fr) (CVE-2026-45831) | A Chroma server using `SimpleRBACAuthorizationProvider`: a permission is not limited to its tenant, database or collection |

`DocumentStoreConnector` runs no Chroma server. It creates its vector store
without a client, host or persist directory, so with chromadb's default
settings it gets an ephemeral client inside the Machina process that keeps
the index in memory.

chromadb also reads its settings from environment variables and from a `.env`
file in the working directory. Machina does not support these configurations:

- `CHROMA_API_IMPL` set to chromadb's HTTP client
  (`chromadb.api.fastapi.FastAPI`), with `CHROMA_SERVER_HOST`: Machina becomes
  a client of an external Chroma server. The advisories then apply to that
  server, and its exposure is that server's deployment, outside Machina.
- `IS_PERSISTENT`, which keeps the index on disk in `PERSIST_DIRECTORY`
  (`./chroma` by default), with a directory others can write: whoever can write
  it can change what the agent retrieves.
- Running a Chroma server next to Machina and exposing it: that server is what
  the advisories describe.

If you use a persist directory, make it private to the account Machina runs
under (for example `chmod 700`). This limits the exposure; it does not fix the
advisories, which stay open until chromadb publishes a fixed release.

## Hardening Checklist

- [ ] Use streamable-http transport (not stdio) in multi-user environments
- [ ] Generate unique MCP tokens per client with `openssl rand -hex 32`
- [ ] Map tokens to client identities in `MACHINA_MCP_TOKENS_JSON`
- [ ] Place behind a TLS-terminating reverse proxy, and list its host name in `mcp.allowed_hosts`
- [ ] Restrict `/etc/machina/machina.env` to `chmod 600`
- [ ] Enable sandbox mode during initial deployment (`sandbox: true`, or `MACHINA_SANDBOX_MODE=true` with the shipped configs)
- [ ] Keep exported trace files in the exporter's `0700` directory
- [ ] Call `configure_logging()`, or configure structlog with a traceback formatter that omits frame locals ([Log Output](#log-output))
- [ ] Review documents before ingesting into DocumentStore
- [ ] Run `pip-audit` or equivalent in CI
- [ ] Pin the Docker base image digest in production builds
