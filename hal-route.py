"""Sits between Claude Code and the llama.cpp router and rewrites each request by a policy.

route (mode combined): on every new user turn a decision model (/v1/systemone) picks the
  fast or the strong model, and the turn's requests go there; the strong model loses its
  shell and edit tools, so it delegates that work to subagents on the fast model.
effort (mode hybrid-auto): the server thinks at medium, and requests at a turn's decision
  points (a user message, a subagent report, a failing tool result) get a trailing system
  note asking for xhigh. The note follows the cached prefix, so the prompt cache survives.
swap (modes orchestrate*): the strong model orchestrates without shell or edit tools and
  subagents run on the fast model; the two take turns on the GPU. Before a swap the strong
  model's KV cache is saved to disk, and it is restored when the strong model comes back.
  Both models get the effort policy too.

Stdlib only.
"""
import argparse
import hashlib
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Tools that write or execute. Without them the strong model has to hand that work to a
# subagent, which runs on the fast model.
DELEGATED_TOOLS = {"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"}
# Claude Code wraps context it injects into user messages in these tags
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# A finished subagent reports back as a user message; it continues the turn
AGENT_REPORT = re.compile(r"<agent-message\b|<task-notification\b")
# Signs that a shell command failed, checked in Bash results only: source code read with
# other tools is full of names like ValueError
FAILURE = re.compile(r"Traceback \(most recent call last\)|\bFAILED\b|\b\d+ failed\b"
                     r"|\b[A-Z]\w*Error:|^Exit code (?!1$)[1-9]\d*$", re.M)
# Exit code 1 is a failure, except from a search or comparison, where it means "no match"
EXIT_1 = re.compile(r"^Exit code 1$", re.M)
SEARCH_COMMAND = re.compile(r"^\s*(grep|egrep|fgrep|rg|ag|git grep|diff|cmp|test|\[)\b")
# Tools whose result is a subagent's report
AGENT_TOOLS = {"Agent", "Task"}
# Wording of the xhigh effort in modes/qwen3.8-chat.jinja
XHIGH_NOTE = ("Reasoning effort is set to xhigh. Please think carefully through the task, "
              "validate key assumptions, consider plausible alternatives, and prioritize "
              "correctness, consistency, and clarity in the final answer.")
STOP_REASON = re.compile(rb'"stop_reason"\s*:\s*"(\w+)"')
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


class Policy:
    """Base for the policies: logging, and hooks that run around each forwarded request."""

    def __init__(self, args: argparse.Namespace):
        self.upstream = urllib.parse.urlsplit(args.upstream)
        self.note = open(args.prompt, encoding="utf-8").read().strip() if args.prompt else ""
        self.log = open(args.log, "a", encoding="utf-8") if args.log else sys.stderr
        self.lock = threading.Lock()

    def record(self, msg: str) -> None:
        with self.lock:
            self.log.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            self.log.flush()

    def route(self, req: dict) -> dict:
        """Returns a /v1/messages request rewritten for the model that should answer it."""
        return req

    def begin(self, req: dict, path: str):
        """Runs before a POST is forwarded and may rename its model; returns a ticket."""
        return None

    def end(self, ticket, stop_reason: str | None) -> None:
        """Runs after the response to begin()'s request has been passed on."""


class Router(Policy):
    """Policy route: holds the decision made for each conversation's turn."""

    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.fast = args.fast
        self.strong = args.strong
        self.classifier = args.classifier
        self.default = args.default
        self.force = args.force
        self.turns: dict[str, str] = {}

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
        key = conversation_key(messages)
        # Claude Code appends system messages (such as the token budget) after the user turn
        last = next((m for m in reversed(messages) if m.get("role") != "system"), {})
        text = user_text(last)
        if text:
            if self.force:
                choice, p_strong = self.force, float("nan")
            else:
                choice, p_strong = self.classify(text)
            with self.lock:
                self.turns[key] = choice
            self.record(f"turn -> {choice} (p_strong={p_strong:.2f}): {text[:80]!r}")
        with self.lock:
            choice = self.turns.get(key, self.default)
        if choice == "fast":
            req["model"] = self.fast
            return req
        return orchestrate(req, self.note)


