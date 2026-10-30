"""Tests for `halgateway.models.manifest`: sha256 verification of model files (REQ-SEC-79, REQ-GW-75)."""

import hashlib
import os

import pytest

from halgateway.models.manifest import ManifestError, Verifier, load_manifest

BODY = b"synthetic weights"
SIZE = len(BODY)
DIGEST = hashlib.sha256(BODY).hexdigest()
OLLAMA = "sha256:" + "a" * 64


def write(tmp_path, extra="", name="m.gguf", digest=DIGEST, fmt="gguf", size=SIZE):
    (tmp_path / name).write_bytes(BODY)
    toml = tmp_path / "models.toml"
    toml.write_text(
        f'schema = 1\n[[model]]\nname = "m"\nfile = "{name}"\nformat = "{fmt}"\n'
        f'sha256 = "{digest}"\nsize = {size}\nsource = "local"\n{extra}'
    )
    return toml


def verifier(tmp_path, **kw):
    entries = load_manifest(write(tmp_path, **kw), tmp_path)
    return Verifier(entries, tmp_path / "state" / "verified.json")


def test_listed_model_with_matching_hash_loads(tmp_path):
    assert verifier(tmp_path).verify_path(tmp_path / "m.gguf").name == "m"


def test_unlisted_model_is_refused(tmp_path):
    v = verifier(tmp_path)
    (tmp_path / "other.gguf").write_bytes(BODY)
    with pytest.raises(ManifestError, match="no entry"):
        v.verify_path(tmp_path / "other.gguf")


def test_hash_mismatch_is_refused(tmp_path):
    v = verifier(tmp_path, digest="0" * 64)
    with pytest.raises(ManifestError, match="sha256 mismatch"):
        v.verify_path(tmp_path / "m.gguf")


def test_unchanged_file_is_not_rehashed(tmp_path):
    v = verifier(tmp_path)
    v.verify_path(tmp_path / "m.gguf")
    v.verify_path(tmp_path / "m.gguf")
    assert v.hashes == 1


def test_changed_inode_forces_rehash(tmp_path):
    v = verifier(tmp_path)
    path = tmp_path / "m.gguf"
    v.verify_path(path)
    new = tmp_path / "new"
    new.write_bytes(b"x" * len(BODY))  # same size, different content and inode
    os.replace(new, path)
    with pytest.raises(ManifestError, match="sha256 mismatch"):
        v.verify_path(path)
    assert v.hashes == 2


def test_size_change_is_refused_without_hashing(tmp_path):
    v = verifier(tmp_path)
    (tmp_path / "m.gguf").write_bytes(BODY + b"!")
    with pytest.raises(ManifestError, match="size"):
        v.verify_path(tmp_path / "m.gguf")
    assert v.hashes == 0


@pytest.mark.parametrize("name,fmt", [("m.pt", "gguf"), ("m.bin", "gguf"), ("m.gguf", "pickle")])
def test_pickle_format_is_refused(tmp_path, name, fmt):
    with pytest.raises(ManifestError, match="no pickle"):
        load_manifest(write(tmp_path, name=name, fmt=fmt), tmp_path)


def test_unknown_key_and_bad_digest_are_errors(tmp_path):
    with pytest.raises(ManifestError, match="unknown keys"):
        load_manifest(write(tmp_path, extra="bogus = 1\n"), tmp_path)
    with pytest.raises(ManifestError, match="64 lowercase hex"):
        load_manifest(write(tmp_path, digest="abc"), tmp_path)


def test_unpinned_ollama_model_is_refused(tmp_path):
    with pytest.raises(ManifestError, match="not pinned"):
        verifier(tmp_path).check_ollama("gemma", OLLAMA)


def test_moved_ollama_tag_is_refused(tmp_path):
    toml = tmp_path / "models.toml"
    toml.write_text(f'schema = 1\n[[model]]\nname = "gemma"\nsource = "ollama:gemma@{OLLAMA}"\n')
    v = Verifier(load_manifest(toml, tmp_path), tmp_path / "v.json")
    assert v.check_ollama("gemma", OLLAMA).name == "gemma"
    with pytest.raises(ManifestError, match="moved"):
        v.check_ollama("gemma", "sha256:" + "b" * 64)
