"""Minimal web server for the dashboard page. Listens on this laptop only."""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import i18n

PAGE = Path(__file__).with_name("dashboard.html")
COMMANDS_PAGE = Path(__file__).with_name("commands.html")


async def start(get_state, port):
    """Serve the page at /, the state for a language at /data, and its translations at /i18n."""

    async def handle(reader, writer):
        try:
            request = await reader.readline()
            while (await reader.readline()).strip():
                pass
            parts = request.decode("ascii", "replace").split()
            target = urlsplit(parts[1] if len(parts) > 1 else "")
            lang = i18n.normalize(parse_qs(target.query).get("lang", [""])[0])
            if target.path == "/":
                status, kind, body = "200 OK", "text/html; charset=utf-8", PAGE.read_bytes()
            elif target.path == "/commands":
                status, kind, body = "200 OK", "text/html; charset=utf-8", COMMANDS_PAGE.read_bytes()
            elif target.path == "/data":
                status, kind, body = "200 OK", "application/json", get_state(lang).encode()
            elif target.path == "/i18n":
                status, kind = "200 OK", "application/json"
                body = json.dumps({"default": i18n.current_language(), "text": i18n.page_text(lang)}).encode()
            else:
                status, kind, body = "404 Not Found", "text/plain", b"Not found"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
                         "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", port)
