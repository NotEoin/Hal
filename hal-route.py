"""Routes each Claude Code turn to the fast or the strong local model, chosen by a classifier.

Sits between Claude Code and the llama.cpp router in mode combined. On every new user turn
it asks a decision model (/v1/systemone) whether the turn needs the strong model, and sends
the turn's requests there; the strong model loses its shell and edit tools, so it delegates
that work to subagents on the fast model. Stdlib only.
"""
import argparse
import hashlib
import http.client
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Tools that write or execute. Without them the strong model has to hand that work to a
# subagent, which runs on the fast model.
DELEGATED_TOOLS = {"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"}
# Claude Code wraps context it injects into user messages in these tags
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# A finished subagent reports back as a user message; it continues the turn
AGENT_REPORT = re.compile(r"<agent-message\b")
STATE_CHARS = 4000  # keeps the classifier prompt inside the decision model's batch
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "host"}

ROUTE_QUESTION = {"route": {
    "type": "choice",
    "instructions": "A coding assistant must handle this request. Pick the model that should do it.",
    "criteria": {
        "fast": "A small fast model is enough: a routine or mechanical task such as running "
                "commands, reading or searching code, simple questions, renames, small or "
                "obvious edits.",
        "strong": "Needs a large careful model: hard debugging, design or architecture "
                  "decisions, multi-file refactors, subtle correctness or concurrency "
                  "reasoning, or ambiguous requirements.",
    },
}}


class Router:
    """Holds the routing configuration and the decision made for each conversation's turn."""

    def __init__(self, args: argparse.Namespace):
        self.upstream = urllib.parse.urlsplit(args.upstream)
        self.fast = args.fast
        self.strong = args.strong
        self.classifier = args.classifier
        self.default = args.default
        self.note = open(args.prompt, encoding="utf-8").read().strip() if args.prompt else ""
        self.log = open(args.log, "a", encoding="utf-8") if args.log else sys.stderr
        self.turns: dict[str, str] = {}
        self.lock = threading.Lock()

    def record(self, msg: str) -> None:
        with self.lock:
            self.log.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            self.log.flush()

    def classify(self, text: str) -> tuple[str, float]:
        """Returns ("fast" or "strong", probability of strong) for one user request.

        Falls back to the configured default when the decision model can't be reached.
        """
        body = {"model": self.classifier, "state": text[-STATE_CHARS:],
                "questions": ROUTE_QUESTION}
        url = urllib.parse.urlunsplit(self.upstream._replace(path="/v1/systemone"))
        req = urllib.request.Request(url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                answer = json.load(r)["answers"]["route"]
            return answer["choice"], float(answer["probabilities"]["strong"])
        except (OSError, ValueError, KeyError) as e:
            self.record(f"classifier unavailable ({e}); using {self.default}")
            return self.default, float("nan")

    def route(self, req: dict) -> dict:
        """Returns the request rewritten for the model that should answer it."""
        # Subagents and Claude Code's small side requests already name the fast model
        if req.get("model") != self.strong:
            return req
        # Requests without tools are side requests (titles, permission checks), not turns
        if not req.get("tools"):
            req["model"] = self.fast
            return req
        messages = req.get("messages") or []
        if not messages:
            return req
        key = hashlib.sha1(json.dumps(messages[0], sort_keys=True).encode()).hexdigest()
        # Claude Code appends system messages (such as the token budget) after the user turn
        last = next((m for m in reversed(messages) if m.get("role") != "system"), {})
        text = user_text(last)
        if text:
            choice, p_strong = self.classify(text)
            with self.lock:
                self.turns[key] = choice
            self.record(f"turn -> {choice} (p_strong={p_strong:.2f}): {text[:80]!r}")
        with self.lock:
            choice = self.turns.get(key, self.default)
        if choice == "fast":
            req["model"] = self.fast
            return req
        req["tools"] = [t for t in req["tools"] if t.get("name") not in DELEGATED_TOOLS]
        if self.note:
            req["system"] = append_system(req.get("system"), self.note)
        return req


def user_text(message: dict) -> str:
    """Returns what the user typed in a message, or "" for a tool result or other role."""
    if message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in content or [] if b.get("type") == "text"]
        if any(b.get("type") == "tool_result" for b in content or []):
            return ""
    text = REMINDER.sub("", "\n".join(parts)).strip()
    return "" if AGENT_REPORT.search(text) else text


def append_system(system, note: str):
    """Returns the Anthropic `system` field with `note` added as a final text block."""
    if not system:
        return note
    if isinstance(system, str):
        return system + "\n\n" + note
    return list(system) + [{"type": "text", "text": note}]


def make_handler(router: Router):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def forward(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            if method == "POST" and self.path.split("?")[0].endswith("/v1/messages") and body:
                try:
                    body = json.dumps(router.route(json.loads(body))).encode()
                except ValueError:
                    pass
            up = router.upstream
            conn = http.client.HTTPConnection(up.hostname, up.port, timeout=7200)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            if body is not None:
                headers["Content-Length"] = str(len(body))
            try:
                conn.request(method, self.path, body=body, headers=headers)
                resp = conn.getresponse()
            except OSError as e:
                self.send_error(502, f"upstream unavailable: {e}")
                return
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            # Streams (server-sent events) end when the connection closes
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while chunk := resp.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()

        def do_GET(self):
            self.forward("GET")

        def do_POST(self):
            self.forward("POST")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", default="127.0.0.1:8079")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080")
    ap.add_argument("--fast", required=True, help="model name of the fast model")
    ap.add_argument("--strong", required=True, help="model name of the strong model")
    ap.add_argument("--classifier", required=True, help="model name of the decision model")
    ap.add_argument("--default", choices=["fast", "strong"], default="strong",
                    help="route used when the classifier can't be reached")
    ap.add_argument("--prompt", help="file appended to the strong model's system prompt")
    ap.add_argument("--log", help="file to log routing decisions to (default: stderr)")
    args = ap.parse_args()
    host, port = args.listen.rsplit(":", 1)
    router = Router(args)
    router.record(f"listening on {args.listen}, fast={args.fast}, strong={args.strong}")
    ThreadingHTTPServer((host, int(port)), make_handler(router)).serve_forever()


if __name__ == "__main__":
    main()
