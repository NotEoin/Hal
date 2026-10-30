"""Tests for `halagent.tokens`: server counts, the missing-route memo and calibration."""

import pytest

from halagent.tokens import ESTIMATE_FACTOR, TokenCounter

BODY = {"model": "m", "messages": [{"role": "user", "content": "x" * 400}]}


class FakePost:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, path, body):
        self.calls.append(path)
        return self.replies.pop(0)


def test_server_count_is_exact_and_uses_the_api_route():
    post = FakePost((200, {"input_tokens": 123}))
    assert TokenCounter("anthropic", 1000, post).count(BODY) == (123, True)
    assert post.calls == ["/v1/messages/count_tokens"]


@pytest.mark.parametrize("status", [403, 404, 405])
def test_missing_route_is_remembered_and_estimate_used(status):
    post = FakePost((status, None))
    counter = TokenCounter("responses", 1000, post)
    tokens, exact = counter.count(BODY)
    assert not exact and tokens > 0
    counter.count(BODY)
    assert post.calls == ["/v1/responses/input_tokens"]


def test_count_is_skipped_while_far_below_the_window():
    post = FakePost((200, {"input_tokens": 100}))
    counter = TokenCounter("responses", 1000, post)
    assert counter.count(BODY) == (100, True)
    assert counter.count(BODY)[1] is False
    assert len(post.calls) == 1


def test_count_is_repeated_when_the_last_count_is_near_the_window():
    post = FakePost((200, {"input_tokens": 600}), (200, {"input_tokens": 650}))
    counter = TokenCounter("responses", 1000, post)
    counter.count(BODY)
    assert counter.count(BODY) == (650, True)


def test_unreadable_count_reply_falls_back_to_the_estimate():
    counter = TokenCounter("anthropic", 1000, FakePost((200, {"oops": 1})))
    assert counter.count(BODY)[1] is False


def test_observe_calibrates_the_estimate_from_a_real_prompt_count():
    counter = TokenCounter("openai", 1000)
    before = counter.estimate(1000)
    counter.observe(1000, 500)
    assert counter.estimate(1000) > before
    assert counter.estimate(1000) == int(-(-1000 * 0.5 * ESTIMATE_FACTOR // 1))


def test_unknown_api_is_rejected():
    with pytest.raises(ValueError, match="grpc"):
        TokenCounter("grpc", 10)
