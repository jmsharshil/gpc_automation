# """
# crm_chat/chat_agent.py
# ======================
# One small agent loop that replaces the hand-written sandbox / retry machinery
# that used to live inside StreamingChatAPIView.

# How it works (same idea as ChatGPT's Code Interpreter, but the code runs in
# YOUR Azure sandbox so files never leave your infrastructure):

#     model call (streamed)
#         -> model answers in text            -> done
#         -> model calls run_python_code      -> run it in the Azure sandbox
#               -> file produced              -> save it, tell the model, let it write a summary
#               -> stdout (inspect step)      -> send stdout back, model continues
#               -> error / traceback          -> send the REAL error back, model fixes its own code

# The model already knows openpyxl / python-pptx / python-docx / reportlab, so there
# are no rules, no pre-defined style variables and no auto-fixers any more.
# """
# import asyncio
# import base64
# import json
# import logging
# import textwrap 

# from .pptx_quality import check_deck, format_issues
# from .sandbox_client import run_sandbox_with_events
# from .sse_events import (
#     sse, sse_token,
#     TOOL_CALL, SANDBOX_START, SANDBOX_PROGRESS, SANDBOX_COMPLETE,
#     DOCUMENT_START, DOCUMENT_COMPLETE, RETRY, ERROR,
# )

# logger = logging.getLogger(__name__)

# MAX_STEPS = 8          # inspect / build / fix rounds per user message
# MAX_FIX_ROUNDS = 2     # how many times a deck with layout problems is sent back to the model
# ENABLE_LAYOUT_CHECK = False   # True = check decks on the server before delivery

# # --------------------------------------------------------------------------
# # Tool definition — short on purpose.  Edit the library list to match what is
# # really installed in your Azure sandbox.
# # --------------------------------------------------------------------------
# RUN_PYTHON_TOOL = {
#     "type": "function",
#     "function": {
#         "name": "run_python_code",
#         "description": (
#             "Run a Python script in a private sandbox to create or edit files "
#             "(xlsx, pptx, docx, pdf, csv). "
#             "Libraries: openpyxl, pandas, numpy, python-pptx, python-docx, reportlab, Pillow. "
#             "User files are read-only in /tmp/inputs. "
#             "Save every deliverable in /tmp/outputs/<filename>. "
#             "If the script fails you receive the traceback: fix it and call the tool again. "
#             "PowerPoint: python-pptx text boxes do NOT wrap by default, so always set text_frame.word_wrap = True "
#             "and keep all text inside its shape and inside the slide. "
#             "You may first run a script that only print()s to inspect an input file."
#         ),
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "code": {"type": "string", "description": "Complete, self-contained Python script."},
#                 "filename": {
#                     "type": "string",
#                     "description": "Output filename with extension (e.g. report.pptx).",
#                 },
#             },
#             "required": ["code", "filename"],
#         },
#     },
# }

# # Only creates the folders; the model writes its own imports.
# PRELUDE = (
#     "import os\n"
#     "os.makedirs('/tmp/inputs', exist_ok=True)\n"
#     "os.makedirs('/tmp/outputs', exist_ok=True)\n\n"
# )

# # --------------------------------------------------------------------------
# # Prompt note about the files the sandbox can see (names only — cheap)
# # --------------------------------------------------------------------------
# _FILES_NOTE = (
#     "\n\n[FILES AVAILABLE TO run_python_code]\n{names}\n"
#     "They are read-only in /tmp/inputs. Decide each file's role from the user's latest message "
#     "(template / style reference, logo or image to embed, data source, or file to edit) and follow "
#     "their instructions exactly. Save new deliverables under a NEW name in /tmp/outputs. "
#     "If the user asks to change an earlier document, edit the previous file and keep its content.\n"
# )

