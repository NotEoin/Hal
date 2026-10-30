"""Tests for `halgateway.models.download` and `halgateway.models.pull` with an injected opener."""

import hashlib
import io

import pytest

from halgateway import models
from halgateway.models import download
from halgateway.models.manifest import ManifestError

COMMIT = "a" * 40
DATA = b"GGUF-synthetic-weights"
SHA = hashlib.sha256(DATA).hexdigest()


class FakeHF:
    """Stands in for the HTTPS opener; serves `payload` and records the URLs asked for."""

    def __init__(self, payload=DATA):
        self.payload = payload
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return io.BytesIO(self.payload), len(self.payload)


def manifest(tmp_path, source=f"hf:acme/models@{COMMIT}", sha=SHA):
    path = tmp_path / "models.toml"
    path.write_text(
        'schema = 1\n[[model]]\nname = "tiny"\nfile = "tiny/t.gguf"\nformat = "gguf"\n'
        f'sha256 = "{sha}"\nsize = {len(DATA)}\nsource = "{source}"\nhf_file = "t-Q4.gguf"\n'
    )
    return models.Store(path, tmp_path / "w", tmp_path / "verified.json")


def test_pull_verifies_before_rename(tmp_path):
    store = manifest(tmp_path)
    opener = FakeHF()
    seen = []
    models.pull(store, ["tiny"], opener=opener, progress=lambda n, d, t: seen.append((n, d, t)))
    assert (store.models_dir / "tiny" / "t.gguf").read_bytes() == DATA
    assert opener.urls == [f"https://huggingface.co/acme/models/resolve/{COMMIT}/t-Q4.gguf"]
    assert seen[-1] == ("tiny", len(DATA), len(DATA))
    assert not list((store.models_dir / "tiny").glob("*.part"))


def test_pull_deletes_the_temporary_file_on_a_hash_mismatch(tmp_path):
    store = manifest(tmp_path)
    with pytest.raises(download.DownloadError, match="do not match"):
        models.pull(store, ["tiny"], opener=FakeHF(b"tampered-bytes-here!!!"))
    assert list((store.models_dir / "tiny").iterdir()) == []


def test_pull_refuses_unpinned_source(tmp_path):
    for source in ("hf:acme/models@main", "local", "hf:acme/models"):
        store = manifest(tmp_path, source=source)
        with pytest.raises(ManifestError):
            models.pull(store, ["tiny"], opener=FakeHF())
    assert not store.models_dir.exists()


def test_pull_never_overwrites_a_different_file(tmp_path):
    store = manifest(tmp_path)
    target = store.models_dir / "tiny" / "t.gguf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"something else")
    with pytest.raises(download.DownloadError, match="will not overwrite"):
        models.pull(store, ["tiny"], opener=FakeHF())
    assert target.read_bytes() == b"something else"


def test_pull_skips_a_file_that_already_matches(tmp_path):
    store = manifest(tmp_path)
    target = store.models_dir / "tiny" / "t.gguf"
    target.parent.mkdir(parents=True)
    target.write_bytes(DATA)
    opener = FakeHF()
    models.pull(store, ["tiny"], opener=opener)
    assert opener.urls == []


def test_download_refuses_a_non_https_url(tmp_path):
    with pytest.raises(download.DownloadError, match="non-https"):
        download.fetch("http://example.invalid/x", tmp_path / "x", sha256=SHA, size=1, name="x")


def test_plan_lists_only_what_pull_would_fetch(tmp_path):
    store = manifest(tmp_path)
    assert [e.name for e in models.plan(store, [])] == ["tiny"]
    target = store.models_dir / "tiny" / "t.gguf"
    target.parent.mkdir(parents=True)
    target.write_bytes(DATA)
    assert models.plan(store, []) == []
