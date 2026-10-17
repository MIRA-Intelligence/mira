# Mira Server: install and admin guide

Mira Server lets the people who already have accounts on a Linux host use Mira
from the desktop app without SSH. They sign in with their host password (PAM),
and Mira Server runs one engine per person **as that person's own uid**, then
proxies the app's `/api/*` and `/ws` traffic to it. It is Mode C in the
[server edition design](architecture.md); MIRA-Intelligence/mira#152 holds
the decisions and the client contract.

```text
desktop app ──HTTPS──> TLS proxy ──> mira-server (user mira-server, 127.0.0.1:8740)
                                       │  PAM / NSS (sign-in, groups)
                                       │  sudo -n -u alice engine-launch start
                                       ▼
                     alice's engine (uid alice, 127.0.0.1:18790, ~alice/.mira)
                     bob's engine   (uid bob,   127.0.0.1:<free port>, ~bob/.mira)
```

## How it works

- **Sign-in**: username and password are checked through the PAM service
  `mira-server`, so local accounts, LDAP/SSSD, password policy, expiry and
  lockouts apply as for SSH. Mira Server stores no passwords.
- **Access**: two independent groups (primary or supplementary, through NSS;
  both names are configurable). Members of `mira-users` get the `user` role: an
  engine. Members of `mira-admins` get the `admin` role: they manage the
  [team settings](#team-settings). Either one lets an account sign in. Group
  membership is re-read at least every `group_recheck_interval` seconds and on
  every token refresh. Someone removed from both groups loses all sessions at
  the next check; someone removed from `mira-users` loses their engine. Mira
  Server stops it through the `stop`-only sudo rule (see step 4).
- **Engines**: Mira Server runs the root-owned launcher through sudo:
  `sudo -n -u alice /usr/local/libexec/mira/engine-launch start`. The launcher
  runs `mira remote attach --json` as alice (the same command the desktop app
  uses over Remote-SSH, see [remote-ssh.md](../remote-ssh.md)), which starts or
  reuses alice's engine on `127.0.0.1` and reports its port and gateway token.
  A user therefore has one engine (the engine lease) whether Remote-SSH or Mira
  Server started it.
- **Listener check**: before Mira Server sends the engine token to a port, it
  checks in `/proc/net/tcp` and `/proc/net/tcp6` that every loopback listener on
  that port belongs to the user's uid. On a mismatch (another account squatting
  the port) it refuses, logs `security:` and writes a `security.listener_mismatch`
  audit event.
- **Proxy**: requests are forwarded with the engine token; clients never see it.
  WebSocket frames from the client get `user_id` set to the signed-in user.
- **Idle culling**: an engine Mira Server started is stopped after
  `idle_timeout` (default 2 h) with no proxied request and no open WebSocket.
  Engines Mira Server only attached to (for example a Remote-SSH engine the user
  started) are left alone. Cron jobs do not keep an engine alive yet.
- **Restarts**: engines keep running when Mira Server restarts. On start Mira
  Server asks each known user's launcher for `status` and re-attaches (after the
  listener and health checks) instead of assuming.

## Prerequisites

- Linux (PAM, sudo and `/proc/net/tcp` are required in production). Python 3.11+.
- sudo, and a TLS reverse proxy (Caddy or nginx) for remote clients.
- bubblewrap (`bwrap`) and unprivileged user namespaces for the agent sandbox.
- The users' home directories hold their Mira data (`~/.mira`), as with SSH.

## Install

The commands assume Debian/Ubuntu; adjust package names elsewhere.

### 1. Groups

```sh
sudo groupadd mira-users
sudo groupadd mira-admins          # optional
sudo gpasswd -a alice mira-users
sudo gpasswd -a admin-bob mira-admins        # administration account
sudo gpasswd -a admin-bob mira-users         # optional: lets it try the team settings
```

LDAP/SSSD groups work the same way, as long as `getent group mira-users` and
`id alice` show the membership (use `gpasswd -a`; `usermod -aG` fails for
directory accounts). Give administrators a separate account for administration
(`admin-bob` next to their everyday `bob`): what an administrator changes in the
app's settings applies to everyone.

