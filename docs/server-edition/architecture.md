# Mira Server Edition: Hub Architecture

Status: Draft for review (#151), revision 2. Milestone: *Server Edition (Multi-user)*.

This document describes how people use Mira on a shared server. The target
environment is a typical lab or company Linux host where every person already
has a server account (local or LDAP/SSSD) and a home directory.

The design follows **Plan B**: an authenticating **Mira Hub** in front of
**one `mira-engine` per user, running as that user's own Unix account**. This
is the same shape as JupyterHub with PAM authentication and a sudo spawner.
The engine stays a single-user program; multi-user concerns live in the hub,
and the operating system's existing permissions are the isolation boundary.

Revision 2 changes: existing server accounts are the default identity source
(not hub-created OS users), three access modes are defined (§2), team keys go
through a hub LLM proxy, and runtime state is separated from home-directory data.

## 1. Goals and non-goals

### Goals

- Reuse the server's existing accounts, groups and home directories. A person
  signs in with their server username and password; Mira stores no passwords.
- An agent can access exactly what its user can access on the host, no more.
  Existing group-shared directories (for example `/data/lab`) just work.
- A single user can use a remote engine without any hub (Modes A and B, §2).
- Teams can share projects and live sessions, with model usage billed to the
  session owner, and can use team provider keys without exposing them.
- The desktop app keeps working unchanged in local mode. Hub features are opt-in.
- Reuse the existing engine with minimal change.

### Non-goals (for this milestone)

- Making one engine process multi-tenant.
- Public SaaS: billing, sign-up and abuse handling for anonymous users. The hub
  targets teams of roughly 5–100 people on infrastructure they control.
- Managing server accounts. Creating users, groups and home directories stays
  with the host's existing tooling (LDAP, `useradd`, configuration management).
- Horizontal scaling of one user's engine across machines, and HA hub clusters.
- Real-time co-editing of deliverables (CRDT). Sharing is project and session
  based.
- Several engines writing to one project directory, as in
  `docs/modular-workflow-api-contract.md`. A project has exactly one hosting
  engine at a time (§8.3, §9.1).

## 2. Access modes

| | A. Direct remote engine | B. Remote-SSH | C. Hub |
|---|---|---|---|
| Who | One person | One person | A team |
| Server side | `mira gateway` started by hand (or as the user's service) | Nothing pre-started; the desktop starts it over SSH | `mira-hub` service run by an admin |
| Authentication | Gateway token (#149) | SSH (keys, agent, `~/.ssh/config`, ProxyJump) | Server account via PAM / LDAP |
| Transport security | Must be added: TLS reverse proxy or SSH tunnel | SSH tunnel | TLS reverse proxy |
| Engine runs as | Whoever started it | The SSH user | The signed-in user (sudo launcher) |
| Needs root / admin | No | No | Yes, once (install hub, sudoers rule) |
| Sharing, shared sessions, team keys, quotas | No | No | Yes |

### 2.1 Mode A: direct remote engine

This is what works today: the engine is installed on a server and mira-ui's
`remoteManual` mode connects to its HTTP and WebSocket endpoints.

- After #149 every request needs the gateway token (`mira gateway-token` on the
  server prints it; paste it into the desktop's connection settings).
- The token and all traffic, including prompts, files and results, are
  **plaintext** over `http://` and `ws://`. Exposing the port on a network is
  only acceptable behind TLS:
  - preferred: keep the engine on `127.0.0.1` and put Caddy or nginx in front
    with `https://` and `wss://`; or
  - use an SSH tunnel, which is the manual form of Mode B:
    `ssh -N -L 18790:127.0.0.1:18790 user@server`, then connect to
    `http://127.0.0.1:18790` with the token.
- Binding `0.0.0.0` without TLS logs a startup warning (#149) and is not a
  supported configuration.
- The engine has the permissions of whoever started it. Do not run it as root
  or as a shared service account.

### 2.2 Mode B: Remote-SSH (ticket to be filed)

The desktop app automates the SSH-tunnel setup, like VS Code Remote-SSH:

```mermaid
sequenceDiagram
  participant D as Desktop (mira-ui)
  participant S as ssh client
  participant H as Server (user's account)
  D->>S: ssh host (honours ~/.ssh/config, ProxyJump, agent)
  S->>H: mira --version (install / upgrade check)
  S->>H: start or attach engine: mira gateway --host 127.0.0.1 --port P (engine lease, §7.3)
  S->>H: mira gateway-token
  S-->>D: token (over SSH, never on disk locally)
  D->>S: ssh -L localport:127.0.0.1:P
  D->>D: connect http://127.0.0.1:localport with Bearer token
```

- Uses the system `ssh` binary, so existing keys, agents, jump hosts and
  2FA keep working. Mira stores no SSH credentials.
- The engine keeps running on the server when the laptop sleeps or the tunnel
  drops; the desktop reconnects and re-attaches.
- Engine version check on connect: if the server has no compatible engine, the
  desktop shows install or upgrade instructions (automatic install is a later
  option).
- Requires the single-engine lease (§7.3) so that a Remote-SSH engine, a
  hub-spawned engine and a manually started `mira gateway` never write the
  same `~/.mira` at once.

Mode B is the recommended way for a single person to use a server: no admin
involvement, no new open ports, and the permission model is exactly SSH's.

### 2.3 Mode C: Hub

The rest of this document. Choose it when a team needs browser access,
project sharing, shared live sessions, team-owned provider keys, quotas or an
audit trail.

## 3. Why one engine per user (Plan B)

Today the engine assumes one user everywhere:

| Area | Current behaviour (code) |
|---|---|
| Identity | No user model. WebSocket `user_id` is client-supplied and defaults to `ui_user` (`channels/ui.py` WS handler); used only for logging and the workflow `lease_owner`. |
| State | `MIRA_HOME` (`config/loader.py:get_home_dir`) holds one `config.json`, `media/`, `cron/`, `logs/`, `runtime/` and the UI registries `workspace.json` / `organization.json`; `ProjectRegistry` has a single `projects_root` (`projects.py`). |
| Keys | `ProviderConfig.api_key` lives once per provider in the global `config.json`; `POST /api/config` rewrites it and hot-swaps provider and model for every session. |
| Tools | Run as the engine's OS user. `tools.restrict_to_workspace` defaults to `false`, `tools.exec.sandbox` defaults to `""` (`config/schema.py`). |
| Background services | Cron, heartbeat, MCP servers and IM channels are started once per gateway process (`cli/commands.py:gateway`). |

Making the process multi-tenant would mean touching every row, and any one we
miss becomes a cross-user leak. With one engine per user running as that
user's own uid, the kernel enforces the same file permissions the person
already has over SSH, and the engine's single-user semantics stay correct.

Cost: one Python process per *active* user, roughly 150–400 MB RSS plus MCP
children. Idle culling (§10) keeps this bounded.

## 4. Components

```mermaid
flowchart LR
  subgraph Clients
    W[mira-ui web build<br/>served by the hub]
    D[mira-ui desktop<br/>Team server mode]
    C[CLI / OpenAI-compatible clients]
  end
  subgraph Server
    P[TLS reverse proxy<br/>Caddy / nginx]
    H[Mira Hub<br/>runs as mira-hub<br/>auth · spawner · proxy · policy]
    L[LLM proxy<br/>team keys, metering]
    DB[(hub.sqlite3)]
    PAM[[PAM / LDAP / SSSD]]
    subgraph E1["engine: alice (uid alice)"]
      G1[gateway 127.0.0.1:p1]
    end
    subgraph E2["engine: bob (uid bob)"]
      G2[gateway 127.0.0.1:p2]
    end
  end
  W & D & C --> P --> H
  H --- DB
  H --- PAM
  H -->|sudo launcher| E1 & E2
  H -->|Bearer engine token| G1
  H -->|Bearer engine token| G2
  G1 & G2 -->|team-key models| L
```

### 4.1 Mira Hub (`mira_hub/`, new package in this repo)

- **Auth**: PAM by default, direct LDAP bind as an alternative, hub-managed
  accounts only as a fallback (§6).
- **User store**: SQLite with identities (username, uid, groups snapshot),
  hub sessions, project memberships, engine records, quotas and the audit log.
  No passwords.
- **Spawner**: starts, stops and health-checks one engine per user through a
  sudo launcher (§7).
- **Proxy**: authenticates every request and forwards `/api/*` and `/ws` to the
  right engine, injecting that engine's token. Browsers never see engine tokens.
- **LLM proxy**: an OpenAI-compatible endpoint that holds team provider keys,
  enforces the model allowlist and quotas, and meters usage (§8.3).
- **Policy**: route-level authorization for roles, admin-only routes and shared
  project access (§9), plus quota checks (§11).
- **Static UI**: serves the mira-ui web build on the same origin, so hub session
  cookies work without CORS.

**Stack: Python + aiohttp**, shipped as a second package in this repo with a
`mira-hub` console script:

- The engine gateway is already aiohttp (`channels/ui.py`); proxying WebSocket
  and streaming HTTP in aiohttp is straightforward.
- The hub can import shared pieces (`config/schema.py` models,
  `observability.py`, the token-usage schema) instead of re-implementing them.
- One CI pipeline and one release train. A separate wheel or an extra keeps hub
  dependencies (`python-pam`, `ldap3`, `authlib`) out of desktop bundles.

Not chosen: Go or Node for the hub (a second toolchain for a small service), and
JupyterHub itself (its spawner and proxy model fits, but its UI and
Jupyter-specific APIs would need heavy adaptation).

### 4.2 Per-user engine

The existing `mira gateway`, unchanged in behaviour, started by the launcher
as the user's own uid with:

- `MIRA_HOME=~/.mira` (the same home a Mode A/B engine would use)
- `MIRA_RUNTIME_DIR` on local disk (§8.1; new engine setting)
- `--host 127.0.0.1 --port <assigned>`
- `MIRA_GATEWAY_TOKEN=<hub-generated>` in the environment (§6.3)
- `MIRA_GATEWAY_ALLOWED_ORIGINS=<hub origin>`
- Provider `api_base` for team models pointing at the hub LLM proxy (§8.3)

Everything else comes from the user's own `~/.mira/config.json`. The user
owns that file, so nothing that protects *other* users is enforced through it
(§8.2).

### 4.3 mira-ui connection modes

| Mode | Status | Use |
|---|---|---|
| `localBundle` | exists | Engine on this machine |
| `remoteManual` | exists | Mode A |
| `remoteSsh` | new (ticket to be filed) | Mode B |
| `teamServer` | new (mira-ui#69) | Mode C; server URL + sign-in, all traffic through the hub |

In Team-server mode the desktop is a **thin client**: all projects live on the
server. Hybrid local-plus-server projects are out of scope for v1.

## 5. Request flow

### 5.1 HTTP

```mermaid
sequenceDiagram
  participant U as mira-ui
  participant H as Hub
  participant E as Engine (alice)
  U->>H: GET /api/projects (cookie or Bearer hub token)
  H->>H: authenticate → alice, check policy, ensure engine running
  H->>E: GET /api/projects<br/>Authorization: Bearer ENGINE_TOKEN<br/>X-Mira-Request-Id: ID
  E-->>H: 200 JSON
  H-->>U: 200 JSON (hop-by-hop headers stripped)
```

- The hub strips client `Authorization` and `Cookie` headers before forwarding
  and adds the engine token, plus `X-Mira-Hub-User`, `X-Mira-Hub-Role` and a
  hub-generated `X-Mira-Request-Id` (lines up with the engine's runtime
  diagnostics). Engines treat `X-Mira-Hub-*` as informational; the hub already
  enforced policy.
- Streaming responses and uploads are proxied chunk-by-chunk.
- Resource URLs loaded through `<img>` or `<iframe>` work through the cookie on
  the hub origin. The engine-side `access_token` query parameter is only used
  hub → engine.

### 5.2 WebSocket and streaming

```mermaid
sequenceDiagram
  participant U as mira-ui
  participant H as Hub
  participant E as Engine
  U->>H: GET /ws (Upgrade, cookie / hub token, Origin)
  H->>H: authenticate, Origin check, resolve target engine
  H->>E: GET /ws?access_token=ENGINE_TOKEN (Upgrade)
  E-->>H: 101
  H-->>U: 101
  loop each frame
    U->>H: client frame
    H->>H: policy filter (role, project, quota)
    H->>E: frame (+ hub user attribution)
    E-->>H: stream / tool / workflow events
    H-->>U: frame
  end
```

- The hub reads `type` and `session_id` of client frames to enforce roles, and
  overwrites `user_id` with the authenticated identity (today the engine trusts
  the client value).
- Engine-to-client frames pass through unparsed.
- On engine restart the hub closes client sockets with code 1012; mira-ui's
  existing reconnect logic re-establishes them.

## 6. Identity and authentication

### 6.1 Identity sources

1. **PAM (default).** The hub authenticates username and password through the
   host's PAM stack (a dedicated `/etc/pam.d/mira-hub` service), so local
   accounts, LDAP/SSSD, password policies, expiry and lockouts all apply as
   they do for SSH. The hub never stores passwords.
2. **Direct LDAP bind**, for hubs that cannot use the host PAM stack (for
   example a containerised hub). Username → DN mapping and group lookup are
   configured in `hub.toml`.
3. **Hub-managed accounts (fallback)**, for hosts without per-person server
   accounts. The hub then creates a dedicated Unix user per Mira user through a
   privileged helper (the revision-1 design). Not part of the v1 MVP.
4. **OIDC / Feishu (later).** An external login must still resolve to a Unix
   account (for example a `preferred_username` or employee-ID claim mapped to
   the LDAP `uid`), because the engine runs as that account.

**Authorization uses groups**, resolved through NSS at login and refreshed
periodically: members of `mira-users` (name configurable) may use the hub,
members of `mira-admins` are hub admins. Removing someone from the group, or
disabling the LDAP account, revokes access at the next check and stops their
engine.

### 6.2 Hub sessions

| Client | Mechanism | Why |
|---|---|---|
| Web build (served by the hub) | `HttpOnly; Secure; SameSite=Lax` session cookie + CSRF header on state-changing requests | No token in JS; same origin, no CORS. |
| Desktop (Electron) | Bearer access token (15 min) + refresh token (30 days) in Electron `safeStorage` | Cross-origin from `file://`; refresh tokens are revocable server-side. |
| CLI and `/v1` clients | Personal access tokens, scoped and revocable | Scripting and OpenAI-compatible tools (#160). |

Tokens are stored hashed; logout, group removal and account disable revoke them.

### 6.3 Hub ↔ engine

The hub uses the Step 0 engine contract (#149):

- The hub generates a random token per engine start and passes it as
  `MIRA_GATEWAY_TOKEN`. It cannot read the user's `0600`
  `~/.mira/runtime/gateway-token`, and it does not need to.
- The token is handed to the launcher on **stdin**, never in argv (argv is
  visible to every user through `ps`). `/proc/<pid>/environ` is readable only
  by the process owner and root.
- The engine binds `127.0.0.1`. Every local user can reach a loopback port, so
  the token is mandatory, and the hub verifies the listener before sending it
  (§7.2, port squatting).
- The engine must strip `MIRA_GATEWAY_TOKEN` from the environment of tool
  subprocesses (shell, MCP stdio servers), so a prompt-injected command cannot
  call its own engine's API directly (engine work item).
- Later option: a Unix domain socket instead of TCP, removing the port-squatting
  class entirely.

## 7. Spawner

### 7.1 Options

| | Sudo launcher on the host (v1) | Container per user (later) | Batch scheduler, e.g. Slurm (later) |
|---|---|---|---|
| Runs as | The user's real uid | A container user mapped to the uid | The user's job |
| Files | Home and group-shared dirs as today | Bind-mounted home and data dirs | Cluster filesystems |
| Fit | Shared lab/company servers with existing accounts | Teams standardised on Docker/k8s | HPC clusters |

**v1: sudo launcher on the host.** It matches environments where people already
have accounts and homes, needs no container runtime, and keeps the engine's
bwrap sandbox effective. The spawner sits behind an interface
(`Spawner.start/stop/status`) so container and batch spawners can follow.

### 7.2 Sudo launcher details

- The hub runs as the unprivileged system user `mira-hub`.
- One sudoers rule, nothing else:
  `mira-hub ALL=(%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch`
- `engine-launch` is a small root-owned script (the JupyterHub sudospawner
  pattern). It accepts only `start|stop|status`, reads the port and token from
  stdin as JSON, validates them, sets `MIRA_HOME`, `MIRA_RUNTIME_DIR`,
  `MIRA_GATEWAY_TOKEN` and allowed origins, and execs the system-installed
  engine (`/opt/mira/bin/mira gateway --host 127.0.0.1 --port P`). It never
  runs caller-supplied commands.
- **Resource limits**: when the user has a systemd user manager (lingering
  enabled by the admin), the launcher starts the engine with
  `systemd-run --user --unit mira-engine -p MemoryMax=… -p CPUQuota=…`;
  otherwise admins set limits for all of a user's processes with a
  `user-.slice.d/` drop-in. Either way limits are per user, not per hub.
- **Ports** come from a configured range. **Port squatting**: before sending
  the token, the hub checks in `/proc/net/tcp{,6}` that the listening socket on
  that port belongs to the expected uid; on mismatch it picks another port and
  logs a security event.
- **Readiness**: the hub polls `GET /health` and `GET /version` (open per
  #149), then checks version compatibility.

### 7.3 Single engine per user

A person may have a hub-spawned engine, a Remote-SSH engine and a manually
started `mira gateway`, all pointing at the same `~/.mira`, possibly on
different hosts sharing an NFS home. Only one may run at a time.

- **Engine lease** (engine work item): on start the engine creates
  `~/.mira/engine.lease` with `O_EXCL` (safe on NFSv3+), containing host, pid,
  port and start time, and refreshes a heartbeat every 30 s. A lease whose
  heartbeat is older than 3 intervals is stale and may be taken over.
- A second start fails with a clear message naming the holder
  (`engine already running on host X, pid Y`).
- The hub and Remote-SSH desktop show that message and offer to stop the other
  engine. Stopping is done by the launcher or over SSH as the same user, so no
  extra privilege is needed.

## 8. Data, configuration and keys

### 8.1 Layout

```text
/var/lib/mira-hub/            # owner mira-hub, 0700
  hub.sqlite3                 # identities, sessions, memberships, engines, quotas, audit
  hub.toml                    # listen, auth (PAM service / LDAP), spawner, port range
  secrets/                    # hub signing key, team provider keys (LLM proxy)
  skills/                     # team skill library (§12)

~alice/.mira/                 # MIRA_HOME, owned by alice (may be on NFS)
  config.json                 # alice's own config, 0600 (personal keys)
  engine.lease                # single-engine lease (§7.3)
  logs/ media/ cron/ ui/ workspace/ skills/

$MIRA_RUNTIME_DIR             # local disk: /run/user/<uid>/mira or /var/lib/mira/run/<user>
  gateway-token, pid, sockets, temp files, local SQLite state

<any project dir alice can write>/   # e.g. ~/research/x or /data/lab/x
  .mira/                      # sessions/*.jsonl, memory, skills, token-usage.sqlite3
```

- Projects stay where users put them (`project_storage="user_selectable"`).
  Kernel permissions decide what each user can open, including group-shared
  lab directories.
- **NFS homes**: file locking and SQLite are unreliable on many NFS setups.
  The engine needs a `MIRA_RUNTIME_DIR` setting, separate from `MIRA_HOME`, for
  locks, sockets and transient state (engine work item). Per-project SQLite
  files (`token-usage.sqlite3`) on NFS must use a journal mode that is safe
  there, or move to the runtime dir with the project id as key; this is part
  of the same work item.

### 8.2 Configuration principle

The user owns `~/.mira/config.json` and can edit it outside Mira. Therefore:

- **Settings that protect the user** (sandbox, `restrict_to_workspace`) are
  defaults written for new users; a user who weakens them only affects
  themselves.
- **Settings that protect other users or the team** are never enforced through
  the user's config. They are enforced outside the engine:
  - loopback bind, token and allowed origins: launcher arguments and environment
  - file access: kernel permissions
  - team models, allowlist, spending: the hub LLM proxy
  - shared-project access: hub policy

In Team-server mode, config routes (`POST /api/config`, provider tests) still
write the user's own config; team settings live only in the hub admin API.

### 8.3 Provider keys

- **Personal keys** stay in the user's `~/.mira/config.json` (`0600`), exactly
  as in local mode. Only the owner and root can read them.
- **Team keys never enter any home directory.** An engine runs tools as the
  user, so anything readable by the user's uid is readable by the agent and by
  the user. Team models are therefore served by the **hub LLM proxy**:
  - the launcher configures an extra provider whose `api_base` is the hub
    proxy, authenticated with a per-engine proxy credential;
  - the proxy injects the real key, enforces the model allowlist and per-user
    quotas before the request, and records exact usage per user and project.
- The proxy is on the latency path; it streams responses without buffering.

## 9. Sharing (#155, #156)

### 9.1 File-level sharing: Unix groups

Teams already share data through group-owned directories. Mira does not
duplicate that: any user whose uid can read a directory can open it as a
project in their own engine. Because only one engine may host a project at a
time, each project gets a **project host lease** (`<project>/.mira/host.lease`,
same mechanism as §7.3). A second engine opening a hosted project gets it
read-only, or is redirected through the hub to the hosting engine.

### 9.2 Hub roles for live collaboration

- A shared project is hosted in its **owner's** engine.
- **Members** have one of three roles:

| Role | Read history and artifacts | Send messages | Run tools / workflows | Manage members, settings |
|---|---|---|---|---|
| viewer | ✓ | ✗ | ✗ | ✗ |
| editor | ✓ | ✓ (direct, or via approval if the owner enables it) | ✓ (or via approval) | ✗ |
| owner | ✓ | ✓ | ✓ | ✓ |

- Membership lives in the hub DB. Requests from members are routed to the
  owner's engine with a route and frame allowlist per role. Work runs under the
  owner's uid, so members act with the owner's file permissions only through
  the operations their role allows, never through a shell of their own.

### 9.3 Billing

All model usage in a shared project is charged to the **owner** (their keys or
their team-key quota), because it runs in the owner's engine. Usage records
carry the acting hub user from the attribution header.

### 9.4 Shared live sessions

Today the UI channel keeps **one** WebSocket per session
(`self._clients[session_id] = ws`). The engine changes to a set of sockets per
session and broadcasts to all of them (#156). This also fixes "two windows on
one session" for desktop users. On top, the hub adds:

- **Presence** events per session.
- **Attribution**: user messages carry the hub user id and display name.
- **Approval queue** (optional per project): editor messages and tool calls
  wait for the owner, reusing the existing confirmation patterns.
- **Turn arbitration**: a second sender while a turn runs gets "queued"
  feedback rather than an interleaved turn.

#150 (per-turn tool context isolation) must ship first.

## 10. Lifecycle and failure modes

| Event | Behaviour |
|---|---|
| First request of a user | Hub checks the engine lease, then spawns (cold start ~2–5 s); UI shows "starting your workspace". |
| Engine already running elsewhere | Lease held by a manual or Remote-SSH engine: the hub shows the holder and offers to stop it (as the same user via the launcher). |
| Idle culling | No HTTP or WS activity and no running workflow or cron job for `idle_timeout` (default 2 h) → hub stops the engine. Cron and heartbeat pin the engine as active (open question 3). |
| Engine crash | The systemd user unit (or the hub) restarts it; clients reconnect after a 1012 close; workflows resume from durable run state. |
| Hub restart | Engines keep running. The hub reloads its engine table, re-validates each listener's uid and `/health`, and clients reconnect. |
| Port squatted by another user | Listener uid mismatch → token not sent, new port chosen, security event logged. |
| NFS home unavailable | Engine fails to start or stalls; the hub reports "home directory unavailable" instead of retrying in a loop. |
| User removed from `mira-users` or disabled in LDAP | At the next group refresh the hub revokes sessions and stops the engine. Data stays in the home directory, managed by the host's normal policy. |
| Engine upgrade | The engine is installed system-wide (`/opt/mira`); engines pick up the new version on restart. The hub refuses incompatible `/version`s. |

## 11. Quotas, metering and audit (#157)

- **Team keys**: the LLM proxy meters every request exactly and enforces
  quotas before forwarding.
- **Personal keys**: the user pays their provider directly; the hub shows usage
  read from the engine's token-usage records through an engine API, not by
  reading the home directory.
- **Audit log**, metadata only, following `docs/runtime-diagnostics.md` privacy
  rules: logins, token issue and revoke, group-based access changes, shares and
  role changes, engine spawn and stop, port-squatting events, and per-turn tool
  summaries for shared projects (tool name, project, outcome; no content).

## 12. Team skill library (#158)

- The hub hosts a versioned library with publish, install and fork. Installing
  copies a skill into the user's project or workspace `.mira/skills/` through
  the user's engine, using the existing skill-plugin install routes.
- Skills declare required capabilities (`files`, `network`, `shell`, `mcp`);
  install requires confirmation.
- Later, hub accounts link to mira-community accounts for public sharing.

## 13. Deployment (#159) and the /v1 API (#160)

- **Reference deployment**:
  - TLS reverse proxy (Caddy or nginx)
  - `mira-hub` system service running as `mira-hub`
  - engine installed system-wide in `/opt/mira`, plus `engine-launch` and the
    one sudoers rule
  - `/etc/pam.d/mira-hub` (or LDAP settings in `hub.toml`)
- **Admin prerequisites**: a `mira-users` group (and optionally `mira-admins`);
  optionally `loginctl enable-linger` for users, if per-user systemd units are
  wanted.
- **Backup**: only `/var/lib/mira-hub/` is new. User data lives in home and
  project directories already covered by the host's backups.
- **`/v1` (OpenAI-compatible) API**: `mira serve` gets bearer-key auth (#160).
  In Team-server mode the hub exposes `/v1/*`, authenticates personal access
  tokens and proxies to the user's engine.

## 14. Threat model

| Threat | Mitigation |
|---|---|
| Unauthenticated network access to an engine | #149: token on all routes, loopback bind, Origin allowlist. Mode A only behind TLS or SSH. |
| Another local user connecting to an engine's loopback port | Token required; token passed via stdin/env, never argv; tool subprocesses don't inherit it. |
| Another local user squatting the assigned port to capture the token | Hub verifies the listener's uid in `/proc/net/tcp` before sending the token. |
| User A reads user B's files via the agent | Engine runs as A's uid; kernel permissions apply exactly as over SSH. bwrap and `restrict_to_workspace` additionally protect A from prompt injection. |
| Team key exfiltration | Team keys only in the hub LLM proxy, never in any home directory. |
| User weakens their own config | Affects only themselves; team protections are enforced outside the engine (§8.2). |
| Two engines writing one project or one `~/.mira` | Engine lease and project host lease (§7.3, §9.1). |
| Member escalating within a shared project | Hub route and frame allowlist per role; hub overwrites client `user_id`; owner approval queue. |
| Malicious web page driving an engine or the hub | Origin checks; `SameSite` cookie + CSRF header on the hub; no bearer tokens in cookies. |
| Stolen hub token | Short-lived access tokens, hashed storage, revocation on logout, disable or group removal. |
| Hub compromise | The hub runs unprivileged and can only invoke `engine-launch`. But it holds every engine token and can start engines as any `mira-users` member, so a compromised hub can act as those users through their agents (the same trust level as JupyterHub). Keep the hub minimal, patched and audited, and keep `mira-users` to people who need it. |
| Resource exhaustion by one user | Per-user systemd limits; quotas; idle culling. |
| Cross-session tool context bleed inside one engine | #150 fix; required before shared sessions. |

## 15. Open questions for the maintainer

Resolved in revision 2:

- ~~Spawner~~: run engines as the users' existing accounts through a sudo
  launcher; containers and batch schedulers later behind the same interface.
- ~~Identity source~~: existing server accounts via PAM (or LDAP bind);
  hub-managed accounts only as a fallback.
- ~~Team key exposure~~: personal keys in the user's home; team keys only
  through the hub LLM proxy.
- ~~Desktop in Team-server mode~~: thin client; hybrid later if needed.

Still open:

1. **External login after PAM/LDAP**: OIDC (which provider) or Feishu, and
   which claim maps to the Unix username?
2. **IM channels in server mode**: per-user bots (each engine with its own
   bot), or one team bot run by the hub that maps `sender_id` to users?
3. **Scheduled work and culling**: should cron and heartbeat keep a user's
   engine alive, or should the hub own scheduling and wake engines?
4. **Team-owned projects**: is a project always owned by one person, or do we
   need projects hosted in a service account's engine?
5. **Packaging**: a separate `mira-hub` distribution, or an extra
   `mira-engine[hub]`?
6. **Licensing and edition** of the server edition.
7. **Priority of Mode B vs the hub**: Remote-SSH is small and serves single
   users immediately; should it ship before the hub MVP?

## 16. Phased plan

```mermaid
flowchart TD
  S0["149 Step 0: engine token, loopback, Origin<br/>+ mira-ui 68"] --> SSH & HUB
  R["150 per-session tool context"] --> LIVE
  RT["runtime dir + engine lease<br/>(ticket to be filed)"] --> SSH & HUB
  SSH["Remote-SSH mode, engine + mira-ui<br/>(ticket to be filed)"]
  DOC["151 this design"] --> HUB
  HUB["152 Hub MVP: PAM auth, sudo launcher, proxy<br/>+ mira-ui 69 login"] --> ISO["153 isolation hardening<br/>(token env stripping, port checks, limits)"]
  HUB --> CFG["154 hub LLM proxy, team keys"]
  HUB --> SK["158 team skill library"]
  ISO --> SHARE["155 project sharing, roles, host lease<br/>+ mira-ui 70"]
  SHARE --> LIVE["156 shared live sessions<br/>+ mira-ui 70"]
  CFG --> QUOTA["157 quotas, metering, audit<br/>+ mira-ui 71"]
  HUB --> DEP["159 deployment, TLS, backup"]
  API["160 /v1 API auth"] --> DEP
```

| Phase | Tickets | Outcome |
|---|---|---|
| 0. Hardening | #149, mira-ui#68, #150, #160 | Engine safe to reach remotely behind TLS/SSH; no cross-session context bleed. |
| 1. Single-user remote | runtime dir + engine lease, Remote-SSH (tickets to be filed) | One person uses a server engine safely with SSH only. |
| 2. Hub MVP | #151, #152, mira-ui#69, #153 | Team members sign in with server accounts; each gets an engine as their own uid. |
| 3. Team keys | #154, #157, mira-ui#71 | Team models through the LLM proxy; quotas and audit. |
| 4. Collaboration | #155, #156, mira-ui#70 | Shared projects with roles; shared live sessions billed to the owner. |
| 5. Ecosystem and ops | #158, #159 | Team skill library; production deployment guide. |

Development happens on the long-lived `feat/server-edition` branch. Each ticket
gets its own branch and PR into it, and `dev` is merged into `feat/server-edition`
regularly. Tickets that also fix desktop or single-user behaviour (#149, #150,
#160, mira-ui#68, runtime dir + lease, Remote-SSH) target `dev` directly.
