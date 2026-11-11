"""Tests for `halcore.tunebench`: timing each role through the gateway."""

import json

from halcore import tunebench
from halgateway.client import ClientError


def sse(*docs):
    return [f"data: {json.dumps(d)}\n".encode() for d in docs] + [b"data: [DONE]\n"]


def delta(**kw):
    return {"choices": [{"delta": kw}]}


class FakeClient:
    """Answers each stream call with the next scripted list of lines, or raises it."""

    def __init__(self, *answers):
        self.answers, self.bodies = list(answers), []

    def stream(self, path, body, *, timeout_s):
        self.bodies.append(body)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        yield from answer


def ticks(*values):
    it = iter(values)
    return lambda: next(it)


def test_decode_speed_counts_reasoning_tokens_from_the_first_to_the_last():
    lines = sse(delta(reasoning_content="hm"), delta(content="a"), delta(content="b"), {"usage": {"completion_tokens": 11}})
    client = FakeClient(lines)
    # start, then one tick per token line: the first token at 2.0 s, the last at 3.0 s
    r = tunebench.measure(client, "code", clock=ticks(0.0, 2.0, 2.5, 3.0))
    assert (r.ttft_s, r.tok_s, r.tokens, r.error) == (2.0, 10.0, 11, "")
    body = client.bodies[0]
    assert body["model"] == "hal-code" and body["messages"][0]["content"] == tunebench.CODE_PROMPT
    assert body["stream_options"] == {"include_usage": True}


def test_a_refusal_or_an_empty_answer_is_reported_not_raised():
    client = FakeClient(ClientError("gateway answered 503: busy", 503), sse({"usage": {"completion_tokens": 0}}))
    assert "503" in tunebench.measure(client, "writer").error
    assert tunebench.measure(client, "chat").error == "no tokens came back"


def test_bench_runs_every_role_twice_and_render_lists_failures():
    ok = sse(delta(content="a"), delta(content="b"), {"usage": {"completion_tokens": 2}})
    results = tunebench.bench(FakeClient(ok, ok, ClientError("down"), ClientError("down")), ["chat", "writer"])
    assert [(r.role, r.run) for r in results] == [("chat", 1), ("chat", 2), ("writer", 1), ("writer", 2)]
    text = tunebench.render(results)
    assert "writer         1  failed: down" in text and text.splitlines()[0].startswith("role")
