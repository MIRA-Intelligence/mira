"""The reserved chat where the agent's background community work lands.

The desktop UI keeps one chat per conversation the user started. Work the agent
does on its own — the heartbeat's community task inbox, HEARTBEAT.md tasks —
used to be written into whichever chat the user had touched last, in the middle
of whatever they were doing, with no shared context to talk back in.

It now has one home: a chat with this fixed id. The heartbeat runs its turns in
that chat's session and delivers what it did there, so the user can read it when
they want, answer, ``/approve`` a held action or ask why. The UI shows the chat
once the user has joined the community (``GET /api/community/status`` reports
``chat_id``); until then nothing the agent writes there interrupts anyone.
"""

from __future__ import annotations

COMMUNITY_CHAT_ID = "community"