# _PPTX_NOTE = (
#     "PowerPoint rules: start from the attached deck with Presentation('/tmp/inputs/<file>'), delete its sample "
#     "slides, then add slides with prs.slides.add_slide(prs.slide_layouts[i]) using the layout indexes from the "
#     "TEMPLATE REPORT. Keep every shape inside the layout's SAFE AREA from the report and compute positions from "
#     "those numbers (rows, columns, grids), never from assumed slide dimensions. Fill the title placeholder with "
#     "plain text only (do not change its font, size, colour or position) and do not put your own text on the "
#     "template's bands, side bars, logo or footer. Size fonts so text fits its box (about 11-16 pt in cards, never "
#     "below 9 pt) and delete unused placeholders before saving. The server checks the saved deck and returns any "
#     "layout problems to you to fix before it is delivered.\n"
# )


# def files_note(names) -> str:
#     """System-prompt text listing the files available to the sandbox (empty if none)."""
#     names = [n for n in names if n]
#     if not names:
#         return ""
#     text = _FILES_NOTE.format(names="\n".join(f"- /tmp/inputs/{n}" for n in names))
#     if any(n.lower().endswith(".pptx") for n in names):
#         text += _PPTX_NOTE
#     return text


# # --------------------------------------------------------------------------
# # Helpers
# # --------------------------------------------------------------------------
# def _strip_fences(raw: str) -> str:
#     raw = (raw or "").strip()
#     if raw.startswith("```"):
#         lines = raw.split("\n")[1:]
#         if lines and lines[-1].strip().startswith("```"):
#             lines = lines[:-1]
#         raw = "\n".join(lines)
#     return textwrap.dedent(raw).strip("\n")


# def _inputs_code(files: dict) -> str:
#     """Python source that writes each input file into /tmp/inputs inside the sandbox."""
#     if not files:
#         return ""
#     out = ["import base64 as _b64"]
#     for name, data in files.items():
#         b64 = base64.b64encode(data).decode("ascii")
#         out.append(f"open('/tmp/inputs/' + {name!r}, 'wb').write(_b64.b64decode('{b64}'))")
#     out.append("del _b64\n")
#     return "\n".join(out) + "\n"


# async def _model_turn(client, kwargs: dict, out: dict, text_sink: list):
#     """
#     Stream ONE model call.  Yields SSE strings for tokens / tool-call notices and
#     stores the final text, tool calls and finish reason in `out`.
#     """
#     stream = await client.chat.completions.create(**kwargs, stream=True)
#     text, calls, finish = [], {}, None
#     async for chunk in stream:
#         if not chunk.choices:
#             continue
#         ch = chunk.choices[0]
#         if ch.finish_reason:
#             finish = ch.finish_reason
#         d = ch.delta
#         if not d:
#             continue
#         for tc in (d.tool_calls or []):
#             is_new = tc.index not in calls
#             slot = calls.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
#             if tc.id:
#                 slot["id"] = tc.id
#             fn = tc.function
#             if fn is not None and fn.name:
#                 slot["name"] = fn.name
#             if is_new:
#                 yield sse(TOOL_CALL, {"name": "run_python_code", "message": "✍️ Writing Python code…"})
#             if fn is not None and fn.arguments:
#                 slot["arguments"] += fn.arguments
#         if d.content:
#             text.append(d.content)
#             text_sink.append(d.content)
#             yield sse_token(d.content)
#     out.update(text="".join(text), calls=list(calls.values()), finish=finish)


# # --------------------------------------------------------------------------
# # The agent loop
# # --------------------------------------------------------------------------
# async def run_chat_agent(
#     *,
#     client,
#     base_kwargs: dict,
#     messages: list,
#     load_input_files,
#     sandbox_url,
#     save_file,
#     result: dict,
#     text_sink: list,
#     allow_tools: bool = True,
# ):
#     """
#     Async generator that yields SSE strings.

