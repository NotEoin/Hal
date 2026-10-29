"""Hal's brain: a local model that streams its reply and calls the tools of installed extensions.

Each turn sends the conversation to Ollama with the few hal-hub tools that fit the
request (plus search, so the model can look for more), streams the reply as it is
generated, and runs any tool calls in between. With no extensions installed it is
a plain local chat model. Long, many-step work goes to Claude Code through the
deep_work tool, in the background.

    from halcore.brain import Brain
    brain = Brain(channel="voice")
    for text in brain.ask("what's on tomorrow?"):
        speak(text)
    brain.last_turn      # the reply, the tools run and how long it took

Every turn is appended to <state>/turns.jsonl, which the test console reads.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from . import config

PERSONA = """You are Hal, a calm, precise assistant running entirely on this computer. You look after
{user_ref}'s calendar, tasks, notes, files and computer through your tools, and answer questions
directly. You are capable and you do what is asked whenever it is possible."""

STYLE = {
    "voice": "Your words are spoken aloud: one to three short sentences, plain speech, no lists, no markdown, "
             "no emoji. Say what you did or found, not how.",
    "chat": "Reply briefly and plainly. Short lists are fine when they help; no headings.",
    "telegram": "You are replying in a phone chat: be brief, plain text, at most a short list.",
}

TOOL_RULES = """
RULE ONE, above everything below: you act through tool calls. When {user} asks you to do, remember,
schedule, book, plan, change or look up anything about their life, their plans or this computer, call
the tool FIRST and reply after it returns. Saying you did something without a tool call is a lie.
Any plan with a day or a time (lunch, a meeting, an appointment, a call) is a calendar_add.
General knowledge (facts, explanations, maths, advice) needs no tool: answer it directly.

Tools:
- When a request needs a tool, call it; do not describe what you would do. If none of the tools
  offered fits, call hal_find_tools to search for one.
- Times: pass the time words exactly as {user} said them ("thursday at ten") to calendar, task and timer tools.
- A result with needs_confirmation means the action is destructive: ask in one short sentence,
  and only when {user} agrees call the tool again with _confirmed set to true.
- Anything {user} tells you about themselves that will still be true next month, save with memory_fact.
  Anything they ask you to remember, save with memory_remember.
- For a long job (writing or changing code, researching and writing a document, anything with
  many steps), call deep_work with a complete description; it runs in the background and you
  will be told when it is done. Say you have started it.
