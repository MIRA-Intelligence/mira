# Single-user gateway authentication

This branch supports one trusted user (including access through an SSH tunnel).
A gateway token grants administration of all projects in that engine instance.
It does not provide accounts, project ownership or isolation between multiple
users. Keep the gateway on loopback or behind an authenticated TLS proxy.

## Token lifecycle and UI configuration

Starting the gateway creates `MIRA_HOME/runtime/gateway-token` with mode 0600
on POSIX systems. The default home is `~/.mira`. Existing valid tokens are reused.
Invalid, symlinked, hard-linked or publicly readable token files stop startup;
authentication is never silently disabled. Keep MIRA_HOME under the engine
user's control. Protect the user profile with equivalent ACLs on Windows.

The token path and format match mira-ui's existing Electron token bridge.
For the browser UI, enter the token in Settings > Engine Token for the chosen
engine API origin. The token is a gateway credential, separate from model
provider API keys. Do not commit the token file or put it in issue reports.

## Request policy

- GET/HEAD health and version probes remain public.
- OPTIONS allows browser preflight without invoking application operations.
- Other HTTP requests require `Authorization: Bearer <token>`.
- WebSocket connections and project artifact GET/HEAD requests may instead use
  `access_token` in the query string, matching the existing UI client.
- Query tokens cannot authorize configuration changes, deletions or other API
  requests. Invalid headers cannot fall back to a query token; duplicate
  credentials are rejected.
- CORS permits the UI's Content-Type, Authorization and X-Mira-Request-Id
  headers for configured origins. CORS is not the authentication mechanism.

Authenticated HTTP responses disable caching and referrer propagation.
Default aiohttp access logs are disabled because request URLs may contain the
token. Reverse proxies must also redact or omit query strings in their logs.
Project HTML, SVG and XML documents receive a CSP sandbox; their scripts will
not execute when opened through the engine. This is an intentional restriction
on active documents, including otherwise interactive HTML deliverables.

## File operations and startup

Single-file deletion is not included in this branch. Its UI entry and client
helper are removed in the paired UI branch. Existing project-level operations
are protected by the same gateway-admin token; they are not per-user ACLs.

Artifact paths preserve filename whitespace exactly. A missing filename with
trailing whitespace does not fall back to another file with a trimmed name.
Existing project-boundary checks remain in place.

Gateway startup fails if its port is already occupied. It does not terminate
another process to acquire the port.

This is targeted hardening, not a claim of zero risk or a complete security
audit. Do not treat the branch as a multi-tenant deployment solution. Model
tool permissions and the rest of the engine still require their own review.