#     base_kwargs       model, max_completion_tokens, temperature / reasoning_effort
#                       (do NOT include messages, tools or stream)
#     messages          full chat payload (system prompt + RAG + history + user message)
#     load_input_files  async () -> {filename: bytes}; called ONLY when the model runs code
#     sandbox_url       Azure sandbox start URL
#     save_file         async (filename, bytes) -> {"path": ..., "url": ...}
#     result            dict that is filled with: text, finish, files
#     text_sink         list that receives every streamed text piece (for the FINAL event)
#     """
#     msgs = list(messages)
#     result.update(text="", files=[], finish=None)
#     file_saved = False
#     fix_rounds = 0

#     for step in range(MAX_STEPS):
#         kw = {**base_kwargs, "messages": msgs}
#         if allow_tools:
#             kw["tools"] = [RUN_PYTHON_TOOL]
#             # after a file is saved, only allow a text summary
#             kw["tool_choice"] = "none" if file_saved else "auto"

#         turn: dict = {}
#         async for ev in _model_turn(client, kw, turn, text_sink):
#             yield ev
#         result["text"] += turn["text"]
#         result["finish"] = turn["finish"]

#         call = next((c for c in turn["calls"] if c["name"] == "run_python_code"), None)
#         if not call:
#             return  # plain text answer -> finished

#         feedback = None
#         args: dict = {}
#         code = ""
#         sent_arguments = call["arguments"]
#         try:
#             args = json.loads(call["arguments"])
#             code = args["code"]
#         except (json.JSONDecodeError, KeyError, TypeError):
#             sent_arguments = "{}"   # never send broken JSON back to the API
#             logger.error("Bad tool arguments from model: %.300s", call["arguments"])
#             feedback = (
#                 "Your tool call was cut off or not valid JSON. "
#                 "Send the complete script again in a single run_python_code call."
#             )
#             if turn["finish"] == "length":
#                 yield sse(ERROR, {
#                     "message": "⚠️ The AI was cut off while writing the code (token limit reached). "
#                                "Please increase your Max Tokens setting.",
#                 })

#         if feedback is None:
#             try:
#                 files = await load_input_files()
#             except Exception as exc:  # never let a storage problem kill the chat
#                 logger.warning("Could not load input files: %s", exc)
#                 files = {}

#             full_code = PRELUDE + _inputs_code(files) + _strip_fences(code)
#             logger.info("[AI CODE] filename=%s chars=%d", args.get("filename"), len(code))

#             job, err = None, None
#             async for e in run_sandbox_with_events(full_code, sandbox_url):
#                 if e["type"] == SANDBOX_START:
#                     yield sse(SANDBOX_START, {"message": e.get("message", "Running code…")})
#                 elif e["type"] == SANDBOX_PROGRESS:
#                     yield sse(SANDBOX_PROGRESS, {
#                         "elapsed": e.get("elapsed", 0),
#                         "message": e.get("message", "Running…"),
#                     })
#                 elif e["type"] == SANDBOX_COMPLETE:
#                     job = e["result"]
#                 elif e["type"] == ERROR:
#                     err = e.get("message", "Sandbox error")

#             if job and job.get("success") and job.get("file_b64"):
#                 name = job.get("filename") or args.get("filename") or "document"
#                 ext = name.rsplit(".", 1)[-1] if "." in name else "file"
#                 data = base64.b64decode(job["file_b64"])

#                 # Layout check (PowerPoint only), done on YOUR server. A deck with visible layout
#                 # problems is NOT delivered; the problems go back to the model to fix first.
#                 layout_issues = []
#                 # if ext.lower() == "pptx":
#                 if ext.lower() == "pptx" and ENABLE_LAYOUT_CHECK:
#                     layout_issues = await asyncio.to_thread(check_deck, data)

