"""Privacy boundary between the private profile and the public community (mira#203)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mira_engine.agent.context import ContextBuilder
from mira_engine.channels.community import CommunityChannel
from mira_engine.community.client import CommunityClient, CommunityRuleError
from mira_engine.community.privacy import (
    PrivacyGuard,
    PrivacyViolation,
    check_outbound_payload,
)

_UUID = "24110f35-3c7d-4006-81e4-02a54584a7a4"


def _workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "USER.local.md").write_text(
        "## Basic Information\n\n- **Name**: Dana Quill\n- **Email**: dana.quill@corp.example.net\n"
        "\n## Topics of Interest\n\n- Project Bluefin internals\n"
    )
    return ws


@pytest.fixture
def guard(tmp_path):
    return PrivacyGuard.from_workspace(_workspace(tmp_path), extra_terms=["Bluefin"])


# --------------------------------------------------------------------------- #
# Outbound scanning                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "token: ghp_" + "a" * 36,
        "use sk-ant-" + "b" * 30,
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "AKIA" + "A" * 16,
        "password = hunter2hunter2",
    ],
)
def test_secrets_are_detected(guard, text):
    assert any(f.kind == "secret" for f in guard.scan(text))


def test_profile_name_email_and_config_terms_are_private(guard):
    assert {f.kind for f in guard.scan("Thanks, Dana Quill!")} == {"private-term"}
    assert "private-term" in {f.kind for f in guard.scan("we use bluefin at work")}
    assert "email" in {f.kind for f in guard.scan("mail dana.quill@corp.example.net")}


def test_home_paths_are_private(guard):
    assert {f.kind for f in guard.scan("see /Users/someone/proj/a.py")} == {"home-path"}
    assert {f.kind for f in guard.scan(r"C:\Users\someone\x")} == {"home-path"}


def test_ordinary_text_and_public_emails_pass(guard):
    assert guard.scan("Retry with jitter; see docs/retry.md. Contact 123+bot@users.noreply.github.com") == []


def test_placeholder_profile_name_is_not_a_term(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    (ws / "USER.local.md").write_text("- **Name**: MIRA User\n")
    assert PrivacyGuard.from_workspace(ws).scan("Hello MIRA User") == []


def test_redact_masks_private_details(guard):
    text, findings = guard.redact("Dana Quill fixed /home/dq/x.py, ping dana.quill@corp.example.net")
    assert "Dana" not in text and "/home/dq" not in text and "corp.example.net" not in text
    assert {f.kind for f in findings} == {"private-term", "home-path", "email"}


def test_payload_check_scans_text_fields_and_only_secrets_in_diffs(guard):
    with pytest.raises(PrivacyViolation):
        check_outbound_payload(guard, {"thread_id": _UUID, "content": "hi from Dana Quill"})
    # Code may contain emails/paths; only credentials block a diff.
    check_outbound_payload(guard, {"diff": "+author = 'x@corp.example.net'  # /home/ci/build"})
    with pytest.raises(PrivacyViolation):
        check_outbound_payload(guard, {"diff": "+KEY = 'ghp_" + "c" * 36 + "'"})


async def test_client_refuses_to_send_private_content(guard):
    client = CommunityClient("http://unused.invalid", "tok", privacy_guard=guard)
    with pytest.raises(CommunityRuleError) as exc:
        await client._post("/agents/messages", {"thread_id": _UUID, "content": "Dana Quill says hi"})
    assert exc.value.rule_id == "privacy-filter"
    assert exc.value.how_to_resolve


# --------------------------------------------------------------------------- #
# Channel replies                                                              #
# --------------------------------------------------------------------------- #


class _FakeHttp:
    def __init__(self):
        self.posts: list[dict] = []
        self.headers: dict = {}

    async def post(self, url, json):
        self.posts.append(json)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {}

        return _Resp()


def _channel(guard):
    config = SimpleNamespace(api_base="http://x/community", agent_token="t", agent_id="a", rules_version=0, rules_text="")
    channel = CommunityChannel(config, SimpleNamespace())
    channel._http = _FakeHttp()
    channel._privacy_guard = guard
    return channel


async def test_channel_masks_private_details_in_replies(guard):
    channel = _channel(guard)
    await channel.send(SimpleNamespace(chat_id=_UUID, content="Dana Quill here, see /Users/dq/a.py", reply_to=None))
    assert len(channel._http.posts) == 1
    posted = channel._http.posts[0]["content"]
    assert "Dana" not in posted and "/Users/dq" not in posted


async def test_channel_drops_replies_with_credentials(guard):
    channel = _channel(guard)
    await channel.send(SimpleNamespace(chat_id=_UUID, content="my key is ghp_" + "z" * 36, reply_to=None))
    assert channel._http.posts == []


# --------------------------------------------------------------------------- #
# Community turns never see the private profile                                #
# --------------------------------------------------------------------------- #


def test_community_prompt_excludes_profile_memory_and_history(tmp_path):
    ws = _workspace(tmp_path)
    builder = ContextBuilder(ws)
    builder.memory.write_long_term("The user works on Project Bluefin at Initech.")
    builder.memory.append_history("User asked to draft the Bluefin launch email")

    private = builder.build_system_prompt(channel="ui")
    assert "Dana Quill" in private and "Initech" in private and "launch email" in private

    community = builder.build_system_prompt(channel="community")
    # "## USER.md" is the bootstrap section header; the bare file name is also
    # mentioned by the default AGENTS.md template, which community turns do see.
    for secret in ("Dana Quill", "Initech", "launch email", "## USER.md", "# Memory", "# Recent History"):
        assert secret not in community
    assert "## SOUL.md" in community


def test_community_prompt_uses_the_approved_persona(tmp_path):
    ws = _workspace(tmp_path)
    # A phrase no bundled skill description uses, so the negative check is meaningful.
    (ws / "COMMUNITY_PERSONA.md").write_text("Interested in tide-pool photogrammetry and retry strategies.")
    community = ContextBuilder(ws).build_system_prompt(channel="community")
    assert "tide-pool photogrammetry and retry strategies" in community
    assert "tide-pool photogrammetry" not in ContextBuilder(ws).build_system_prompt(channel="ui")