### 2. The engine, system-wide

Install the engine and Mira Server into a root-owned virtual environment. Every
user's engine runs from it.

```sh
sudo python3 -m venv /opt/mira
sudo /opt/mira/bin/pip install 'mira-engine[server]'
```

The venv must stay writable by root only: the launcher runs its interpreter as
each user.

### 3. The mira-server account

```sh
sudo useradd --system --home-dir /var/lib/mira-server --create-home \
     --shell /usr/sbin/nologin mira-server
sudo chmod 700 /var/lib/mira-server
```

### 4. Launcher, sudoers rules and engine sessions

```sh
sudo /opt/mira/bin/mira-server install-launcher --prefix /usr/local \
     --engine-python /opt/mira/bin/python
```

This writes `/usr/local/libexec/mira/engine-launch` (root-owned, mode 0755) and
prints the sudoers lines Mira Server needs. Install the `mira-engine` PAM
service first, because the sudoers lines refer to it:

```sh
sudo cp deploy/mira-server/pam.d-mira-engine /etc/pam.d/mira-engine
sudo tee /etc/sudoers.d/mira-server >/dev/null <<'EOF'
Cmnd_Alias MIRA_ENGINE_START = /usr/local/libexec/mira/engine-launch start
Defaults!MIRA_ENGINE_START pam_service=mira-engine
mira-server ALL=(%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch
mira-server ALL=(ALL,!root) NOPASSWD: /usr/local/libexec/mira/engine-launch stop
EOF
sudo chmod 0440 /etc/sudoers.d/mira-server
sudo visudo -cf /etc/sudoers.d/mira-server
```

