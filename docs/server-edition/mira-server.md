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
- **Access**: members of `mira-users` (primary or supplementary group, through
  NSS) may sign in; members of `mira-admins` also get the `admin` role. Both
  names are configurable. Group membership is re-read at least every
  `group_recheck_interval` seconds and on every token refresh. Someone removed
  from `mira-users` loses all sessions at the next check, and Mira Server stops
  their engine (through the `stop`-only sudo rule, see step 4).
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
- The users' home directories hold their Mira data (`~/.mira`), as with SSH.

## Install

The commands assume Debian/Ubuntu; adjust package names elsewhere.

### 1. Groups

```sh
sudo groupadd mira-users
sudo groupadd mira-admins          # optional
sudo usermod -aG mira-users alice
sudo usermod -aG mira-users,mira-admins bob
```

LDAP/SSSD groups work the same way, as long as `getent group mira-users` and
`id alice` show the membership.

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

### 4. Launcher and sudoers rule

```sh
sudo /opt/mira/bin/mira-server install-launcher --prefix /usr/local \
     --engine-python /opt/mira/bin/python
```

This writes `/usr/local/libexec/mira/engine-launch` (root-owned, mode 0755) and
prints the two sudoers rules Mira Server needs:

```sh
sudo tee /etc/sudoers.d/mira-server >/dev/null <<'EOF'
mira-server ALL=(%mira-users) NOPASSWD: /usr/local/libexec/mira/engine-launch
mira-server ALL=(ALL,!root) NOPASSWD: /usr/local/libexec/mira/engine-launch stop
EOF
sudo chmod 0440 /etc/sudoers.d/mira-server
sudo visudo -cf /etc/sudoers.d/mira-server
```

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
private, so it shares the runtime dir with the user's SSH sessions), and execs:

| Action | Command run as the user |
|---|---|
| `start` | `/opt/mira/bin/python -I -m mira_engine remote attach --json --timeout 60` |
| `status` | `/opt/mira/bin/python -I -m mira_engine remote status --json` |
| `stop` | `/opt/mira/bin/python -I -m mira_engine remote stop --json` |

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

### 7. Check and start

```sh
sudo -u mira-server /opt/mira/bin/mira-server check --config /etc/mira-server/mira-server.toml
```

`check` validates the config, the PAM service file, the groups, the launcher's
ownership and permissions (and of every directory above it), the engine
interpreter, the sudo rules (`sudo -n -l`; a missing `stop` rule is a warning)
and the data dir.

Run Mira Server with systemd using
[`deploy/mira-server/mira-server.service`](../../deploy/mira-server/mira-server.service).
Keep `KillMode=process`: user engines are started through sudo and must survive
a Mira Server restart. Mira Server refuses to run as root.

```sh
sudo cp deploy/mira-server/mira-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mira-server
```

### 8. TLS reverse proxy

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
admin = true

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
| `/api/*` | bearer (or `?access_token=` on GET/HEAD) | Proxied to the caller's engine |
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
| 403 | `forbidden` | Not in `mira-users` (or removed from it; all sessions are revoked). |
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
`stop_failed`/`forgotten`/`unhealthy`, and security events
(`security.refresh_reuse`, `security.listener_mismatch`). It never contains
passwords, tokens, prompts or request bodies.

## Limitations (follow-ups)

- **Isolation hardening (#153)**: per-user resource limits, stripping the
  engine token from tool subprocess environments, and port-squatting defence
  inside `mira remote attach` itself (it checks the engine with the token before
  Mira Server's own listener check).
- **Team keys and the LLM proxy (#154)**.
- **Local passwords on RHEL/Fedora**: there is no `shadow` group, so only
  LDAP/SSSD accounts can sign in until a privileged password helper exists.
- **`Origin: null`**: the packaged desktop app sends `Origin: null`, which any
  sandboxed page can also send. This is acceptable while all authentication is
  by bearer token (a page cannot obtain one without the user's password); it
  will be revisited when browser sign-in with cookies is added.
- Browser sign-in with cookies, LDAP bind without PAM, and OIDC come later.