#                 if layout_issues and fix_rounds < MAX_FIX_ROUNDS:
#                     fix_rounds += 1
#                     feedback = format_issues(layout_issues)
#                     yield sse(RETRY, {
#                         "attempt": fix_rounds,
#                         "max": MAX_FIX_ROUNDS,
#                         "message": "🎨 Improving the slide layout…",
#                     })
#                 else:
#                     if layout_issues:
#                         logger.warning("Delivering deck with remaining layout issues: %s", layout_issues)
#                         result.setdefault("warnings", []).extend(layout_issues)
#                     yield sse(DOCUMENT_START, {
#                         "document_type": ext,
#                         "message": f"Saving your {ext.upper()} document…",
#                     })
#                     saved = await save_file(name, data)
#                     yield sse(DOCUMENT_COMPLETE, {
#                         "document_type": ext,
#                         "filename": name,
#                         "file_url": saved["url"],
#                     })
#                     result["files"].append({"filename": name, **saved})
#                     file_saved = True
#                     feedback = (
#                         f"File '{name}' was created and saved. "
#                         "Reply with one short line summarising what it contains. "
#                         "Do not repeat the file contents."
#                     )
#             elif job and job.get("success"):
#                 feedback = ("STDOUT:\n" + (job.get("stdout") or "(the script printed nothing)"))[:3000]
#             else:
#                 feedback = (job or {}).get("error") or err or "Unknown sandbox error"
#                 tb = (job or {}).get("traceback")
#                 if tb:
#                     feedback += "\n" + tb
#                 feedback = feedback[:3000]
#                 yield sse(RETRY, {
#                     "attempt": step + 1,
#                     "max": MAX_STEPS,
#                     "message": f"⚠️ Code error — the AI is fixing it (attempt {step + 2}/{MAX_STEPS})…",
#                 })

#         msgs.append({
#             "role": "assistant",
#             "content": turn["text"] or None,
#             "tool_calls": [{
#                 "id": call["id"],
#                 "type": "function",
#                 "function": {"name": "run_python_code", "arguments": sent_arguments},
#             }],
#         })
#         msgs.append({"role": "tool", "tool_call_id": call["id"], "content": feedback})

#     yield sse(ERROR, {"message": "⚠️ Stopped after too many steps without finishing."})





"""
crm_chat/chat_agent.py  (Responses API version)
===============================================
One small agent loop.  The model decides BY ITSELF which tool to use:

    * web_search       OpenAI's built-in web search (runs inside OpenAI; citations come back)
    * run_python_code  YOUR Azure sandbox (files never leave your infrastructure)
    * neither          a normal text answer (RAG excerpts, memory and chat history are in the prompt)

Loop:
    model call (streamed)
        -> text answer                         -> done (web sources appended if missing)
        -> web search                          -> happens inside the same response, no work for us
        -> run_python_code                     -> run it in the Azure sandbox, send the result back
              -> file produced                 -> (pptx: layout check first) save it, model writes a summary
              -> stdout (inspect step)         -> send stdout back, model continues
              -> error / traceback             -> send the REAL error back, model fixes its own code

Stateless by default (store=False + encrypted reasoning items), so OpenAI does not keep the
conversation.  Set settings.OPENAI_STORE_RESPONSES = True to use previous_response_id instead.
"""
import asyncio
import base64
import json
import logging
import textwrap

from .pptx_quality import check_deck, format_issues
from .sandbox_client import run_sandbox_with_events
from .sse_events import (
    sse, sse_token,
    TOOL_CALL, SANDBOX_START, SANDBOX_PROGRESS, SANDBOX_COMPLETE,
    DOCUMENT_START, DOCUMENT_COMPLETE, RETRY, ERROR,
)

logger = logging.getLogger(__name__)

MAX_STEPS = 8          # model rounds per user message (code inspect / build / fix)
MAX_FIX_ROUNDS = 2     # how many times a deck with layout problems is sent back to the model

