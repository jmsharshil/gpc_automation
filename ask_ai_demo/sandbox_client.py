"""
crm_chat/sandbox_client.py
──────────────────────────
Async generator that manages the full Azure Durable Function sandbox
lifecycle and yields typed event dicts (not SSE strings — the caller
in views.py serialises them via sse_events.sse()).

Usage in views.py:
    async for event in run_sandbox_with_events(code, start_url):
        if event["type"] == SANDBOX_START:
            yield sse(SANDBOX_START, {...})
        elif event["type"] == SANDBOX_PROGRESS:
            yield sse(SANDBOX_PROGRESS, {...})
        elif event["type"] == SANDBOX_COMPLETE:
            job_result = event["result"]
        elif event["type"] == ERROR:
            err_body = event["message"]

Replaces the old run_sandbox_job() helper that returned a single dict
after silently polling (users saw a blank screen for the full duration).
"""
import asyncio
import logging

import httpx

from ask_ai_demo.sse_events import SANDBOX_START, SANDBOX_PROGRESS, SANDBOX_COMPLETE, ERROR

logger = logging.getLogger(__name__)


async def run_sandbox_with_events(
    code: str,
    start_url: str,
    poll_interval: float = 3.0,
    max_wait_seconds: int = 600,
):
    """
    Async generator — manages one sandbox execution end-to-end.

    Yields typed event dicts:
        {"type": SANDBOX_START,    "execution_id": str | None, "message": str}
        {"type": SANDBOX_PROGRESS, "execution_id": str | None, "elapsed": int, "message": str}
        {"type": SANDBOX_COMPLETE, "execution_id": str | None, "result": dict}
        {"type": ERROR,            "execution_id": str | None, "message": str}

    The "result" dict on SANDBOX_COMPLETE is the Azure Durable output, i.e.:
        {"success": True,  "filename": "report.xlsx", "file_b64": "..."}
        {"success": False, "error": "...", "traceback": "..."}
    """
    execution_id = None

    client = httpx.AsyncClient()
    try:
        # ── 1. Submit job ─────────────────────────────────────────
        try:
            start_resp = await client.post(start_url, json={"code": code}, timeout=30.0)
            start_resp.raise_for_status()
            start_data = start_resp.json()
        except Exception as exc:
            logger.exception("sandbox_client: failed to start job — %s", exc)
            yield {
                "type": ERROR,
                "execution_id": None,
                "message": f"Failed to start sandbox: {exc}",
            }
            return

        # Azure Durable Functions returns "id" and "statusQueryGetUri"
        execution_id = start_data.get("id")
        status_url = start_data.get("statusQueryGetUri")

        if not status_url:
            yield {
                "type": ERROR,
                "execution_id": execution_id,
                "message": "Sandbox did not return a status URL — check AZURE_SANDBOX_URL config.",
            }
            return

        logger.info("sandbox_client: job started execution_id=%s", execution_id)

        yield {
            "type": SANDBOX_START,
            "execution_id": execution_id,
            "message": "Running Python code in sandbox...",
        }

        # ── 2. Poll until done ────────────────────────────────────
        elapsed = 0.0
        while elapsed < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status_resp = await client.get(status_url, timeout=15.0)
                status_resp.raise_for_status()
                status_data = status_resp.json()
            except Exception as poll_exc:
                logger.warning(
                    "sandbox_client: poll failed execution_id=%s elapsed=%.0fs — %s",
                    execution_id, elapsed, poll_exc,
                )
                yield {
                    "type": ERROR,
                    "execution_id": execution_id,
                    "message": f"Status poll failed: {poll_exc}",
                }
                return

            runtime_status = status_data.get("runtimeStatus", "")

            if runtime_status == "Completed":
                output = status_data.get("output") or {}
                logger.info(
                    "sandbox_client: completed execution_id=%s elapsed=%.0fs success=%s",
                    execution_id, elapsed, output.get("success"),
                )
                yield {
                    "type": SANDBOX_COMPLETE,
                    "execution_id": execution_id,
                    "result": output,
                }
                return

            if runtime_status in ("Failed", "Terminated"):
                output = status_data.get("output")
                logger.warning(
                    "sandbox_client: %s execution_id=%s output=%s",
                    runtime_status, execution_id, output,
                )
                yield {
                    "type": ERROR,
                    "execution_id": execution_id,
                    "message": f"Sandbox {runtime_status}: {output}",
                }
                return

            # Still Running / Pending — emit a progress tick every poll interval
            logger.debug(
                "sandbox_client: still running execution_id=%s elapsed=%.0fs status=%s",
                execution_id, elapsed, runtime_status,
            )
            yield {
                "type": SANDBOX_PROGRESS,
                "execution_id": execution_id,
                "elapsed": int(elapsed),
                "message": f"Executing code… ({int(elapsed)}s elapsed)",
            }

        # ── 3. Timeout ────────────────────────────────────────────
        logger.error(
            "sandbox_client: timeout after %ss execution_id=%s", max_wait_seconds, execution_id
        )
        yield {
            "type": ERROR,
            "execution_id": execution_id,
            "message": f"Sandbox timeout after {max_wait_seconds}s — the code may contain an infinite loop.",
        }

    finally:
        try:
            await client.aclose()
        except Exception:
            pass
