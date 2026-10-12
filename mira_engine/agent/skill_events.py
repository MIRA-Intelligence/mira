"""Facts about skill instructions loaded into context, not execution claims."""

import re
from datetime import datetime, timezone
from uuid import uuid4


def skill_loaded(name: str, source: str, **details) -> dict:
    return {
        **details,
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "skill_name": name,
        "source": source,
        "loaded": True,
    }


def skill_read_details(tool_name: str, arguments: object, result: object) -> dict | None:
    """Only a successful, numbered text read counts; errors/empty files do not."""
    if tool_name != "read_file" or not isinstance(arguments, dict) or not isinstance(result, str):
        return None
    path = arguments.get("path")
    if not isinstance(path, str) or not re.match(r"^\d+\|", result):
        return None
    parts = path.replace("\\", "/").rstrip("/").split("/")
    if len(parts) < 2 or parts[-1].lower() != "skill.md" or not parts[-2]:
        return None
    return skill_loaded(
        parts[-2], "read_file", tool=tool_name,
        partial=arguments.get("offset", 1) != 1 or "(Showing lines " in result,
    )
