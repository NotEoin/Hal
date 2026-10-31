"""Tests for `halagent.images`: `@image` references become attached images."""

import base64

import pytest

from halagent.images import MAX_IMAGES, ImageError, extract_images
from halagent.loop import Agent
from halagent.profiles import Profile, resolve_profile
from halagent.settings import AgentSettings
from halagent.tools import Toolset

PNG = b"\x89PNG\r\n\x1a\nsynthetic"


def test_an_existing_image_path_is_attached_as_base64(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    found = extract_images("look at @a.png please", tmp_path)
    assert found == [{"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}]


def test_other_at_words_and_missing_files_are_left_alone(tmp_path):
    assert extract_images("ping @alice about @missing.png and @notes.txt", tmp_path) == []


def test_a_path_outside_the_workspace_is_refused(tmp_path):
    with pytest.raises(ImageError, match="outside"):
        extract_images("@../secret.png", tmp_path)


def test_too_many_images_are_refused(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    with pytest.raises(ImageError, match="at most"):
        extract_images(" ".join(["@a.png"] * (MAX_IMAGES + 1)), tmp_path)


def test_prompt_with_an_image_reaches_a_vision_model_as_an_image_part(server, tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    server.script("I see it")
    Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path)).run("what is in @a.png")
    content = server.requests[-1]["body"]["messages"][-1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_only_profile_refuses_an_image_clearly(server, tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    assert resolve_profile("qwen3-coder:30b").values["vision"] is False
    profile = Profile("m", "x", {**resolve_profile("m").values, "vision": False}, {})
    agent = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), profile=profile)
    with pytest.raises(ValueError, match="text-only"):
        agent.run("what is in @a.png")
    assert not server.requests
