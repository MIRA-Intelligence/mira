"""WhatsApp channel implementation using Node.js bridge."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path

from loguru import logger

from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.channels.base import BaseChannel
from medpilot.config.paths import get_runtime_subdir
from medpilot.config.schema import WhatsAppConfig


def _bridge_token_path() -> Path:
    return get_runtime_subdir("whatsapp-auth") / "bridge-token"


def _load_or_create_bridge_token(path: Path) -> str:
    """Load a persisted bridge token or create one on first use."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


class WhatsAppChannel(BaseChannel):
    """WhatsApp channel that connects to a Node.js bridge."""

    name = "whatsapp"

    def __init__(self, config: WhatsAppConfig | dict, bus: MessageBus):
        if isinstance(config, dict):
            config = WhatsAppConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._lid_to_phone: dict[str, str] = {}
        self._bridge_token: str | None = None
        self.transcription_provider: str = "openai"
        self.transcription_api_key: str = ""

    def _effective_bridge_token(self) -> str:
        if self._bridge_token is not None:
            return self._bridge_token
        configured = self.config.bridge_token.strip()
        if configured:
            self._bridge_token = configured
        else:
            self._bridge_token = _load_or_create_bridge_token(_bridge_token_path())
        return self._bridge_token

    async def login(self, force: bool = False) -> bool:
        """Run bridge login flow (QR) in foreground."""
        try:
            bridge_dir = _ensure_bridge_setup()
        except RuntimeError as e:
            logger.error("{}", e)
            return False

        env = {**os.environ}
        env["BRIDGE_TOKEN"] = self._effective_bridge_token()
        env["AUTH_DIR"] = str(_bridge_token_path().parent)

        try:
            subprocess.run([shutil.which("npm"), "start"], cwd=bridge_dir, check=True, env=env)
        except subprocess.CalledProcessError:
            return False
        return True

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.bridge_url
        logger.info("Connecting to WhatsApp bridge at {}...", bridge_url)
        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "auth", "token": self._effective_bridge_token()}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("WhatsApp bridge connection error: {}", e)
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        chat_id = msg.chat_id
        if msg.content:
            payload = {"type": "send", "to": chat_id, "text": msg.content}
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

        for media_path in msg.media or []:
            mime, _ = mimetypes.guess_type(media_path)
            payload = {
                "type": "send_media",
                "to": chat_id,
                "filePath": media_path,
                "mimetype": mime or "application/octet-stream",
                "fileName": media_path.rsplit("/", 1)[-1],
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def transcribe_audio(self, path: str) -> str | None:
        try:
            from medpilot.providers.transcription import OpenAITranscriptionProvider

            if not self.transcription_api_key:
                return None
            provider = OpenAITranscriptionProvider(api_key=self.transcription_api_key)
            return await provider.transcribe(Path(path))
        except Exception:
            return None

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")
        if msg_type == "message":
            pn = data.get("pn", "")
            sender = data.get("sender", "")
            content = data.get("content", "")
            message_id = data.get("id", "")

            if message_id:
                if message_id in self._processed_message_ids:
                    return
                self._processed_message_ids[message_id] = None
                while len(self._processed_message_ids) > 1000:
                    self._processed_message_ids.popitem(last=False)

            is_group = data.get("isGroup", False)
            was_mentioned = data.get("wasMentioned", False)
            if is_group and getattr(self.config, "group_policy", "open") == "mention":
                if not was_mentioned:
                    return

            raw_a = pn or ""
            raw_b = sender or ""
            id_a = raw_a.split("@")[0] if "@" in raw_a else raw_a
            id_b = raw_b.split("@")[0] if "@" in raw_b else raw_b

            phone_id = ""
            lid_id = ""
            for raw_val, extracted in [(raw_a, id_a), (raw_b, id_b)]:
                if "@s.whatsapp.net" in raw_val:
                    phone_id = extracted
                elif "@lid.whatsapp.net" in raw_val:
                    lid_id = extracted
                elif extracted and not phone_id:
                    phone_id = extracted

            if phone_id and lid_id:
                self._lid_to_phone[lid_id] = phone_id
            sender_id = phone_id or self._lid_to_phone.get(lid_id, "") or lid_id or id_a or id_b

            media_paths = data.get("media") or []
            if content == "[Voice Message]":
                if media_paths:
                    transcription = await self.transcribe_audio(media_paths[0])
                    content = transcription or "[Voice Message: Transcription failed]"
                else:
                    content = "[Voice Message: Audio not available]"

            if media_paths:
                for p in media_paths:
                    mime, _ = mimetypes.guess_type(p)
                    media_type = "image" if mime and mime.startswith("image/") else "file"
                    media_tag = f"[{media_type}: {p}]"
                    content = f"{content}\n{media_tag}" if content else media_tag

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                },
            )
        elif msg_type == "status":
            status = data.get("status")
            logger.info("WhatsApp status: {}", status)
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
        elif msg_type == "qr":
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
        elif msg_type == "error":
            logger.error("WhatsApp bridge error: {}", data.get("error"))


def _ensure_bridge_setup() -> Path:
    """Ensure the WhatsApp bridge is available and built."""
    from medpilot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    npm_path = shutil.which("npm")
    if not npm_path:
        raise RuntimeError("npm not found. Please install Node.js >= 18.")

    current_file = Path(__file__)
    pkg_bridge = current_file.parent.parent / "bridge"
    src_bridge = current_file.parent.parent.parent / "bridge"

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge
    if not source:
        raise RuntimeError("WhatsApp bridge source not found.")

    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))
    subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)
    subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)
    return user_bridge
