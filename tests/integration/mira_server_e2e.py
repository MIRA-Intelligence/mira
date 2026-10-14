#!/usr/bin/env python3
"""Mira Server end-to-end check on a real multi-user Linux host (CI acceptance test).

Run by .github/workflows/mira-server-integration.yml after it has created the
accounts, installed the engine in /opt/mira, the launcher, the sudoers rule and
the PAM service, and started ``mira-server serve`` as the ``mira-server`` user.
The caller needs passwordless sudo (GitHub's ubuntu runners have it) to inspect
other users' processes and files and to restart Mira Server.

Not collected by pytest (the file name does not start with ``test_``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import aiohttp

USERS = {"alice": "alice-Pw-1", "bob": "bob-Pw-2", "carol": "carol-Pw-3"}
STEP = 0


def step(message: str) -> None:
    global STEP
    STEP += 1
    print(f"\n[{STEP}] {message}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"    ok: {message}", flush=True)


def sudo(*args: str, user: str | None = None, check_rc: bool = True) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n"] + (["-u", user] if user else []) + ["--", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if check_rc and result.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed ({result.returncode}): {result.stderr.strip()}")
    return result


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def request(self, method: str, path: str, *, token: str | None = None, body: Any = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                status, resp_headers = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status, resp_headers = exc.code, dict(exc.headers)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")
        return status, resp_headers, payload

    def login(self, user: str, password: str | None = None) -> tuple[int, Any]:
        status, _, payload = self.request("POST", "/server/auth/login", body={
            "username": user, "password": password if password is not None else USERS[user], "client": "e2e"})
        return status, payload

    def api_until_ready(self, path: str, token: str, timeout: float = 120) -> Any:
        """Call /api/* until the engine has started (503 engine_starting meanwhile)."""
        deadline = time.monotonic() + timeout
        seen_starting = False
        while time.monotonic() < deadline:
            status, headers, payload = self.request("GET", path, token=token)
            if status == 200:
                return payload, seen_starting
            if status == 503 and isinstance(payload, dict) and payload.get("error") == "engine_starting":
                seen_starting = True
                assert headers.get("Retry-After") == "2", headers
                time.sleep(1)
                continue
            raise AssertionError(f"GET {path}: {status} {payload}")
        raise AssertionError(f"GET {path}: engine not ready after {timeout}s")


def lease(user: str) -> dict[str, Any]:
    return json.loads(sudo("cat", f"/home/{user}/.mira/engine.lease").stdout)


def process_owner(pid: int) -> str:
    return subprocess.run(["ps", "-o", "user=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def process_uid(pid: int) -> int:
    with open(f"/proc/{pid}/status", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("Uid:"):
                return int(line.split()[1])
    raise AssertionError(f"no Uid line for pid {pid}")


def pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def wait_server(client: Client, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, payload = client.request("GET", "/server/info")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise AssertionError("Mira Server did not come up")


async def ws_roundtrip(base: str, token: str, path: str = "/ws") -> Any:
    url = base.replace("http", "ws", 1) + f"{path}?access_token={token}"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, headers={"Origin": "file://"}, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
            await ws.send_str("not json")  # the engine answers with an error frame
            msg = await ws.receive(timeout=30)
            return msg.type, msg.data


async def ws_status(base: str, token: str, origin: str) -> int:
    url = base.replace("http", "ws", 1) + f"/ws?access_token={token}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(url, headers={"Origin": origin}):
                return 101
        except aiohttp.WSServerHandshakeError as exc:
            return exc.status


def restart_server(args: argparse.Namespace, client: Client) -> None:
    sudo("pkill", "-TERM", "-u", "mira-server", "-f", "mira-server serve", check_rc=False)
    for _ in range(50):
        if sudo("pgrep", "-u", "mira-server", "-f", "mira-server serve", check_rc=False).returncode != 0:
            break
        time.sleep(0.2)
    subprocess.run(["sudo", "-n", "-u", "mira-server", "-H", "sh", "-c",
                    f"cd / && nohup {args.mira_server} serve --config {args.config} >> {args.log} 2>&1 &"],
                   check=True)
    wait_server(client)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8740")
    parser.add_argument("--config", default="/etc/mira-server/mira-server.toml")
    parser.add_argument("--mira-server", default="/opt/mira/bin/mira-server")
    parser.add_argument("--log", default="/tmp/mira-server.log")
    args = parser.parse_args()
    client = Client(args.url)
    wait_server(client)

    step("server info")
    status, _, info = client.request("GET", "/server/info")
    check(status == 200 and info["product"] == "mira-server" and info["protocol"] == 1, f"info {info}")

    step("wrong password is 401, non-member is 403, unknown user is 401")
    status, payload = client.login("alice", "wrong-password")
    check(status == 401 and payload["error"] == "unauthorized", f"wrong password -> {status}")
    status, payload = client.login("carol")
    check(status == 403 and payload["error"] == "forbidden", f"carol (not in mira-users) -> {status}")
    status, _ = client.login("nobody-such-user", "x")
    check(status == 401, f"unknown user -> {status}")

    step("alice and bob sign in with their host passwords (PAM)")
    status, alice = client.login("alice")
    check(status == 200, f"alice login -> {status} {alice}")
    check(alice["user"]["roles"] == ["user"], f"alice roles {alice['user']['roles']}")
    status, bob = client.login("bob")
    check(status == 200, f"bob login -> {status} {bob}")
    check(bob["user"]["roles"] == ["user", "admin"], f"bob roles {bob['user']['roles']}")
    at, bt = alice["access_token"], bob["access_token"]

    step("first /api call starts each engine on demand")
    version, starting = client.api_until_ready("/api/version", at)
    check(version.get("service") == "mira-gateway", f"alice /api/version {version}")
    check(starting, "alice saw 503 engine_starting while her engine started")
    version, _ = client.api_until_ready("/api/version", bt)
    check(version.get("service") == "mira-gateway", f"bob /api/version {version}")
    for name, token in (("alice", at), ("bob", bt)):
        _, _, me = client.request("GET", "/server/me", token=token)
        check(me["engine"]["state"] == "running" and me["engine"]["version"], f"{name} engine {me['engine']}")

    step("each engine runs as its user's own uid")
    leases = {name: lease(name) for name in ("alice", "bob")}
    for name, record in leases.items():
        pid = record["pid"]
        uid = int(subprocess.run(["id", "-u", name], capture_output=True, text=True).stdout)
        check(process_owner(pid) == name, f"{name}'s engine pid {pid} runs as {process_owner(pid)}")
        check(process_uid(pid) == uid, f"{name}'s engine pid {pid} has uid {uid}")
        check(record["host"] == "127.0.0.1", f"{name}'s engine binds {record['host']}")
        check(record["mira_home"] == f"/home/{name}/.mira", f"{name}'s MIRA_HOME is {record['mira_home']}")
    check(leases["alice"]["port"] != leases["bob"]["port"],
          f"distinct ports {leases['alice']['port']} / {leases['bob']['port']}")

    step("/api returns each user's own data")
    _, _, a_status = client.request("GET", "/api/status", token=at)
    _, _, b_status = client.request("GET", "/api/status", token=bt)
    check(a_status["projects_root"].startswith("/home/alice/"), f"alice projects_root {a_status['projects_root']}")
    check(b_status["projects_root"].startswith("/home/bob/"), f"bob projects_root {b_status['projects_root']}")
    _, _, spoof = client.request("GET", "/api/status", token=at, headers={"X-Mira-Server-User": "bob"})
    check(spoof["projects_root"].startswith("/home/alice/"), "a spoofed X-Mira-Server-User header changes nothing")
    status, _, _ = client.request("GET", f"/api/version?access_token={at}")
    check(status == 200, "query-string token works for GET /api/*")

    step("alice cannot reach bob's engine")
    bob_engine_token = sudo("cat", "/home/bob/.mira/runtime/gateway-token").stdout.strip()
    _, _, a_config = client.request("GET", "/api/config", token=at)
    check(bob_engine_token not in json.dumps(a_config), "bob's engine token never appears in alice's responses")
    for name, record in leases.items():
        token = sudo("cat", f"/home/{name}/.mira/runtime/gateway-token").stdout.strip()
        check(token not in json.dumps(alice) + json.dumps(bob), f"{name}'s engine token is never sent to clients")
    direct = sudo("python3", "-c", (
        "import urllib.request, urllib.error, sys\n"
        f"r = urllib.request.Request('http://127.0.0.1:{leases['bob']['port']}/api/status')\n"
        "try:\n    print(urllib.request.urlopen(r, timeout=10).status)\n"
        "except urllib.error.HTTPError as e:\n    print(e.code)\n"), user="alice")
    check(direct.stdout.strip() == "401", f"alice connecting to bob's engine port directly -> {direct.stdout.strip()}")
    read = sudo("cat", "/home/bob/.mira/runtime/gateway-token", user="alice", check_rc=False)
    check(read.returncode != 0, "alice cannot read bob's engine token file")
    read = sudo("cat", "/home/bob/.mira/config.json", user="alice", check_rc=False)
    check(read.returncode != 0, "alice cannot read bob's config")

    step("WebSocket proxy")
    kind, data = asyncio.run(ws_roundtrip(args.url, at))
    check(kind == aiohttp.WSMsgType.TEXT and json.loads(data).get("type") == "error",
          f"alice /ws round trip -> {kind} {data}")
    kind, data = asyncio.run(ws_roundtrip(args.url, bt, "/ws/workflow/v1"))
    check(kind == aiohttp.WSMsgType.TEXT and json.loads(data).get("message_type") == "protocol_error",
          f"bob /ws/workflow/v1 round trip -> {kind} {data}")
    check(asyncio.run(ws_status(args.url, at, "https://evil.example")) == 403, "foreign Origin is refused")
    check(asyncio.run(ws_status(args.url, "msa_bogus", "file://")) == 401, "bad token is refused")

    step("refresh rotates; a replayed refresh token revokes the session")
    status, _, refreshed = client.request("POST", "/server/auth/refresh", body={"refresh_token": bob["refresh_token"]})
    check(status == 200 and refreshed["access_token"] != bt, "refresh issued new tokens")
    status, _, _ = client.request("POST", "/server/auth/refresh", body={"refresh_token": bob["refresh_token"]})
    check(status == 401, "replayed refresh token -> 401")
    status, _, _ = client.request("GET", "/server/me", token=refreshed["access_token"])
    check(status == 401, "the whole family was revoked")
    status, bob = client.login("bob")
    bt = bob["access_token"]

    step("Mira Server restart re-attaches to running engines")
    alice_pid = leases["alice"]["pid"]
    restart_server(args, client)
    for _ in range(60):
        _, _, me = client.request("GET", "/server/me", token=at)
        if me["engine"]["state"] == "running":
            break
        time.sleep(0.5)
    check(me["engine"]["state"] == "running", f"alice's engine re-attached: {me['engine']}")
    check(lease("alice")["pid"] == alice_pid, "same engine process after the restart")
    payload, _ = client.api_until_ready("/api/status", at)
    check(payload["projects_root"].startswith("/home/alice/"), "proxy works after the restart")

    step("logout revokes the session")
    status, _, _ = client.request("POST", "/server/auth/logout", token=at)
    check(status == 204, "logout -> 204")
    status, _, _ = client.request("GET", "/server/me", token=at)
    check(status == 401, "access token is dead after logout")
    status, _, _ = client.request("POST", "/server/auth/refresh", body={"refresh_token": alice["refresh_token"]})
    check(status == 401, "refresh token is dead after logout")

    step("stop works")
    status, alice = client.login("alice")
    at = alice["access_token"]
    status, _, engine = client.request("POST", "/server/engine/stop", token=at)
    check(status == 200 and engine["state"] == "stopped", f"stop -> {status} {engine}")
    for _ in range(50):
        if not pid_alive(alice_pid):
            break
        time.sleep(0.2)
    check(not pid_alive(alice_pid), f"alice's engine pid {alice_pid} exited")
    check(pid_alive(leases["bob"]["pid"]), "bob's engine is untouched")

    step("removal from mira-users revokes access")
    sudo("gpasswd", "-d", "bob", "mira-users")
    status, _, payload = client.request("GET", "/server/me", token=bt)
    check(status == 403 and payload["error"] == "forbidden", f"bob after group removal -> {status}")
    status, _, _ = client.request("POST", "/server/auth/refresh", body={"refresh_token": bob["refresh_token"]})
    check(status == 401, "bob's sessions were revoked")
    status, _ = client.login("bob")
    check(status == 403, "bob can no longer sign in")

    print("\nAll Mira Server end-to-end checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