- `Cmnd_Alias`/`Defaults` run `engine-launch start` through the PAM service
  [`mira-engine`](../../deploy/mira-server/pam.d-mira-engine). Its session
  stack (`pam_systemd`) registers a logind session for the user, so the engine
  runs in the user's `user-<uid>.slice` like their SSH sessions, and the
  per-user limits below apply. `status` and `stop` open no session. See
  [Resource limits](#resource-limits).
- The first rule lets Mira Server start, inspect and stop the engines of
  `mira-users` members.
- The second allows only `engine-launch stop` (sudoers matches the argument
  exactly), as any account except root. Mira Server uses it to stop the engine
  of someone who was removed from `mira-users`; without it that engine keeps
  running as the user until it is stopped over SSH or the host restarts. The
  launcher itself also refuses to run as root.

The launcher accepts exactly one argument, `start`, `status` or `stop`, and no
other input. It discards the caller's environment, sets `HOME`, `USER`,
`MIRA_HOME=~/.mira` and `PATH` from the passwd entry of the uid sudo runs it as
(plus `XDG_RUNTIME_DIR=/run/user/<uid>` when that directory exists and is
private, so it shares the runtime dir with the user's SSH sessions), and runs:

| Action | Command run as the user |
|---|---|
| `start` | `/opt/mira/bin/python -I -m mira_engine remote attach --json --timeout 60` |
| `status` | `/opt/mira/bin/python -I -m mira_engine remote status --json` |
| `stop` | `/opt/mira/bin/python -I -m mira_engine remote stop --json` |

For `start` inside a logind session, the launcher prints the attach result and
then stays, with its output closed, until the engine exits (one such process
per engine). This keeps the session open: when a user's last session ends,
logind removes `/run/user/<uid>` and, with the default `RemoveIPC=yes`, the
user's shared memory and semaphores, which a running engine (and Python
multiprocessing in its tools) relies on. Mira Server reads the result line and
does not wait for the launcher to exit.

Re-run `install-launcher` after moving the engine install.

### 5. PAM service

Copy [`deploy/mira-server/pam.d-mira-server`](../../deploy/mira-server/pam.d-mira-server)
to `/etc/pam.d/mira-server`:

```text
@include common-auth
@include common-account
```

(RHEL/Fedora: `auth include password-auth` and `account include password-auth`.)

**Local `/etc/shadow` passwords.** `pam_unix` refuses to verify *another*
user's password from an unprivileged process unless that process can read
`/etc/shadow`. On Debian/Ubuntu add the account to the `shadow` group:

```sh
sudo usermod -aG shadow mira-server
```

This lets Mira Server read password hashes, which is a real privilege: keep the
host's password policy strong and Mira Server patched.

- **LDAP/SSSD**: accounts that come from `sssd` (`pam_sss`) need no extra group,
  and the `shadow` group does not affect them. On a host with both local and
  LDAP accounts, add the group for the local ones; the `common-auth` stack
  tries `pam_unix` first and falls through to `pam_sss` as usual.
- **RHEL/Fedora** have no `shadow` group (`/etc/shadow` is readable by root
  only), so only LDAP/SSSD accounts can sign in there for now. A small
  privileged helper for local passwords is a follow-up.
- The systemd unit does not set `SupplementaryGroups=shadow`, because the unit
  would fail to start on hosts without the group; `User=` picks up the
  membership added with `usermod`.

`mira-server check` warns when `/etc/shadow` is not readable.

### 6. Configuration

Copy [`deploy/mira-server/mira-server.toml`](../../deploy/mira-server/mira-server.toml)
to `/etc/mira-server/mira-server.toml` and set at least `public_url`. Every
setting is documented in that file. Unknown keys are rejected.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `server` | `listen` | `127.0.0.1:8740` | Keep it on loopback behind the TLS proxy. |
| | `public_url` | — | URL clients use; its origin is allowed for CORS. |
| | `data_dir` | `/var/lib/mira-server` | SQLite database (0700 dir, 0600 file). |
| | `allowed_origins` | `[]` | Empty: `file://`, `null` (the desktop app) and the `public_url` origin. A list replaces that. |
| | `trusted_proxies` | `[]` | Peers whose `X-Forwarded-For` gives the client IP for rate limiting. |
| `auth` | `backend` | `pam` | `dev` is for UI development (needs `--dev`). |
| | `pam_service` | `mira-server` | `/etc/pam.d/<name>` |
| | `users_group` / `admins_group` | `mira-users` / `mira-admins` | |
| | `access_token_ttl` / `refresh_token_ttl` | `900` / `2592000` | Seconds. |
| | `group_recheck_interval` | `300` | Seconds between NSS re-checks per user. |
| | `login_max_attempts` / `login_window` | `5` / `300` | Failed sign-ins per (username, client IP) per window. |
| `spawner` | `backend` | `sudo` | `local` is for UI development (needs `--dev`). |
| | `launcher` | `/usr/local/libexec/mira/engine-launch` | |
| | `sudo` | `/usr/bin/sudo` | |
| | `start_timeout` / `stop_timeout` | `90` / `30` | Seconds to wait for the launcher. |
| | `idle_timeout` | `7200` | Seconds; `0` disables culling. |
| | `verify_listener` | `true` | `false` needs `--dev`. |

### 7. Host policy and agent sandbox

```sh
sudo apt-get install bubblewrap
sudo install -D -o root -g root -m 0644 deploy/mira-server/policy.toml /etc/mira/policy.toml
```

[`deploy/mira-server/policy.toml`](../../deploy/mira-server/policy.toml) keeps
file and shell tools inside the current project and runs every shell command in
the bubblewrap sandbox. See [Agent isolation](#agent-isolation).

Ubuntu 23.10+ restricts unprivileged user namespaces through AppArmor, and
bwrap then fails with `setting up uid map: Permission denied` (`mira-server
check` reports it, and agents' shell commands are refused). Allow user
namespaces for bwrap only, with
[`deploy/mira-server/apparmor-bwrap`](../../deploy/mira-server/apparmor-bwrap)
(or the distribution's own bwrap profile, if it ships one):

```sh
sudo install -m 0644 deploy/mira-server/apparmor-bwrap /etc/apparmor.d/mira-bwrap
sudo apparmor_parser -r /etc/apparmor.d/mira-bwrap
```

### 8. Check and start

```sh
sudo -u mira-server /opt/mira/bin/mira-server check --config /etc/mira-server/mira-server.toml
```

`check` validates the config, the PAM service file, the groups, the launcher's
ownership and permissions (and of every directory above it), the engine
interpreter, the sudo rules (`sudo -n -l`; a missing `stop` rule is a warning),
the data dir, the host policy and whether the bwrap sandbox works.

Run Mira Server with systemd using
[`deploy/mira-server/mira-server.service`](../../deploy/mira-server/mira-server.service).
Keep `KillMode=process`: user engines are started through sudo and must survive
a Mira Server restart. Mira Server refuses to run as root.

```sh
sudo cp deploy/mira-server/mira-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mira-server
```

### 9. TLS reverse proxy

Mira Server speaks plain HTTP on loopback. Put a TLS proxy in front, forward
WebSocket upgrades, disable response buffering (streaming and SSE), and allow
large request bodies (uploads). Set `trusted_proxies = ["127.0.0.1", "::1"]` so
rate limiting sees real client IPs.

`<img>`, `<iframe>`, download and WebSocket URLs carry the access token as
`?access_token=`, so **the proxy's access log must not record query strings**.
Mira Server itself never logs query strings or tokens. Access tokens expire
after 15 minutes (`access_token_ttl`), which limits what a leaked log line is
worth, but keep them out of logs anyway. The examples do this:

- [`deploy/mira-server/nginx.conf`](../../deploy/mira-server/nginx.conf): a
  `log_format` that records `$uri` (the path) instead of `$request`, which the
  default `combined` format uses and which includes the query string. nginx's
  *error* log still quotes the full request line when an upstream request fails
  (for example while Mira Server restarts), so keep `/var/log/nginx` readable
  by administrators only, as distributions do by default.
- [`deploy/mira-server/Caddyfile`](../../deploy/mira-server/Caddyfile): Caddy
  writes no access log unless a site has a `log` block. The example has one with
  a filter that deletes `access_token` from the logged URI. Caddy already redacts
  `Authorization` and `Cookie` headers.

CI checks both examples (`nginx -t`, `caddy validate`) and runs requests through
nginx to confirm the token never reaches its access log.

## Several users on one host

- Each engine binds `127.0.0.1`. The first one takes port 18790; later ones get
  a random free port (`mira remote attach` picks one when 18790 is taken), so any
  number of users can run engines side by side. Mira Server reads each port from
  the launcher output and verifies its owner before use.
- The engine lease (one engine per `~/.mira`) also covers engines started over
  Remote-SSH or by hand. Mira Server reuses such an engine when it can attach to
  it. If it cannot (an engine on another host sharing an NFS home, one started
  with another token, or one bound to a public address), the engine state is
  `conflict` and `/api/*` answers 409 `engine_conflict`; the user can stop it
  with `POST /server/engine/stop` or over SSH.
- On hosts with systemd-logind, consider `loginctl enable-linger <user>` (see
  [remote-ssh.md](../remote-ssh.md)) so logging out of SSH does not affect the
  engine.

## Resource limits

Engines Mira Server starts run in the user's `user-<uid>.slice` (see step 4),
so systemd's per-user limits cover them together with the user's SSH sessions:
one budget per person. Set them for all users with a drop-in such as
[`deploy/mira-server/user-slice-mira.conf`](../../deploy/mira-server/user-slice-mira.conf):

```sh
sudo install -D -m 0644 deploy/mira-server/user-slice-mira.conf \
     /etc/systemd/system/user-.slice.d/50-mira.conf
sudo systemctl daemon-reload
```

```ini
[Slice]
MemoryMax=16G
MemorySwapMax=0
CPUQuota=400%
TasksMax=4096
```

Use `/etc/systemd/system/user-<uid>.slice.d/` for one user. `pam_limits`
(`/etc/security/limits.conf`) also applies to the engine session. Mira Server
logs a warning and an `engine.outside_user_slice` audit event when an engine
runs outside its user's slice (for example when the `mira-engine` PAM service
is not set up), and `mira-server check` reports a missing setup.

## Agent isolation

Users are separated by their OS accounts: each engine runs as its user, so the
kernel applies the same permissions as over SSH. On top of that, the **host
policy** `/etc/mira/policy.toml` protects each user's agent from prompt
injection and limits what agents may do. Every engine on the host reads it,
whether Mira Server, Remote-SSH or the user started it; its values override the
user's `~/.mira/config.json`, and the app cannot change them (`POST /api/config`
answers 403 `locked_by_policy`; `GET /api/config` and `GET /api/status` list
the enforced settings under `policy`). It is not a boundary against the user,
who can run any program as themselves. An invalid file stops engines from
starting.

| Setting | Effect |
|---|---|
| `tools.restrict_to_workspace = true` | File tools and the shell tool's paths and `working_dir` stay inside the current project. |
| `tools.exec = false` | No shell tool. |
| `tools.mcp = false` | MCP servers in users' configs are not started. MCP servers run unsandboxed as the user. |
| `tools.plugin_install = false` | Skill plugins cannot be installed or removed (403). |
| `sandbox.mode = "bwrap"` | Every shell command, background job and project venv setup (`uv sync`) runs in bubblewrap. If bwrap does not work, commands are refused, never run unsandboxed. |
| `sandbox.network = "none"` | Sandboxed commands get a private network namespace (no network at all). |
| `sandbox.read_only_paths`, `read_write_paths` | Extra host paths visible in the sandbox (datasets, software, scratch). |
| `[[groups]]` with `name = "<group>"` | Overrides for members of a Unix group, applied in file order; can relax the base policy. |

Inside the sandbox a command sees: system directories read-only (`/usr`,
`/etc`, `/opt`, ...), its own `/proc` (pid namespace), `/dev` with GPU device
nodes, a private `/tmp`, the project read-write, skills and uploaded media
read-only, uv's cache and interpreters, and a persistent `$HOME` backed by
`~/.mira/sandbox/home` (pip caches, `pip --user`). The Mira home itself
(config with provider keys, the gateway token) is hidden, as are other
directories, including the user's real home and other users' homes. The shell
runs inside without a login profile and with the exec tool's curated
environment, which never contains credentials (`*KEY*`, `*TOKEN*`, `*SECRET*`,
`*PASSWORD*`, `*COOKIE*`).

With network access, sandboxed commands can connect to other users' engines on
loopback, which refuse requests without that engine's token. `mira remote
attach` checks that the engine's port is held by the engine process before it
sends the token, so another account squatting the port receives nothing
(`listener_mismatch`).

## Team settings

Administrators (`mira-admins`) set up model access for everyone from the
desktop app: they sign in to the Team Server and use the usual settings pages.
For an administrator's session Mira Server answers the settings requests
(`/api/config`, `/api/providers/{name}/models` and `/test`) itself, against the
**team settings** in `/var/lib/mira-server/team-config.json` (mode 0600, owned
by `mira-server`), instead of the administrator's own config.

| Groups | Engine | The settings pages change |
|---|---|---|
| `mira-users` | yes | the user's own `~/.mira/config.json` |
| `mira-users` and `mira-admins` | yes | the team settings |
| `mira-admins` | no (other pages answer 403 `no_engine`) | the team settings |

An administration account that is also in `mira-users` has an engine like
everyone else, so the administrator can try the team settings the way users get
them (its usage counts for that account).

What the team settings do in each user's engine:

- **Team providers** (a provider with an API key or endpoint in the team
  settings, and turned on) replace the user's own entry for that provider. Users
  see them as managed by the team and cannot change them (`403 locked_by_team`).
  For custom providers this is per provider: users can still add their own.
- Providers the team does not configure, or turns off, stay each user's own,
  to use with their own key. Turning a provider off on the administrator's
  Providers page does not turn it off for users.
- **Defaults** (provider and model, reasoning effort, temperature, iteration
  limits, team-role models) apply until a user picks their own value.
- **Model parameter rules**, if the team sets any, replace the users' rules.
- The host policy (`/etc/mira/policy.toml`) still applies on top. The projects
  folder and `restrict_to_workspace` are not team settings.

**Team keys never reach users' engines.** Mira Server gives each engine the
team settings when the engine starts (or Mira Server attaches to it) and again
after every change, with each team provider pointing at Mira Server's LLM
proxy, `http://127.0.0.1:8740/llm/<provider>`, and the engine's own token as
its API key. The proxy accepts only loopback requests that did not come through
the TLS proxy (no `X-Forwarded-For` or other forwarding header) and that carry the token of an engine Mira
Server runs for a current `mira-users` member. It replaces that token, in
whichever header the provider client sent it, with the team key and streams
the provider's answer back. The provider must be reachable from the Mira Server
host (`providers.proxy` in the team settings is used for these requests).

Changes are recorded in the audit log as `team.config_changed` with the
account and the changed settings (never values).

Limitations: providers that sign in per user (OpenAI Codex, GitHub Copilot)
cannot be team providers; a provider without a known default endpoint needs an
API Base in the team settings; engines older than this feature ignore the team
settings (logged as `team.push_unsupported`).

## Dev mode (UI development only)

For working on the desktop client without a Linux host or real accounts:

```toml
[server]
listen = "127.0.0.1:8740"
data_dir = "/tmp/mira-server-dev"

[auth]
backend = "dev"

[[auth.dev_users]]
name = "alice"
password_hash = "scrypt$16384$8$1$..."   # mira-server hash-password
admin = true          # team settings; add `user = false` for no engine

[spawner]
backend = "local"
local_mira_home = "/tmp/mira-dev/{user}"  # one MIRA_HOME (and engine) per dev user
```

```sh
mira-server serve --config dev.toml --dev
```

`--dev` is required for `auth.backend = "dev"`, `spawner.backend = "local"` and
`verify_listener = false`, and only works on a loopback listen address. Dev mode
also allows the Vite dev server origins `http://localhost:5173` and
`http://127.0.0.1:5173`. Every dev user's engine runs as you: there is no
isolation.

## HTTP API (protocol 1)

All bodies are JSON. Errors are `{"error": "<code>", "message": "..."}`. Every
response carries `X-Mira-Request-Id`.

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /server/info` | none | `{"product": "mira-server", "version", "protocol": 1, "auth": {"methods": ["password"]}}` |
| `POST /server/auth/login` | none | `{"username", "password", "client"}` → tokens + `user` |
| `POST /server/auth/refresh` | none | `{"refresh_token"}` → same shape as login; the refresh token rotates |
| `POST /server/auth/logout` | bearer, or `{"refresh_token"}` | Revokes the session (refresh family). 204. |
| `GET /server/me` | bearer | `{"user": {...}, "engine": {"state", "version", "message"}}` |
| `POST /server/engine/start` | bearer | Starts the engine; returns the `engine` object (state may still be `starting`) |
| `POST /server/engine/stop` | bearer | Stops the engine; returns the `engine` object |
| `/api/*` | bearer (or `?access_token=` on GET/HEAD) | Proxied to the caller's engine; for administrators the settings endpoints answer from the [team settings](#team-settings) (`"scope": "team"`) |
| `/llm/<provider>/*` | an engine's token (loopback only) | The [LLM proxy](#team-settings) for team providers |
| `GET /ws`, `GET /ws/*` | bearer or `?access_token=` | WebSocket proxy to the same path on the engine (`/ws`, `/ws/workflow/v1`) |

Token response:

```json
{"access_token": "msa_...", "expires_in": 900, "refresh_token": "msr_...",
 "refresh_expires_in": 2592000,
 "user": {"name": "alice", "display_name": "Alice", "roles": ["user"]}}
```

Engine states: `stopped`, `starting`, `running`, `error`, `conflict`.

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | Malformed body. |
| 401 | `unauthorized` | Missing, invalid or expired token; wrong username or password. |
| 403 | `forbidden` | In neither `mira-users` nor `mira-admins` (or removed; all sessions are revoked). |
| 403 | `no_engine` | An account only in `mira-admins` asked for an engine. |
| 403 | `origin_not_allowed` | A browser `Origin` that is not allowed. |
| 409 | `engine_conflict` | The engine lease is held by an engine Mira Server cannot attach to. |
| 429 | `too_many_attempts` | Login rate limit; see `Retry-After`. |
| 503 | `engine_starting` | The engine is (re)starting; `Retry-After: 2`. |
| 503 | `engine_unavailable` | The engine failed to start; `message` says why. Retried after a short backoff or on `POST /server/engine/start`. |
| 503 | `unavailable` | Account lookup (NSS/directory) failed; transient. |

Details:

- **Refresh**: 400/401/403 only for definitive problems (malformed request,
  invalid, expired, revoked or replayed token, account no longer allowed). The
  desktop app signs out on those and keeps its tokens on anything else (5xx).
  The refresh token is consumed only after the account check passed, so a
  transient failure never turns the next retry into a replay. Replaying a
  consumed refresh token revokes the whole session.
- **Starting on demand**: `/api/*`, `/ws` and `/ws/*` start a stopped engine.
  If it is not ready within about 1.5 s they answer 503 `engine_starting` with
  `Retry-After: 2` (for WebSockets, before the upgrade); the client polls
  `/server/me` or calls `/server/engine/start`. If the engine restarted with a
  new token or stopped answering, Mira Server re-attaches and answers
  `engine_starting` meanwhile.
- **Forwarded headers**: Mira Server drops the client's `Authorization`,
  `Cookie`, `Origin`, hop-by-hop headers and any `X-Mira-Server-*` /
  `X-Mira-Request-Id`, and adds `Authorization: Bearer <engine token>`,
  `X-Mira-Server-User`, `X-Mira-Server-Role` (`user` or `admin`) and its own
  `X-Mira-Request-Id`. `access_token` is removed from the query. Request and
  response bodies are streamed. `Set-Cookie` and the engine's own CORS headers
  are dropped from responses.
- **WebSockets**: `user_id` in every JSON-object client frame is replaced with
  the signed-in username on all `/ws*` paths (the workflow socket ignores
  unknown keys). Other frames pass through unchanged. When the engine side
  closes (restart, crash) the client socket is closed with 1012; when the user
  stops the engine, with 1001.
- **CORS**: allowed origins get `Access-Control-Allow-Origin` (echoed),
  `Access-Control-Expose-Headers: X-Mira-Request-Id, Retry-After,
  Content-Disposition`, and preflights allow `Authorization`, `Content-Type`,
  `X-Mira-Request-Id` (plus any other requested header). Tokens are bearer
  tokens; there are no cookies, so `null` (packaged Electron) is accepted.

## Audit log

The `audit` table in `mira-server.sqlite3` records metadata only: sign-ins
(`login.ok`, `login.failed`, `login.denied`, `login.rate_limited`), `logout`,
`access.revoked`, engine `started`/`attached`/`stopped`/`start_failed`/
`stop_failed`/`forgotten`/`unhealthy`/`revoked`, team settings
(`team.config_changed`, `team.push_failed`, `team.push_unsupported`), and security events
(`security.refresh_reuse`, `security.listener_mismatch`). It never contains
passwords, tokens, prompts or request bodies.

## Limitations (follow-ups)

- **Resource limits for engines started over Remote-SSH** come from the
  user's SSH session (also `user-<uid>.slice`); an engine started by hand with
  `nohup` outside any session gets none.
- **MCP servers** run outside the sandbox; disable them with `tools.mcp = false`
  where that matters.
- **Usage metering and quotas** for team providers (#157), and personal keys
  encrypted at rest.
- **Local passwords on RHEL/Fedora**: there is no `shadow` group, so only
  LDAP/SSSD accounts can sign in until a privileged password helper exists.
- **`Origin: null`**: the packaged desktop app sends `Origin: null`, which any
  sandboxed page can also send. This is acceptable while all authentication is
  by bearer token (a page cannot obtain one without the user's password); it
  will be revisited when browser sign-in with cookies is added.
- Browser sign-in with cookies, LDAP bind without PAM, and OIDC come later.