# --------------------------------------------------------------------------
# Tools.  Edit the library list to match what is really installed in your Azure sandbox.
# --------------------------------------------------------------------------
RUN_PYTHON_TOOL = {
    "type": "function",
    "name": "run_python_code",
    "description": (
        "Run a Python script in a private sandbox to create or edit files "
        "(xlsx, pptx, docx, pdf, csv). "
        "Libraries: openpyxl, pandas, numpy, python-pptx, python-docx, reportlab, Pillow. "
        "User files are read-only in /tmp/inputs. "
        "Save every deliverable in /tmp/outputs/<filename>. "
        "PowerPoint: python-pptx text boxes do NOT wrap by default, so always set text_frame.word_wrap = True "
        "and keep all text inside its shape and inside the slide. "
        "If the script fails you receive the traceback: fix it and call the tool again. "
        "You may first run a script that only print()s to inspect an input file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Complete, self-contained Python script."},
            "filename": {
                "type": "string",
                "description": "Output filename with extension (e.g. report.pptx).",
            },
        },
        "required": ["code", "filename"],
        "additionalProperties": False,
    },
    "strict": False,
}

# Only creates the folders; the model writes its own imports.
PRELUDE = (
    "import os\n"
    "os.makedirs('/tmp/inputs', exist_ok=True)\n"
    "os.makedirs('/tmp/outputs', exist_ok=True)\n\n"
)

# --------------------------------------------------------------------------
# Prompt note about the files the sandbox can see (names only — cheap)
# --------------------------------------------------------------------------
_FILES_NOTE = (
    "\n\n[FILES AVAILABLE TO run_python_code]\n{names}\n"
    "They are read-only in /tmp/inputs. Decide each file's role from the user's latest message "
    "(template / style reference, logo or image to embed, data source, or file to edit) and follow "
    "their instructions exactly. Save new deliverables under a NEW name in /tmp/outputs. "
    "If the user asks to change an earlier document, edit the previous file and keep its content.\n"
)

_PPTX_NOTE = (
    "PowerPoint rules: start from the attached deck with Presentation('/tmp/inputs/<file>'), delete its sample "
    "slides, then add slides with prs.slides.add_slide(prs.slide_layouts[i]) using the layout indexes from the "
    "TEMPLATE REPORT. Keep every shape inside the layout's SAFE AREA from the report and compute positions from "
    "those numbers (rows, columns, grids), never from assumed slide dimensions. Fill the title placeholder with "
    "plain text only (do not change its font, size, colour or position) and do not put your own text on the "
    "template's bands, side bars, logo or footer. Size fonts so text fits its box (about 11-16 pt in cards, never "
    "below 9 pt) and delete unused placeholders before saving. The server checks the saved deck and returns any "
    "layout problems to you to fix before it is delivered.\n"
)


def files_note(names) -> str:
    """System-prompt text listing the files available to the sandbox (empty if none)."""
    names = [n for n in names if n]
    if not names:
        return ""
    text = _FILES_NOTE.format(names="\n".join(f"- /tmp/inputs/{n}" for n in names))
    if any(n.lower().endswith(".pptx") for n in names):
        text += _PPTX_NOTE
    return text


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _strip_fences(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines)
    return textwrap.dedent(raw).strip("\n")


def _inputs_code(files: dict) -> str:
    """Python source that writes each input file into /tmp/inputs inside the sandbox."""
    if not files:
        return ""
    out = ["import base64 as _b64"]
    for name, data in files.items():
        b64 = base64.b64encode(data).decode("ascii")
        out.append(f"open('/tmp/inputs/' + {name!r}, 'wb').write(_b64.b64decode('{b64}'))")
    out.append("del _b64\n")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Chat-Completions-style messages  ->  Responses API input
