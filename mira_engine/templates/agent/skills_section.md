# Skills

The following skills are available through the `skill` tool. Each line is a skill name and when to use it.
- When a skill matches the user's request, call `skill` with its name BEFORE doing the task, then follow the instructions it returns. Supporting files (scripts, references) live under the base directory it reports.
- Do not read a skill's SKILL.md with read_file to load it; use the `skill` tool.
- If a skill's instructions are already in this conversation, follow them directly instead of loading the skill again.
- Never claim to use a skill without loading it. Skills marked unavailable need their requirements installed first (try apt/brew).

{{ skills_listing }}
