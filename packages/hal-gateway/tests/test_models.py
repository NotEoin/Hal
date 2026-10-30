"""Tests for `halgateway.models`: verify, add_local, record_ollama and the manifest writer."""

import hashlib
import os
import stat

import pytest

from halgateway import models
from halgateway.models.manifest import ManifestError, load_manifest


class FakeOllama:
    def tags(self):
        return [{"name": "gemma:1", "digest": "d" * 64}, {"name": "q:latest", "digest": "sha256:" + "e" * 64}]


@pytest.fixture
def store(tmp_path):
    return models.Store(
        tmp_path / "cfg" / "models.toml", tmp_path / "w", tmp_path / "state" / "verified.json"
    )


def weights(tmp_path, name="m.gguf", data=b"synthetic weights"):
    path = tmp_path / "w" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def test_add_local_pins_hash_and_writes_a_private_manifest(tmp_path, store):
    path = weights(tmp_path)
    entry = models.add_local(store, path, profile="qwen3")
    assert entry.sha256 == hashlib.sha256(b"synthetic weights").hexdigest() and entry.source == "local"
    assert stat.S_IMODE(os.stat(store.manifest).st_mode) == 0o600
    again = load_manifest(store.manifest, store.models_dir)["m"]
    assert (again.sha256, again.size, again.profile) == (entry.sha256, entry.size, "qwen3")
    assert 'file = "m.gguf"' in store.manifest.read_text()


def test_add_local_refuses_pickle_files_and_duplicate_names(tmp_path, store):
    with pytest.raises(ManifestError, match="only .gguf"):
        models.add_local(store, weights(tmp_path, "m.pt"))
    models.add_local(store, weights(tmp_path))
    with pytest.raises(ManifestError, match="already"):
        models.add_local(store, weights(tmp_path))


def test_verify_reports_mismatch(tmp_path, store):
    path = weights(tmp_path)
    models.add_local(store, path)
    path.write_bytes(b"synthetic WEIGHTS")  # same size, other bytes
    [result] = models.verify(store, ["m"], full=True)
    assert not result.ok and "mismatch" in result.detail


def test_quick_verify_uses_cache(tmp_path, store, monkeypatch):
    models.add_local(store, weights(tmp_path))
    assert models.verify(store, [], full=True)[0].ok
    calls = []
    real = models.sha256_file
    monkeypatch.setattr("halgateway.models.manifest.sha256_file", lambda p: calls.append(p) or real(p))
    assert models.verify(store, [], full=False)[0].ok and calls == []
    assert models.verify(store, [], full=True)[0].ok and len(calls) == 1


def test_record_ollama_pins_current_digests_and_keeps_other_entries(tmp_path, store):
    models.add_local(store, weights(tmp_path), profile="p")
    added = models.record_ollama(store, ["gemma:1", "q"], FakeOllama())
    assert [e.source for e in added] == [f"ollama:gemma:1@sha256:{'d' * 64}", f"ollama:q@sha256:{'e' * 64}"]
    entries = load_manifest(store.manifest, store.models_dir)
    assert set(entries) == {"m", "gemma:1", "q"} and entries["m"].profile == "p"


def test_record_ollama_refuses_a_model_ollama_lacks(tmp_path, store):
    with pytest.raises(ManifestError, match="pull it first"):
        models.record_ollama(store, ["nope"], FakeOllama())
    assert not store.manifest.exists()


def test_verify_of_an_unknown_name_is_an_error(store):
    with pytest.raises(ManifestError, match="not in the manifest"):
        models.verify(store, ["ghost"], full=False)