# --------------------------------------------------------------------------
def _convert_content(role, content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        t = p.get("type") if isinstance(p, dict) else None
        if t in ("text", "input_text", "output_text"):
            parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": p.get("text", "")})
        elif t == "image_url":
            img = p.get("image_url")
            url = img.get("url") if isinstance(img, dict) else img
            parts.append({"type": "input_image", "image_url": url})
        else:
            parts.append(p)
    return parts


def to_responses_input(messages):
    """(instructions, input_items): the first system message becomes `instructions`."""
    instructions, items = None, []
    for i, m in enumerate(messages):
        role = m.get("role")
        if i == 0 and role == "system":
            instructions = m.get("content") or ""
            continue
        items.append({"role": role, "content": _convert_content(role, m.get("content"))})
    return instructions, items


# --------------------------------------------------------------------------
# Small helpers that work for SDK objects and plain dicts
# --------------------------------------------------------------------------
def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _dump(item):
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    return dict(item)


def _collect_queries(resp):
    queries = []
    for item in (_get(resp, "output") or []):
        if _get(item, "type") != "web_search_call":
            continue
        action = _get(item, "action")
        q = _get(action, "query")
        if q:
            queries.append(q)
        for q2 in (_get(action, "queries") or []):
            if q2 and q2 not in queries:
                queries.append(q2)
    return queries


def _collect_sources(resp):
    found = []
    for item in (_get(resp, "output") or []):
        if _get(item, "type") != "message":
            continue
        for part in (_get(item, "content") or []):
            for ann in (_get(part, "annotations") or []):
                if _get(ann, "type") == "url_citation":
                    url = _get(ann, "url")
                    if url and url not in [u for u, _ in found]:
                        found.append((url, _get(ann, "title")))
    return found


def _sources_markdown(resp, text):
    """Sources the model cited but did not already link inside its answer text."""
    missing = [(u, t) for u, t in _collect_sources(resp) if u.split("?")[0] not in text]
    if not missing:
        return ""
    return "\n\n**Sources**\n" + "\n".join(f"- [{t or u}]({u})" for u, t in missing)


# --------------------------------------------------------------------------
# One streamed model call
# --------------------------------------------------------------------------
async def _model_turn(client, kwargs: dict, out: dict, text_sink: list):
    """Stream ONE Responses call.  Yields SSE strings; stores the final response in `out`."""
    stream = await client.responses.create(**kwargs, stream=True)
    text, final, error = [], None, None
    async for ev in stream:
        t = _get(ev, "type")
        if t == "response.output_text.delta":
            d = _get(ev, "delta") or ""
            text.append(d)
            text_sink.append(d)
            yield sse_token(d)
        elif t == "response.output_item.added":
            item = _get(ev, "item")
            kind = _get(item, "type")
            if kind == "function_call":
                yield sse(TOOL_CALL, {"name": _get(item, "name", "run_python_code"), "message": "✍️ Writing Python code…"})
            elif kind == "web_search_call":
                yield sse(TOOL_CALL, {"name": "web_search", "message": "🔎 Searching the web…"})
        elif t in ("response.completed", "response.incomplete"):
            final = _get(ev, "response")
        elif t == "response.failed":
            final = _get(ev, "response")
            error = _get(_get(final, "error"), "message") or "The model response failed."
        elif t == "error":
            error = _get(ev, "message") or "Streaming error from OpenAI."
    out.update(text="".join(text), response=final, error=error)


# --------------------------------------------------------------------------
# run_python_code handler
# --------------------------------------------------------------------------
async def _handle_code_call(call, st, ctx, box):
    """Runs one run_python_code call. Yields SSE strings, puts the tool output text in box['output']."""
    raw_args = _get(call, "arguments") or ""
    feedback, args, code = None, {}, ""
    try:
        args = json.loads(raw_args)
        code = args["code"]
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.error("Bad tool arguments from model: %.300s", raw_args)
        box["bad_args"] = True
        feedback = (
            "Your tool call was cut off or not valid JSON. "
            "Send the complete script again in a single run_python_code call."
        )
        if ctx["finish"] == "length":
            yield sse(ERROR, {
                "message": "⚠️ The AI was cut off while writing the code (token limit reached). "
                           "Please increase your Max Tokens setting.",
            })

    if feedback is None:
        try:
            files = await ctx["load_input_files"]()
        except Exception as exc:  # never let a storage problem kill the chat
            logger.warning("Could not load input files: %s", exc)
            files = {}

        full_code = PRELUDE + _inputs_code(files) + _strip_fences(code)
        logger.info("[AI CODE] filename=%s chars=%d", args.get("filename"), len(code))

        job, err = None, None
        async for e in run_sandbox_with_events(full_code, ctx["sandbox_url"]):
            if e["type"] == SANDBOX_START:
                yield sse(SANDBOX_START, {"message": e.get("message", "Running code…")})
            elif e["type"] == SANDBOX_PROGRESS:
                yield sse(SANDBOX_PROGRESS, {
                    "elapsed": e.get("elapsed", 0),
                    "message": e.get("message", "Running…"),
                })
            elif e["type"] == SANDBOX_COMPLETE:
                job = e["result"]
            elif e["type"] == ERROR:
                err = e.get("message", "Sandbox error")

        if job and job.get("success") and job.get("file_b64"):
            name = job.get("filename") or args.get("filename") or "document"
            ext = name.rsplit(".", 1)[-1] if "." in name else "file"
            data = base64.b64decode(job["file_b64"])

            # Layout check (PowerPoint only), done on YOUR server. A deck with visible layout
            # problems is NOT delivered; the problems go back to the model to fix first.
            layout_issues = []
            if ext.lower() == "pptx":
                layout_issues = await asyncio.to_thread(check_deck, data)
            if layout_issues and st["fix_rounds"] < MAX_FIX_ROUNDS:
                st["fix_rounds"] += 1
                feedback = format_issues(layout_issues)
                yield sse(RETRY, {
                    "attempt": st["fix_rounds"],
                    "max": MAX_FIX_ROUNDS,
                    "message": "🎨 Improving the slide layout…",
                })
            else:
                if layout_issues:
                    logger.warning("Delivering deck with remaining layout issues: %s", layout_issues)
                    ctx["result"].setdefault("warnings", []).extend(layout_issues)
                yield sse(DOCUMENT_START, {
                    "document_type": ext,
                    "message": f"Saving your {ext.upper()} document…",
                })
                saved = await ctx["save_file"](name, data)
                yield sse(DOCUMENT_COMPLETE, {
                    "document_type": ext,
                    "filename": name,
                    "file_url": saved["url"],
                })
                ctx["result"]["files"].append({"filename": name, **saved})
                st["file_saved"] = True
                feedback = (
                    f"File '{name}' was created and saved. "
                    "Reply with one short line summarising what it contains. "
                    "Do not repeat the file contents."
                )
        elif job and job.get("success"):
            feedback = ("STDOUT:\n" + (job.get("stdout") or "(the script printed nothing)"))[:3000]
        else:
            feedback = (job or {}).get("error") or err or "Unknown sandbox error"
            tb = (job or {}).get("traceback")
            if tb:
                feedback += "\n" + tb
            feedback = feedback[:3000]
            yield sse(RETRY, {
                "attempt": ctx["step"] + 1,
                "max": MAX_STEPS,
                "message": f"⚠️ Code error — the AI is fixing it (attempt {ctx['step'] + 2}/{MAX_STEPS})…",
            })
    box["output"] = feedback


# --------------------------------------------------------------------------
# The agent loop
# --------------------------------------------------------------------------
async def run_chat_agent(
    *,
    client,
    base_kwargs: dict,
    messages: list,
    load_input_files,
    sandbox_url,
    save_file,
    result: dict,
    text_sink: list,
    allow_tools: bool = True,
    web_search: bool = True,
    web_search_options: dict = None,
    max_tool_calls: int = None,
    store: bool = False,
):
    """
    Async generator that yields SSE strings.

    base_kwargs         model, max_output_tokens, and either `reasoning` ({"effort": ...}) or `temperature`
                        (do NOT include input, instructions, tools, stream or store)
    messages            chat payload: first item = system prompt, then RAG/memory/history/user message
    load_input_files    async () -> {filename: bytes}; called ONLY when the model runs code
    sandbox_url         Azure sandbox start URL
    save_file           async (filename, bytes) -> {"path": ..., "url": ...}
    result              dict that is filled with: text, finish, files, searches, sources, warnings
    text_sink           list that receives every streamed text piece (for the FINAL event)
    web_search          offer OpenAI's built-in web search; the model decides whether to use it
    web_search_options  extra options for the web_search tool, e.g. {"filters": {"allowed_domains": [...]}}
    max_tool_calls      cap on built-in tool calls (searches) per model round
    store               False = stateless (default, nothing kept by OpenAI); True = previous_response_id
    """
    instructions, input_items = to_responses_input(messages)
    result.update(text="", files=[], finish=None, searches=[], sources=[])

    tools = []
    if allow_tools:
        tools.append(RUN_PYTHON_TOOL)
    if web_search:
        tools.append({"type": "web_search", **(web_search_options or {})})

    st = {"fix_rounds": 0, "file_saved": False}
    pending, prev_id = input_items, None

    for step in range(MAX_STEPS):
        kw = {**base_kwargs, "input": pending, "store": store}
        if instructions:
            kw["instructions"] = instructions
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = "none" if st["file_saved"] else "auto"
            if max_tool_calls and web_search:
                kw["extra_body"] = {"max_tool_calls": int(max_tool_calls)}
        if "reasoning" in base_kwargs and not store:
            kw["include"] = ["reasoning.encrypted_content"]   # lets reasoning travel with stateless calls
        if store and prev_id:
            kw["previous_response_id"] = prev_id

        turn: dict = {}
        async for ev in _model_turn(client, kw, turn, text_sink):
            yield ev
        result["text"] += turn["text"]

        resp = turn["response"]
        if turn["error"] or resp is None:
            yield sse(ERROR, {"message": f"⚠️ {turn['error'] or 'No response from the model.'}"})
            return

        reason = _get(_get(resp, "incomplete_details"), "reason")
        finish = "length" if (_get(resp, "status") == "incomplete" and reason == "max_output_tokens") else "stop"
        result["finish"] = finish

        for q in _collect_queries(resp):
            result["searches"].append(q)
            logger.info("[WEB SEARCH] query=%r", q)          # audit trail: what left your system as a query

        output_items = list(_get(resp, "output") or [])
        calls = [it for it in output_items if _get(it, "type") == "function_call"]

        if not calls:                                        # final answer
            sources = _collect_sources(resp)
            result["sources"] = [{"url": u, "title": t} for u, t in sources]
            extra = _sources_markdown(resp, result["text"])
            if extra:
                result["text"] += extra
                text_sink.append(extra)
                yield sse_token(extra)
            return

        # ---- run the function calls the model asked for ----
        ctx = {"load_input_files": load_input_files, "sandbox_url": sandbox_url, "save_file": save_file,
               "result": result, "finish": finish, "step": step}
        outputs, bad_ids = [], set()
        for call in calls:
            box = {}
            if _get(call, "name") == "run_python_code":
                async for ev in _handle_code_call(call, st, ctx, box):
                    yield ev
                if box.get("bad_args"):
                    bad_ids.add(_get(call, "call_id"))
            else:
                box["output"] = f"Unknown tool '{_get(call, 'name')}'. Use run_python_code or web search."
            outputs.append({"type": "function_call_output", "call_id": _get(call, "call_id"), "output": box["output"]})

        if store:
            pending, prev_id = outputs, _get(resp, "id")
        else:
            echoed = []
            for it in output_items:
                d = _dump(it)
                if d.get("call_id") in bad_ids:               # never send broken JSON back to the API
                    d["arguments"], d["status"] = "{}", "completed"
                echoed.append(d)
            input_items = input_items + echoed + outputs
            pending = input_items

    yield sse(ERROR, {"message": "⚠️ Stopped after too many steps without finishing."})