# Remote-SSH engine commands

Remote-SSH (Mode B in [the server edition design](server-edition/architecture.md))
lets the desktop app use an engine on a server through the user's own SSH
account, like VS Code Remote-SSH. The engine listens only on `127.0.0.1` and the
desktop reaches it through `ssh -L`. Nothing new is exposed on the network.

These commands are what the desktop app runs over SSH. They also work by hand.

```sh
mira remote attach --json   # start or reuse this user's engine, print port + token
mira remote status --json   # is an engine running for this MIRA_HOME?
mira remote stop            # stop a standalone or Remote-SSH engine
```

## Contract (protocol 1)

The desktop app runs:

```sh
ssh <host> -- sh -lc 'mira remote attach --json'
```

With `--json`, stdout carries exactly **one line of JSON** and nothing else. All
logs go to stderr.

Success (exit code 0):

```json
{"protocol": 1, "ok": true, "port": 18790, "token": "<gateway token>",
 "version": "0.4.0", "pid": 12345, "mira_home": "/home/alice/.mira", "started": true}
```

- `started` is `true` when this call started the engine and `false` when it reused a running one.
- `port` is the engine's port on the server's `127.0.0.1`. The desktop forwards it with
  `ssh -N -L <local>:127.0.0.1:<port> <host>`.
- `token` is the gateway token ([gateway auth](gateway-auth.md)). Keep it in memory only.

Failure (exit code 1):

```json
{"protocol": 1, "ok": false, "error": {"code": "start_failed", "message": "..."}}
```

| `code` | Meaning |
| --- | --- |
| `start_failed` | The engine exited during startup (message carries the last log line), or the port was answered by another process. |
| `health_timeout` | The engine did not answer `/health` in time (`--timeout`, default 30 s). It may still be starting; retry. |
| `lease_conflict` | The engine for this `MIRA_HOME` runs on another host sharing the home directory, or (for `stop`) it is managed by a system service. |
| `config_invalid` | The Mira config cannot be loaded, the running engine rejects the local token, or it listens on an address an SSH tunnel to `127.0.0.1` cannot reach. |
| `internal` | Anything unexpected. |

`mira remote status --json` returns `{"protocol": 1, "ok": true, "running": false, "mira_home": ...}`
or the success shape above plus `"running": true` and `"hostname"`. The token is
included only when the engine runs on this host. `mira remote stop --json`
returns `{"protocol": 1, "ok": true, "message": ...}` and exits 0 when the engine
stopped or was not running.

## Behaviour

- **One engine per `MIRA_HOME`.** Attach uses the single-engine lease
  ([service lifecycle](service-lifecycle.md)). It reuses a healthy engine whatever
  started it (system service, `mira gateway`, an earlier attach), as long as it
  listens on loopback or all interfaces.
- **Starting.** Attach runs `mira gateway --host 127.0.0.1` on port 18790, or on a
  random free port when 18790 is taken, and waits for `/health`. An engine started
  this way refuses any non-loopback bind.
- **Detached.** The engine runs in its own session (`setsid`; on Windows a detached
  process group), so closing SSH or letting the laptop sleep does not stop it. Its
  output goes to `<MIRA_HOME>/logs/remote-gateway.log`.
- **Stopping.** `mira remote stop` sends SIGTERM to a standalone or Remote-SSH
  engine after checking its PID belongs to a Mira gateway. Service-managed engines
  are left to `mira-engine stop`.

## Making `mira` available to non-interactive SSH

`ssh host -- sh -lc '...'` starts a login shell, which reads `~/.profile` (and,
for bash, `~/.bash_profile` or `~/.bash_login`). It does **not** read `~/.bashrc`,
where conda and many venv setups put their PATH changes. Check with:

```sh
ssh <host> -- sh -lc 'command -v mira && mira --version'
```

If that fails, pick one:

- Install with `pipx install mira-engine` (or `uv tool install mira-engine`). Both
  put `mira` in `~/.local/bin`, which most distributions add to PATH in `~/.profile`.
- Symlink the venv's entry point: `ln -s /path/to/venv/bin/mira ~/.local/bin/mira`.
- Add the directory to PATH in `~/.profile`, not `~/.bashrc`.

## Servers with systemd-logind

`/run/user/<uid>` (the default runtime dir) is removed when the user's last
login session ends. On some distributions, logind also kills the user's processes
then (`KillUserProcesses=yes`). Either can surprise a long-running engine. To
avoid both, run:

```sh
loginctl enable-linger $USER
```

Alternatively, set `MIRA_RUNTIME_DIR` to a persistent local directory.
