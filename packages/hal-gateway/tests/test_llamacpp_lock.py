"""Tests for `halgateway.llamacpp`: the llama.cpp sha256 lock and installer (REQ-SEC-78)."""

import hashlib

import pytest

from halgateway import llamacpp
from halgateway.llamacpp import LockError, verify_archive

BODY = b"synthetic archive"
NAME = "llama-b1-bin.tar.gz"


def lock(tmp_path, digest=None, tag="b1"):
    digest = digest or hashlib.sha256(BODY).hexdigest()
    path = tmp_path / "lock.toml"
    path.write_text(f'tag = "{tag}"\n[assets]\n"{NAME}" = "{digest}"\n')
    archive = tmp_path / NAME
    archive.write_bytes(BODY)
    return path, archive


def test_matching_archive_is_accepted(tmp_path):
    path, archive = lock(tmp_path)
    verify_archive(archive, "b1", path)


def test_sha256_mismatch_stops_the_install(tmp_path):
    path, archive = lock(tmp_path, digest="0" * 64)
    with pytest.raises(LockError, match="mismatch"):
        verify_archive(archive, "b1", path)


def test_archive_missing_from_the_lock_is_refused(tmp_path):
    path, archive = lock(tmp_path)
    other = tmp_path / "other.tar.gz"
    other.write_bytes(BODY)
    with pytest.raises(LockError, match="no sha256"):
        verify_archive(other, "b1", path)


def test_other_tag_is_refused(tmp_path):
    path, archive = lock(tmp_path)
    with pytest.raises(LockError, match="not the pinned"):
        verify_archive(archive, "b2", path)


def test_bad_digest_in_the_lock_is_an_error(tmp_path):
    path, _ = lock(tmp_path, digest="xyz")
    with pytest.raises(LockError, match="64 hex"):
        llamacpp.load_lock(path)


def test_repo_lock_pins_the_tag_setup_installs():
    from halgateway.config import LLAMA_SERVER

    tag, _ = llamacpp.load_lock()
    assert tag == "b12121" and f"llama-{tag}/" in LLAMA_SERVER  # the router's default is where it lands


def test_cli_exit_code_follows_the_check(tmp_path):
    _, archive = lock(tmp_path, digest="0" * 64)
    assert llamacpp.main(["x", "verify", "b12121", str(archive)]) == 1


# --- install ---------------------------------------------------------------------------------


def _archive(tmp_path, name, files, top=None):
    """A .tar.gz holding `files` (name -> bytes), under `top/` when given."""
    import io as _io
    import tarfile as _tarfile

    path = tmp_path / "src" / name
    path.parent.mkdir(exist_ok=True)
    with _tarfile.open(path, "w:gz") as tar:
        for fname, data in files.items():
            info = _tarfile.TarInfo(f"{top}/{fname}" if top else fname)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, _io.BytesIO(data))
    return path


def _lock(tmp_path, archives, variant="cuda"):
    sums = {p.name: llamacpp.hashlib.sha256(p.read_bytes()).hexdigest() for p in archives}
    lock = tmp_path / "lock.toml"
    names = ", ".join(f'"{p.name}"' for p in archives)
    assets = "\n".join(f'"{n}" = "{d}"' for n, d in sums.items())
    lock.write_text(f'tag = "b1"\n[assets]\n{assets}\n[variants]\n{variant} = [{names}]\n')
    return lock


def _opener(archives, seen):
    by_name = {p.name: p for p in archives}

    def opener(url, timeout):
        seen.append((url, timeout))
        return by_name[url.rsplit("/", 1)[-1]].open("rb")

    return opener


def test_install_downloads_checks_and_merges_the_variant_archives(tmp_path):
    main = _archive(
        tmp_path, "llama-b1-cuda.tar.gz", {"llama-server": b"server", "libggml.so": b"g"}, top="llama-b1"
    )
    # regression: the CUDA runtime landed in its archive's own folder, so llama.cpp ran on the CPU
    rt = _archive(tmp_path, "cudart-b1.tar.gz", {"libcudart.so.12": b"rt"}, top="cudart-llama-b1-bin-cuda")
    seen = []
    target = llamacpp.install(
        tmp_path / "root", "cuda", opener=_opener([main, rt], seen), lock=_lock(tmp_path, [main, rt])
    )
    assert target == tmp_path / "root" / "llama-b1"
    assert sorted(p.name for p in target.iterdir()) == ["libcudart.so.12", "libggml.so", "llama-server"]
    assert seen[0][0] == "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-b1-cuda.tar.gz"
    assert (
        llamacpp.install(tmp_path / "root", "cuda", opener=None, lock=_lock(tmp_path, [main, rt])) == target
    )


def test_an_archive_that_does_not_match_the_lock_installs_nothing(tmp_path):
    good = _archive(tmp_path, "llama-b1-cuda.tar.gz", {"llama-server": b"server"}, top="llama-b1")
    lock = _lock(tmp_path, [good])
    good.write_bytes(good.read_bytes() + b"tampered")
    with pytest.raises(llamacpp.LockError, match="sha256 mismatch"):
        llamacpp.install(tmp_path / "root", "cuda", opener=_opener([good], []), lock=lock)
    assert not (tmp_path / "root" / "llama-b1").exists()


def test_the_variant_follows_the_gpu_vendor():
    assert llamacpp.variant_for(["nvidia"]) == "cuda"
    assert llamacpp.variant_for(["amd"]) == "rocm"
    assert llamacpp.variant_for([]) == "cpu"


def test_every_pinned_variant_lists_locked_archives():
    tag, assets = llamacpp.load_lock()
    for variant in llamacpp.VARIANTS:
        _, names = llamacpp.files_of(variant)
        assert names and all(n in assets and tag in n for n in names)
