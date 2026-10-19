"""A stand-in for whisper.cpp's ``whisper-server``: same flags and HTTP API (as far as
voicestream uses them), scripted replies, and the real one's quirks.

Behaviour comes from ``<model>.json`` next to the model file (``-m``)::

    {"load_s": 0.2,                  # time before the port opens (the real one loads the model first)
     "exit_at_start": 0,             # exit with this code before serving (bad flags exit 0 for real)
     "exit_after": 2,                # serve this many inference requests, then die
     "replies": [{...}, ...],        # verbose_json bodies, used in order (the last one repeats)
     "by_language": {"fr": {...}},   # reply for a requested language (wins over "replies")
     "status": 400}                  # answer every inference with this status

Every inference request is appended to ``<model>.requests.jsonl`` (form fields, file size).
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def arg(name: str, default: str | None = None) -> str | None:
    argv = sys.argv[1:]
    return argv[argv.index(name) + 1] if name in argv else default


model = Path(arg("-m") or "")
if not model.is_file():
    sys.exit(3)  # like the real one: a missing model exits 3
script = json.loads(Path(str(model) + ".json").read_text()) if Path(str(model) + ".json").is_file() else {}
if "exit_at_start" in script:
    print("error: invalid argument", file=sys.stderr, flush=True)
    sys.exit(int(script["exit_at_start"]))
prefix = arg("--request-path", "") or ""
log_path = Path(str(model) + ".requests.jsonl")
served = 0
lock = threading.Lock()
replies = list(script.get("replies") or [{"text": "", "segments": [], "language": "english"}])

time.sleep(float(script.get("load_s", 0.0)))
print(f"whisper_init_from_file: loading model from '{model}'", file=sys.stderr, flush=True)
with Path(str(model) + ".argv.json").open("w") as fh:
    json.dump(sys.argv[1:], fh)


def form(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], int]:
    body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + handler.headers["Content-Type"].encode() + b"\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    size = -1
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "file":
            size = len(part.get_payload(decode=True) or b"")
        elif name:
            fields[str(name)] = part.get_content().strip()
    return fields, size


def wrap60(reply: dict) -> dict:
    """What the real server does to verbose_json unless token_timestamps=false: every segment is
    cut into pieces of at most 60 characters, mid-word."""
    out = []
    for seg in reply["segments"]:
        text = seg["text"]
        for k in range(0, len(text), 60):
            out.append({**seg, "text": text[k:k + 60]})
    return {**reply, "segments": out}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # requests go to stderr like the real server
        print(fmt % args, file=sys.stderr, flush=True)

    def send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == f"{prefix}/health":
            self.send(200, b'{"status":"ok"}', "application/json")
        else:
            self.send(404, f"File Not Found ({self.path})".encode(), "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        global served
        if self.path != f"{prefix}/inference":
            self.send(404, f"File Not Found ({self.path})".encode(), "text/plain")
            return
        fields, size = form(self)
        with lock:
            served += 1
            n = served
            with log_path.open("a") as fh:
                fh.write(json.dumps({"fields": fields, "file_bytes": size, "path": self.path}) + "\n")
        if "status" in script:
            status = int(script["status"])
            if status == 400:
                self.send(400, b"Invalid request", "text/plain")
            else:
                self.send(status, b'{"error":"failed to process audio"}', "application/json")
            return
        reply = (script.get("by_language") or {}).get(fields.get("language", ""))
        if reply is None:
            reply = replies[min(n - 1, len(replies) - 1)]
        if fields.get("token_timestamps", "") not in ("false", "0", "no", "n") and reply.get("segments"):
            reply = wrap60(reply)
        self.send(200, json.dumps(reply).encode(), "application/json")
        if script.get("exit_after") and n >= int(script["exit_after"]):
            self.wfile.flush()
            os._exit(1)


server = ThreadingHTTPServer((arg("--host", "127.0.0.1") or "127.0.0.1", int(arg("--port", "8080") or 8080)), Handler)
server.serve_forever()