class EffortRouter(Policy):
    """Policy effort: asks for xhigh reasoning at a turn's decision points only."""

    def route(self, req: dict) -> dict:
        if req.get("tools"):
            self.record(with_effort(req))
        return req


class SwapError(Exception):
    pass


class SwapRouter(Policy):
    """Policy swap: the strong and the fast model take turns on the GPU."""

    QUIET = 2.0  # seconds without a request before the loaded model may be swapped out
    # A subagent that has made no request for this long (a stuck or killed one) no longer
    # holds the fast model; a long test run between two requests stays well inside it
    AGENT_STALE = 900
    # Requests this large without tools are the conversation's own (compaction), not side
    # requests that may go to whichever model is loaded
    BIG_CHARS = 50_000
    LOAD_TIMEOUT = 900

    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.strong = args.strong
        self.fast = args.fast
        self.slots = args.slots
        self.cond = threading.Condition()
        self.active = self.loaded_model() or self.strong
        self.inflight = 0
        self.switching = False
        self.last_done = 0.0
        self.agents: dict[str, float] = {}  # subagent conversation -> last activity
        self.saved: list[int] = []

    def owner(self, req: dict, path: str) -> str | None:
        """Returns the model a request must run on, or None if either will do."""
        if not path.endswith("/v1/messages") or req.get("model") not in (self.strong, self.fast):
            return None
        if req.get("tools") or len(json.dumps(req.get("messages") or [])) > self.BIG_CHARS:
            return req["model"]
        return None

    def route(self, req: dict) -> dict:
        if not req.get("tools"):
            return req
        self.record(f"{req.get('model')}: {with_effort(req)}")
        if req.get("model") == self.strong:
            return orchestrate(req, self.note)
        return req

    def begin(self, req: dict, path: str):
        owner = self.owner(req, path)
        key = conversation_key(req.get("messages") or []) if owner == self.fast else None
        with self.cond:
            while True:
                if not self.switching:
                    if owner is None:
                        if "model" in req:
                            req["model"] = self.active
                        self.inflight += 1
                        return (None,)
                    if owner == self.active:
                        if key is not None:
                            self.agents[key] = time.monotonic()
                        self.inflight += 1
                        return (key,)
                    if self.can_leave():
                        self.switching = True
                        break
                self.cond.wait(1.0)
        try:
            self.switch(owner)
        except (SwapError, OSError, ValueError) as e:
            self.record(f"swap to {owner} failed: {e}")
            with self.cond:
                self.active = self.loaded_model() or self.active
                self.switching = False
                self.cond.notify_all()
            raise SwapError(f"could not load {owner}: {e}") from e
        # Claimed under the same lock, so a request waiting for the other model can't swap
        # straight back before this one starts
        with self.cond:
            self.active = owner
            self.switching = False
            if key is not None:
                self.agents[key] = time.monotonic()
            self.inflight += 1
            self.cond.notify_all()
        return (key,)

    def end(self, ticket, stop_reason: str | None) -> None:
        if ticket is None:
            return
        (key,) = ticket
        with self.cond:
            self.inflight -= 1
            self.last_done = time.monotonic()
            if key is not None:
                # A subagent is done when it answers without asking for a tool
                if stop_reason in ("end_turn", "stop_sequence"):
                    self.agents.pop(key, None)
                else:
                    self.agents[key] = time.monotonic()
            self.cond.notify_all()

    def can_leave(self) -> bool:
        """Whether the loaded model may be swapped out now; called with the lock held."""
        now = time.monotonic()
        if self.inflight or now - self.last_done < self.QUIET:
            return False
        if self.active == self.fast:
            # The orchestrator waits for every running subagent, so swaps don't thrash
            self.agents = {k: t for k, t in self.agents.items() if now - t < self.AGENT_STALE}
            return not self.agents
        return True

    def switch(self, target: str) -> None:
        start = time.monotonic()
        leaving = self.active
        if leaving == self.strong:
            self.saved = self.save_slots()
        self.call("/models/unload", {"model": leaving}, check=False)
        self.wait_status(leaving, "unloaded")
        self.call("/models/load", {"model": target})
        self.wait_status(target, "loaded")
        restored = self.restore_slots() if target == self.strong else 0
        self.record(f"swap {leaving} -> {target} in {time.monotonic() - start:.1f}s"
                    + (f", restored {restored} cached tokens" if target == self.strong else ""))

    def save_slots(self) -> list[int]:
        saved = []
        for i in range(self.slots):
            try:
                r = self.call(f"/slots/{i}?action=save",
                              {"model": self.strong, "filename": f"{self.strong}-{i}.bin"})
                if r.get("n_saved"):
                    saved.append(i)
            except (OSError, ValueError) as e:
                self.record(f"saving slot {i} failed: {e}")
        return saved

    def restore_slots(self) -> int:
        tokens = 0
        for i in self.saved:
            try:
                r = self.call(f"/slots/{i}?action=restore",
                              {"model": self.strong, "filename": f"{self.strong}-{i}.bin"})
                tokens += r.get("n_restored", 0)
            except (OSError, ValueError) as e:
                self.record(f"restoring slot {i} failed: {e}; its prompt will be reprocessed")
        return tokens

    def call(self, path: str, body: dict, check: bool = True) -> dict:
        url = urllib.parse.urlunsplit(self.upstream._replace(path=path.split("?")[0],
                                                             query=path.partition("?")[2]))
        req = urllib.request.Request(url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if check:
                raise SwapError(f"{path}: {e.code} {e.read()[:200]!r}") from e
            return {}

    def statuses(self) -> dict[str, str]:
        url = urllib.parse.urlunsplit(self.upstream._replace(path="/models"))
        with urllib.request.urlopen(url, timeout=10) as r:
            return {m["id"]: m.get("status", {}).get("value", "")
                    for m in json.load(r).get("data", [])}

    def loaded_model(self) -> str | None:
        try:
            statuses = self.statuses()
        except (OSError, ValueError):
            return None
        return next((m for m in (self.strong, self.fast) if statuses.get(m) == "loaded"), None)

    def wait_status(self, name: str, value: str) -> None:
        deadline = time.monotonic() + self.LOAD_TIMEOUT
        while time.monotonic() < deadline:
            status = self.statuses().get(name)
            if status == value:
                return
            if status == "failed":
                raise SwapError(f"{name} failed to load")
            time.sleep(0.5)
        raise SwapError(f"{name} not {value} after {self.LOAD_TIMEOUT}s")


def conversation_key(messages: list) -> str:
    """Identifies a conversation by its first message, which never changes."""
    first = messages[0] if messages else {}
    return hashlib.sha1(json.dumps(first, sort_keys=True).encode()).hexdigest()


def orchestrate(req: dict, note: str) -> dict:
    """Returns the request with the delegated tools removed and `note` added to the system."""
    req["tools"] = [t for t in req["tools"] if t.get("name") not in DELEGATED_TOOLS]
    if note:
        req["system"] = append_system(req.get("system"), note)
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


def with_effort(req: dict) -> str:
    """Adds the xhigh note to a request at a decision point; returns the effort for the log."""
    messages = req.get("messages") or []
    reason = decision_point(messages)
    if not reason:
        return "effort medium"
    req["messages"] = list(messages) + [{"role": "system", "content": XHIGH_NOTE}]
    return f"effort xhigh ({reason})"


def decision_point(messages: list) -> str:
    """Returns why the next reply needs xhigh reasoning, or "" for a routine step."""
    turns = [m for m in messages if m.get("role") != "system"]
    last = turns[-1] if turns else {}
    if last.get("role") != "user":
        return ""
    content = last.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    results = [b for b in content or [] if b.get("type") == "tool_result"]
    if results:
        uses = {}
        if len(turns) > 1 and isinstance(turns[-2].get("content"), list):
            uses = {b.get("id"): b for b in turns[-2]["content"] if b.get("type") == "tool_use"}
        for b in results:
            use = uses.get(b.get("tool_use_id"), {})
            name = use.get("name")
            if name in AGENT_TOOLS:
                return "subagent report"
            if name == "Bash":
                text = block_text(b)
                if FAILURE.search(text):
                    return "failing command"
                if (b.get("is_error") or EXIT_1.search(text)) and not is_search(use):
                    return "failing command"
            elif b.get("is_error"):
                return "tool error"
        return ""
    text = REMINDER.sub("", "\n".join(b.get("text", "") for b in content or []
                                      if b.get("type") == "text")).strip()
    if not text:
        return ""
    return "subagent report" if AGENT_REPORT.search(text) else "user message"


def is_search(tool_use: dict) -> bool:
    """Whether a Bash call ends in a search or comparison, whose exit code 1 means no match."""
    command = str((tool_use.get("input") or {}).get("command", ""))
    last = re.split(r"(?<!\|)\|(?!\|)", command)[-1]
    return bool(SEARCH_COMMAND.search(last) or SEARCH_COMMAND.search(command))


def block_text(block: dict) -> str:
    """Returns the text of a tool_result block, whose content is a string or text blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def append_system(system, note: str):
    """Returns the Anthropic `system` field with `note` added as a final text block."""
    if not system:
        return note
    if isinstance(system, str):
        return system + "\n\n" + note
    return list(system) + [{"type": "text", "text": note}]


def anthropic_overflow(body: bytes) -> bytes | None:
    """Rewrites llama.cpp's context overflow error into the one Claude Code compacts on."""
    try:
        err = json.loads(body)["error"]
    except (ValueError, KeyError, TypeError):
        return None
    if err.get("type") != "exceed_context_size_error":
        return None
    message = f"prompt is too long: {err.get('n_prompt_tokens')} tokens > {err.get('n_ctx')} maximum"
    return json.dumps({"type": "error",
                       "error": {"type": "invalid_request_error", "message": message}}).encode()


def make_handler(router: Policy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def forward(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            path = self.path.split("?")[0]
            ticket = None
            if method == "POST" and body:
                try:
                    req = json.loads(body)
                except ValueError:
                    req = None
                if isinstance(req, dict):
                    if path.endswith("/v1/messages"):
                        req = router.route(req)
                    try:
                        ticket = router.begin(req, path)
                    except SwapError as e:
                        self.send_error(503, str(e))
                        return
                    body = json.dumps(req).encode()
            stop_reason = None
            try:
                stop_reason = self.relay(method, body)
            finally:
                router.end(ticket, stop_reason)

        def relay(self, method: str, body: bytes | None) -> str | None:
            """Passes the request upstream and the response back; returns its stop reason."""
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
                return None
            first = b""
            if resp.status == 400:
                first = resp.read()
                first = anthropic_overflow(first) or first
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            # Streams (server-sent events) end when the connection closes
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            stop_reason, tail = None, b""
            if first:
                self.wfile.write(first)
            while chunk := resp.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
                found = STOP_REASON.findall(tail + chunk)
                if found:
                    stop_reason = found[-1].decode()
                tail = chunk[-64:]
            conn.close()
            return stop_reason

        def do_GET(self):
            self.forward("GET")

        def do_POST(self):
            self.forward("POST")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", default="127.0.0.1:8079")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080")
    ap.add_argument("--policy", choices=["route", "effort", "swap"], default="route")
    ap.add_argument("--fast", help="model name of the fast model (route, swap)")
    ap.add_argument("--strong", help="model name of the strong model (route, swap)")
    ap.add_argument("--classifier", help="model name of the decision model (route)")
    ap.add_argument("--default", choices=["fast", "strong"], default="strong",
                    help="route used when the classifier can't be reached")
    ap.add_argument("--force", choices=["fast", "strong"],
                    help="route every turn this way and skip the classifier (route)")
    ap.add_argument("--slots", type=int, default=2,
                    help="KV slots of the strong model to save across a swap (swap)")
    ap.add_argument("--prompt", help="file appended to the strong model's system prompt")
    ap.add_argument("--log", help="file to log routing decisions to (default: stderr)")
    args = ap.parse_args()
    if args.policy in ("route", "swap") and not (args.fast and args.strong):
        ap.error(f"--policy {args.policy} needs --fast and --strong")
    if args.policy == "route" and not (args.classifier or args.force):
        ap.error("--policy route needs --classifier or --force")
    host, port = args.listen.rsplit(":", 1)
    router = {"route": Router, "effort": EffortRouter, "swap": SwapRouter}[args.policy](args)
    router.record(f"listening on {args.listen}, policy={args.policy}, "
                  f"fast={args.fast}, strong={args.strong}")
    ThreadingHTTPServer((host, int(port)), make_handler(router)).serve_forever()


if __name__ == "__main__":
    main()
