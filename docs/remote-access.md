# Remote Access to an Engine

This guide covers using an engine that runs on another machine (a lab server,
a workstation, a Docker host) from the desktop app or the web UI.

> **Deprecated: direct remote connections.** Pointing the desktop app's remote
> engine settings at `http://<server>:18790` / `ws://<server>:18790` over a
> network sends your prompts, files, results and the gateway token in
> plaintext, and requires the engine to listen on a non-loopback address.
> This mode is being phased out (see [Deprecation timeline](#deprecation-timeline)).
> Use an SSH tunnel today, and Remote-SSH mode once it is released.

## Recommended: SSH tunnel (works today)

Keep the engine bound to loopback on the server and let SSH carry the traffic.
Authentication and encryption come from your existing SSH account; no engine
port is opened to the network.

1. On the server, run the gateway on loopback (the default since
   [gateway-auth.md](gateway-auth.md)):

   ```bash
   mira gateway                 # binds 127.0.0.1:18790
   mira gateway-token           # prints the token for the client
   ```

   If an older `~/.mira/config.json` still contains `"gateway": {"host": "0.0.0.0"}`,
   change it to `127.0.0.1` (or start with `--host 127.0.0.1`).

2. On your computer, open the tunnel and keep it running:

   ```bash
   ssh -N -L 18790:127.0.0.1:18790 user@server
   ```

   `~/.ssh/config` aliases, keys, `ssh-agent` and `ProxyJump` work as usual.

3. In the desktop app's remote engine settings, use:

   | Setting | Value |
   | --- | --- |
   | API URL | `http://127.0.0.1:18790/api` |
   | WebSocket URL | `ws://127.0.0.1:18790/ws` |
   | Engine token | output of `mira gateway-token` on the server |

   The `http://` here only travels over your own loopback interface; the
   network hop is inside the SSH connection.

The engine keeps running on the server when the tunnel drops (laptop sleep,
network change); reopen the tunnel and the app reconnects.

## Coming next: Remote-SSH mode

Remote-SSH mode automates the tunnel, similar to VS Code Remote-SSH: the
desktop app connects with the system `ssh` client, starts or attaches to your
engine on the server (`mira remote attach`), forwards the port, reads the token
and reconnects after network loss. Tracking: #166 (engine) and
MIRA-Intelligence/mira-ui#73 (desktop). Once released it replaces both the
manual tunnel and direct remote connections.

Teams that need shared access, web login with server accounts, sharing and
quotas will use Mira Server (see
[server-edition/architecture.md](server-edition/architecture.md)).

## If the engine must be reachable over a network: TLS only

When an SSH tunnel is not possible, never expose the plaintext port. Keep the
engine on loopback and put a TLS reverse proxy on the same host. A minimal
[Caddy](https://caddyserver.com/) site (certificates and WebSocket upgrades
are handled automatically):

```caddyfile
mira.example.com {
    reverse_proxy 127.0.0.1:18790
}
```

Clients then use `https://mira.example.com/api` and `wss://mira.example.com/ws`
with the gateway token. A browser-based UI served from another origin must be
added to `MIRA_GATEWAY_ALLOWED_ORIGINS` (see [gateway-auth.md](gateway-auth.md#browser-origins)).

## Deprecation timeline

| Stage | When | Engine | Desktop app |
| --- | --- | --- | --- |
| 1. Warn | Now | Gateway token required, loopback bind by default, startup warning on non-loopback binds; the Docker stack publishes ports on the host's loopback only. | Warns when a remote URL is plaintext `http`/`ws` to a non-loopback host (MIRA-Intelligence/mira-ui#74). |
| 2. Restrict | When Remote-SSH ships | Binding a non-loopback address requires an explicit opt-in, intended only for containers behind a TLS proxy or Mira Server. | Manual remote connections accept only loopback (tunnel) or `https`/`wss` URLs; plaintext profiles are offered a migration to Remote-SSH. |
| 3. Remove | At least one release after stage 2 | The opt-in bind remains only for container deployments. | The manual remote engine connection is removed; connection modes are local, Remote-SSH and team server (Mira Server). |

Engine-side tracking: #167. Design: [server-edition/architecture.md](server-edition/architecture.md) §2.
