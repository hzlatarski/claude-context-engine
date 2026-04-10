"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook reads the knowledge base index and recent daily log, then injects
them as additional context so Claude always "remembers" what it has learned.

Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
        }]
    }
}
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
WIP_FILE = ROOT / "wip.md"

MAX_CONTEXT_CHARS = 60_000
MAX_LOG_LINES = 30
MAX_WIP_CHARS = 2_000
# compiled-truth.md lives in the PROJECT root's knowledge/ dir (written by config.py's
# KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"), which is two levels up from the
# memory-compiler root. session-start.py's KNOWLEDGE_DIR points to the memory-compiler's
# own knowledge/ dir, which is a different path.
PROJECT_KNOWLEDGE_DIR = ROOT.parent.parent / "knowledge"
COMPILED_TRUTH_FILE = PROJECT_KNOWLEDGE_DIR / "compiled-truth.md"
MAX_COMPILED_TRUTH_CHARS = 40_000


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def get_wip() -> str | None:
    """Read wip.md if it exists and has content. Returns None if absent/empty."""
    if not WIP_FILE.exists():
        return None
    content = WIP_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return None
    if len(content) > MAX_WIP_CHARS:
        content = content[:MAX_WIP_CHARS] + "\n\n...(truncated)"
    return content


def get_compiled_truth() -> str | None:
    """Read compiled-truth.md if it exists. Returns None if absent/empty."""
    if not COMPILED_TRUTH_FILE.exists():
        return None
    content = COMPILED_TRUTH_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return None
    if len(content) > MAX_COMPILED_TRUTH_CHARS:
        # Truncate at the last complete article boundary
        truncated = content[:MAX_COMPILED_TRUTH_CHARS]
        last_sep = truncated.rfind("\n---\n")
        if last_sep > 0:
            truncated = truncated[:last_sep]
        content = truncated + "\n\n...(truncated)"
    return content


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    parts = []

    # Today's date
    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    # Work In Progress — "resume here" state from the last session that
    # ended mid-task. Placed second so Claude sees it immediately after
    # the date, before the larger knowledge base index.
    wip = get_wip()
    if wip:
        parts.append(f"## Work In Progress (resume here)\n\n{wip}")

    # Knowledge base index (the core retrieval mechanism)
    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        parts.append(f"## Knowledge Base Index\n\n{index_content}")
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    # Compiled truth — dense summary of all current knowledge
    compiled_truth = get_compiled_truth()
    if compiled_truth:
        parts.append(f"## Compiled Truth (all current knowledge)\n\n{compiled_truth}")

    # Recent daily log
    recent_log = get_recent_log()
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    context = "\n\n---\n\n".join(parts)

    # Truncate if too long
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
