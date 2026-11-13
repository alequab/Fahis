"""Connects the backend to the desktop window through a private pipe, with no network port.

The Tauri window sends one JSON request per line on stdin, and each answer goes back as one line on stdout.
"""
import asyncio
import json
import threading
from pathlib import Path

import i18n

PAGE = Path(__file__).parent / "app" / "index.html"


async def answer(app, line):
    """Turn one request line into one answer. Requests carry an id so answers can arrive in any order."""
    try:
        request = json.loads(line)
        request_id = request["id"]
        kind = request["kind"]
        payload = request.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return {"id": None, "status": 400, "data": {"error": "Bad request"}}

    lang = i18n.normalize(payload.get("lang"))
    try:
        if kind == "state":
            return {"id": request_id, "status": 200, "data": app.state(lang)}
        if kind == "i18n":
            return {"id": request_id, "status": 200, "data": {"default": i18n.current_language(), "text": i18n.page_text(lang)}}
        if kind == "api":
            query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
            body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
            status, data = await app.api(payload.get("method"), payload.get("path"), query, body)
            return {"id": request_id, "status": status, "data": data}
    except Exception as error:  # The page waits for every answer, so a failure must still send one.
        return {"id": request_id, "status": 500, "data": {"error": str(error) or type(error).__name__}}
    return {"id": request_id, "status": 404, "data": {"error": "Not found"}}


async def serve(app, requests, replies):
    """Answer requests until the window closes the pipe, while the app talks to the car."""
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def read_lines():
        for line in requests:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    async def respond(line):
        reply = await answer(app, line)
        replies.write(json.dumps(reply).encode() + b"\n")
        replies.flush()

    threading.Thread(target=read_lines, daemon=True).start()
    car = asyncio.create_task(app.run())
    active = set()
    try:
        while (line := await lines.get()) is not None:
            task = asyncio.create_task(respond(line))
            active.add(task)
            task.add_done_callback(active.discard)
    finally:
        car.cancel()
        await asyncio.gather(car, return_exceptions=True)