- After a tool runs, give the outcome in a sentence or two. Never read out raw data or JSON.
"""

NO_TOOLS = """
You have no tools in this installation: answer from your own knowledge, and if asked to act on the
computer, the calendar or anything outside this conversation, say plainly that the extension for it
is not installed (extensions are added with `hal ext add <name>`).
"""

# A reply that says something was done, or that it cannot be done, when no tool ran,
# is held back and retried once.
SUSPECT = re.compile(
    r"\b(I(?:'ve| have|'ll| will)? (?:added|scheduled|set|saved|noted|created|booked|put|moved|cancell?ed|deleted|"
    r"removed|turned|sent|started|reminded|recorded|updated|remembered|stored|logged|written|made a note)|"
    r"(?:added|scheduled|noted|saved|stored|remembered) (?:it|that|this)|can'?not|can't|unable to|not able to|"
    r"(?:don't|do not) have (?:the )?(?:ability|capability|access|tools?|means)|I'?m sorry|I am sorry|"
    r"I apologi[sz]e|unfortunately)\b", re.I)
CLAIM = re.compile(r"\bI(?:'ve| have|'ll| will)? (?:added|scheduled|set|saved|noted|created|booked|put|moved|"
                   r"cancell?ed|deleted|removed|turned|sent|started|reminded|recorded|updated|stored|logged)\b", re.I)
NO_TOOLS_RETRY = ("Your draft reply was not sent: it said you did something, but you have no tools in this "
                  "installation and did nothing. Answer again: say plainly that you cannot do that here, and "
                  "that the extension for it can be added with `hal ext add <name>`.")
RETRY = ("Your draft reply was not sent: it either said something was done without a tool call, or said "
         "you cannot do it. You have tools for nearly everything. If an action was asked for, call the right "
         "tool now: memory_fact for a lasting fact about the user, memory_remember for anything to remember, "
         "calendar_add for any plan with a day or time, or hal_find_tools to search. If it was a general "
         "question, answer it from your own knowledge. Reply with exactly OK only if it is truly impossible.")

DEEP_WORK = {"type": "function", "function": {
    "name": "deep_work",
    "description": "Start a long, many-step task in the background (coding, editing files, research, writing a "
                   "document). An agent works on its own and Hal reports back when it finishes.",
    "parameters": {"type": "object", "properties": {
        "task": {"type": "string", "description": "the full task, with every detail the user gave"}},
        "required": ["task"]}}}

LEAKED = re.compile(r"^\s*(?:\[TOOL_CALLS\])?\s*([a-z_]+)\s*(\{.*\})\s*$", re.S)
SECRET_ARG = re.compile(r"pass(word)?|token|secret|api_?key|credential", re.I)     # never logged
THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def _leaked_call(content: str, names: set[str]) -> dict | None:
    """Returns a tool call the model wrote as text ("name{...json...}"), or None."""
    m = LEAKED.match(content or "")
    if not m or m.group(1) not in names:
        return None
    try:
        return {"function": {"name": m.group(1), "arguments": json.loads(m.group(2))}}
    except json.JSONDecodeError:
        return None


class Brain:
    """One conversation with the local model and the tools of installed extensions."""

    def __init__(self, channel: str = "chat", persona: str | None = None, model: str | None = None,
                 tools: bool = True, log_turns: bool = True, **overrides) -> None:
        shared, cfg = config.shared(), {**config.brain(), **{k: v for k, v in overrides.items() if v not in (None, "")}}
        self.channel = channel
        self.url = str(cfg.get("ollama_url") or shared["ollama_url"]).rstrip("/")
        self.model = model or cfg["model"] or shared["chat_model"]
        self.keep_alive = str(cfg["keep_alive"])
        self.history_turns = int(cfg["history_turns"])
        self.max_rounds = int(cfg["max_tool_rounds"])
        self.tool_limit = int(cfg["tools_per_turn"])
        self.num_ctx = int(cfg["num_ctx"])
        self.temperature = float(cfg["temperature"])
        self.user = str(cfg["user_name"] or "").strip()
        self.persona = (persona or cfg["persona"] or PERSONA).strip()
        self.deep_mode = str(cfg["deep_work"])
        self.deep_dir = Path(cfg["deep_work_dir"] or shared["vault"]).expanduser()
        self.hub = config.hub() if tools else None
        self.log_turns = log_turns
        self.history: list[dict] = []
        self.last_turn: dict = {}
        self.pending: set[str] = set()      # tools that asked for confirmation in an earlier turn
        self.deep_worker: threading.Thread | None = None
        self.on_deep_done: Callable[[str], None] | None = None

    # -- context -----------------------------------------------------------------
    def _has(self, name: str) -> bool:
        return bool(self.hub and self.hub.get(name))

    def system_prompt(self) -> str:
        user = self.user or "the user"
        rules = TOOL_RULES if self.hub else NO_TOOLS
        persona = self.persona.replace("{user_ref}", self.user or "your owner").replace("{user}", user)
        facts = ""
        if self._has("memory_profile"):
            got = self.hub.call("memory_profile", {})
            if isinstance(got.get("result"), str) and got["result"].strip():
                facts = f"\nWhat you know about {user}:\n" + got["result"][:2500]
        now = datetime.now().strftime("%A %-d %B %Y, %H:%M")
        return f"{rules.replace('{user}', user)}\n{persona}\n{STYLE.get(self.channel, STYLE['chat'])}\nIt is {now}.{facts}"

    def tools_for(self, text: str) -> list[dict]:
        deep = [DEEP_WORK] if self.deep_mode != "off" else []
        if not self.hub:
            return deep
        specs = self.hub.ollama_tools(text, self.tool_limit)
        for spec in specs:
            tool = self.hub.get(spec["function"]["name"])
            if tool is not None and tool.confirm:
                spec["function"]["parameters"]["properties"]["_confirmed"] = {
                    "type": "boolean", "description": "true only after the user has agreed"}
        return specs + deep

    def reset(self) -> None:
        self.history = []

    def warm(self) -> None:
        """Loads the model into memory so the first turn is not slow."""
        try:
            self._post({"model": self.model, "messages": [], "keep_alive": self.keep_alive,
                        "options": {"num_ctx": self.num_ctx}}, stream=False).read()
        except OSError:
            pass

    # -- model and tools -----------------------------------------------------------
    def _post(self, payload: dict, stream: bool = True):
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps({**payload, "stream": stream}).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=300)

    def _run_tool(self, name: str, args: dict, asked: set[str]) -> dict:
        if name == "deep_work":
            return self._deep_work(args.get("task", ""))
        if not self.hub:
            return {"error": "no extensions are installed"}
        # Models spell the flag several ways. A confirmation only counts when this tool asked in
        # an earlier turn and the user has answered since; otherwise the question comes back.
        confirmed = any(args.pop(k, False) in (True, "true", "yes") for k in ("_confirmed", "confirmed", "confirm"))
        if confirmed and name in self.pending:
            args["_confirmed"] = True
        out = self.hub.call(name, args)
        if "needs_confirmation" in out:
            asked.add(name)
        elif "error" in out:
            # Without this, a model often tells the user it worked.
            out["note"] = "This did NOT happen. Fix the arguments and call again, or tell the user it failed."
        return out

    def _deep_work(self, task: str) -> dict:
        if self.deep_worker and self.deep_worker.is_alive():
            return {"error": "a background task is already running"}
        if self.deep_mode == "hal":
            cmd = [str(config.ROOT / "hal"), "here", "-p", task]
        elif shutil.which("claude"):
            cmd = ["claude", "-p", task, "--permission-mode", "acceptEdits"]
        else:
            return {"error": "Claude Code is not installed, so long jobs cannot run"}
        cwd = self.deep_dir if self.deep_dir.is_dir() else Path.home()

        def work() -> None:
            try:
                proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=3600)
                reply = (proc.stdout or proc.stderr).strip() or f"finished with exit code {proc.returncode}"
            except (OSError, subprocess.TimeoutExpired) as e:
                reply = f"The background task failed: {e}"
            if self._has("memory_remember"):
                self.hub.call("memory_remember", {"text": f"Task: {task}\n\nResult:\n{reply}", "title": "Background task"})
            if self.on_deep_done:
                self.on_deep_done(reply)

        self.deep_worker = threading.Thread(target=work, name="deep-work", daemon=True)
        self.deep_worker.start()
        return {"started": True}

    def _recent(self) -> list[dict]:
        """Returns the last history_turns exchanges, starting at a user message."""
        users = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        start = users[-self.history_turns] if len(users) > self.history_turns else 0
        self.history = self.history[start:]
        return self.history

    def _stream_round(self, messages: list[dict], tools: list[dict]):
        """Yields ('text', delta) and finally ('done', (content, calls)) for one model call."""
        calls, content = [], ""
        payload = {"model": self.model, "messages": messages, "think": False, "keep_alive": self.keep_alive,
                   "options": {"temperature": self.temperature, "num_ctx": self.num_ctx}}
        if tools:
            payload["tools"] = tools
        with self._post(payload) as r:
            for line in r:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                msg = chunk.get("message", {})
                if msg.get("content"):
                    content += msg["content"]
                    yield "text", msg["content"]
                calls += msg.get("tool_calls") or []
                if chunk.get("done"):
                    break
        yield "done", (content, calls)

    # -- the turn ------------------------------------------------------------------
    def ask(self, text: str) -> Iterator[str]:
        """Yields the reply's text as it streams, running tool calls along the way.

        The first sentence of each model reply is held back until it is clear the model
        is not claiming an action it never took, or refusing something a tool can do;
        such a reply is replaced by one silent retry before anything is shown or spoken.
        """
        started = time.time()
        messages = [{"role": "system", "content": self.system_prompt()}, *self._recent(),
                    {"role": "user", "content": text}]
        tools = self.tools_for(text)
        reply, ran, first_token = "", [], None
        asked: set[str] = set()
        retried = False
        original = ""
        for _ in range(self.max_rounds + 1):
            held, released = "", False
            content, calls = "", []
            for kind, value in self._stream_round(messages, tools):
                if kind == "done":
                    content, calls = value
                    break
                if released:
                    reply += value
                    yield value
                    continue
                held += value
                if re.search(r"[.!?](\s|$)", held) and not SUSPECT.search(held) and "<think>" not in held:
                    released = True
                    first_token = first_token or time.time() - started
                    reply += held
                    yield held
            if not calls:
                leaked = _leaked_call(content, {t["function"]["name"] for t in tools})
                if leaked:
                    calls, content, held, released = [leaked], "", "", True
            messages.append({"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})})
            if calls:
                for call in calls:
                    fn = call.get("function", {})
                    name, args = fn.get("name", ""), fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    t0 = time.time()
                    out = self._run_tool(name, args, asked)
                    shown = {k: "***" if SECRET_ARG.search(k) else v for k, v in args.items()}
                    ran.append({"name": name, "args": shown, "ok": "error" not in out,
                                "confirm": "needs_confirmation" in out, "seconds": round(time.time() - t0, 2),
                                "result": json.dumps(out, default=str)[:400]})
                    messages.append({"role": "tool", "tool_name": name,
                                     "content": json.dumps(out, default=str)[:6000]})
                continue
            if not released and held:
                if not ran and not retried and SUSPECT.search(held) and self.hub:
                    retried, original = True, held
                    messages[-1:] = [{"role": "system", "content": RETRY}]
                    continue
                if not self.hub and not retried and CLAIM.search(held):
                    # Raw hal has no tools, so any claim of having acted is false.
                    retried, original = True, held
                    messages[-1:] = [{"role": "system", "content": NO_TOOLS_RETRY}]
                    continue
                if retried and held.strip().rstrip(".") == "OK":
                    held = original
                held = THINK.sub("", held)
                first_token = first_token or time.time() - started
                reply += held
                yield held
            break
        self.pending = asked
        # The whole turn, tool calls and results included, so later turns see how things were done.
        turn = [m for m in messages[1:] if m.get("role") != "system"]
        self.history += turn[-12:] if turn else [{"role": "user", "content": text}]
        self.last_turn = {"at": datetime.now().isoformat(timespec="seconds"), "channel": self.channel,
                          "model": self.model, "text": text, "reply": reply.strip(), "tools": ran,
                          "retried": retried, "first_words": round(first_token or 0, 2),
                          "seconds": round(time.time() - started, 2)}
        self._log(self.last_turn)

    def reply(self, text: str) -> str:
        """Returns the whole reply to one message."""
        return "".join(self.ask(text)).strip()

    def _log(self, turn: dict) -> None:
        if self.log_turns:
            try:
                with (config.state_dir() / "turns.jsonl").open("a") as f:
                    f.write(json.dumps(turn, default=str) + "\n")
            except OSError:
                pass
        if self._has("memory_log_conversation") and turn["reply"] and self.log_turns:
            threading.Thread(target=self.hub.call, daemon=True, args=(
                "memory_log_conversation", {"user": turn["text"], "assistant": turn["reply"],
                                            "channel": self.channel})).start()
