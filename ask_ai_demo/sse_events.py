"""
crm_chat/sse_events.py
─────────────────────
SSE event type constants and helpers.

Wire format produced:
    event: <event_type>
    data: <json_payload>

    (blank line terminates each event)
"""
import json

# ── Event type constants ──────────────────────────────────────
TOKEN            = "token"           # one AI text chunk
STATUS           = "status"          # general progress message
TOOL_CALL        = "tool_call"       # AI decided to run a tool
SANDBOX_START    = "sandbox_start"   # job submitted to Azure
SANDBOX_PROGRESS = "sandbox_progress"# poll tick while running
SANDBOX_COMPLETE = "sandbox_complete"# sandbox finished
DOCUMENT_START   = "document_start"  # file save started
DOCUMENT_COMPLETE= "document_complete"# file ready + download URL
RETRY            = "retry"           # AI fixing code, retrying
ERROR            = "error"           # any error
FINAL            = "final"           # complete AI text (end of stream)
DONE             = "done"            # stream closed


# ── Serializers ───────────────────────────────────────────────

def sse(event_type: str, data: dict) -> str:
    """Return a fully-formatted SSE event string (ready to yield)."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_token(text: str) -> str:
    """Shortcut for high-frequency token events."""
    return sse(TOKEN, {"text": text})


def sse_status(stage: str, message: str) -> str:
    return sse(STATUS, {"stage": stage, "message": message})


def sse_error(message: str, recoverable: bool = False) -> str:
    return sse(ERROR, {"message": message, "recoverable": recoverable})


def sse_done() -> str:
    return sse(DONE, {})
