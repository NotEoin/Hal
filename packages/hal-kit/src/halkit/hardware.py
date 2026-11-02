"""Hardware detection for model tuning: GPUs, RAM, CPU threads and WSL2.

`detect` reports what this machine offers so `halkit.tuning` can size context and offload. Design:

- Detection never raises. A tool that is missing, times out or prints something unexpected adds
  a line to `Hardware.notes` and the machine is reported without that part.
- Every subprocess goes through an injectable `Runner` (default `halkit.proc.run` with a
  timeout), so tests feed canned output and never touch a real GPU.
- NVIDIA is read with `nvidia-smi`. AMD is read with `amd-smi`, then `rocm-smi`. Their JSON
  layouts differ between ROCm releases, so the AMD parsers accept the shapes seen in the
  ROCm 6 documentation and report anything else as unreadable [unverified on real AMD hardware].
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import proc

log = logging.getLogger(__name__)

SMI_TIMEOUT_S = 10.0  # nvidia-smi can stall for seconds while a GPU wakes from a low power state
MEMINFO = Path("/proc/meminfo")
OSRELEASE = Path("/proc/sys/kernel/osrelease")
BYTES_PER_MIB = 1024 * 1024
KIB_PER_MIB = 1024
NVIDIA_QUERY = "index,name,memory.total,memory.free,compute_cap"
NVIDIA_QUERY_OLD = "index,name,memory.total,memory.free"

# Runs argv with a timeout and returns stdout text; raises OSError, ProcTimeout or CalledProcessError.
Runner = Callable[[list[str], float], str]


@dataclass(frozen=True)
class Gpu:
    """One GPU. `free_mib` equals `total_mib` when the tool does not report free memory."""

    index: int
    vendor: str  # "nvidia" or "amd"
    name: str
    total_mib: int
    free_mib: int
    compute_cap: str = ""


@dataclass(frozen=True)
class Hardware:
    """What `detect` found; `notes` lists what could not be read."""

    gpus: tuple[Gpu, ...] = ()
    ram_total_mib: int = 0
    ram_free_mib: int = 0
    cpu_threads: int = 0
    wsl2: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready view, for `hal tune --json`."""
        return {
            "gpus": [g.__dict__ for g in self.gpus],
            "ram_total_mib": self.ram_total_mib,
            "ram_free_mib": self.ram_free_mib,
            "cpu_threads": self.cpu_threads,
            "wsl2": self.wsl2,
            "notes": list(self.notes),
        }


def from_dict(doc: Mapping[str, object]) -> Hardware:
    """Rebuilds a `Hardware` from `Hardware.to_dict` output (for `hal tune --hardware FILE`).

    Raises `ValueError` for a document that is not shaped like one.
    """
    try:
        gpus = tuple(Gpu(**g) for g in doc.get("gpus", []))  # type: ignore[attr-defined,union-attr]
        return Hardware(
            gpus,
            int(doc.get("ram_total_mib", 0)),  # type: ignore[call-overload]
            int(doc.get("ram_free_mib", 0)),  # type: ignore[call-overload]
            int(doc.get("cpu_threads", 0)),  # type: ignore[call-overload]
            bool(doc.get("wsl2", False)),
            tuple(str(n) for n in doc.get("notes", [])),  # type: ignore[attr-defined,union-attr]
        )
    except (TypeError, AttributeError) as exc:
        raise ValueError(f"not a hardware description: {exc}") from exc


def default_runner(argv: list[str], timeout: float) -> str:
    """Runs `argv` through `halkit.proc.run` and returns its stdout as text."""
    done = proc.run(argv, timeout=timeout)
    return done.stdout.decode("utf-8", "replace")


def _to_mib(value: object, unit: str = "MiB") -> int | None:
    """Converts a number and unit ("B", "KB", "MB", "GB" and the IEC forms) to whole MiB."""
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    scale = {"b": 1 / BYTES_PER_MIB, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1, "mib": 1}
    scale |= {"gb": 1024, "gib": 1024}
    factor = scale.get(unit.strip().lower())
    return None if factor is None else int(number * factor)


def parse_nvidia(text: str) -> list[Gpu]:
    """Parses `nvidia-smi --format=csv,noheader,nounits` rows; raises `ValueError` on a bad row."""
    gpus = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 4:
            raise ValueError(f"unexpected nvidia-smi row {line!r}")
        total, free = int(cells[2]), int(cells[3])
        cap = cells[4] if len(cells) > 4 and re.fullmatch(r"\d+\.\d+", cells[4]) else ""
        gpus.append(Gpu(int(cells[0]), "nvidia", cells[1], total, min(free, total), cap))
    return gpus


def _nvidia(run: Runner, notes: list[str]) -> list[Gpu]:
    for query in (NVIDIA_QUERY, NVIDIA_QUERY_OLD):  # drivers before 510 lack compute_cap
        try:
            out = run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"], SMI_TIMEOUT_S)
            return parse_nvidia(out)
        except FileNotFoundError:
            return []
        except (OSError, proc.ProcTimeout, ValueError, subprocess.CalledProcessError) as exc:
            last = f"nvidia-smi unreadable: {exc}"
    notes.append(last)
    return []


def _walk_value(node: object, *keys: str) -> object:
    """The value under the first of `keys` found at any depth in `node`, or None."""
    if isinstance(node, dict):
        for key in keys:
            if key in node:
                return node[key]
        for child in node.values():
            found = _walk_value(child, *keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _walk_value(child, *keys)
            if found is not None:
                return found
    return None


def _amount(node: object) -> int | None:
    """MiB from `{"value": 24560, "unit": "MB"}` or a bare number of MiB."""
    if isinstance(node, dict) and "value" in node:
        return _to_mib(node["value"], str(node.get("unit", "MB")))
    return _to_mib(node) if node is not None else None


def parse_amd_smi(static_json: str, metric_json: str | None) -> list[Gpu]:
    """Parses `amd-smi static --vram --asic --json` and, when given, `amd-smi metric --mem-usage --json`."""
    static = json.loads(static_json)
    rows = static.get("gpu_data", static) if isinstance(static, dict) else static
    metrics = {}
    if metric_json:
        data = json.loads(metric_json)
        for row in data.get("gpu_data", data) if isinstance(data, dict) else data:
            if isinstance(row, dict) and "gpu" in row:
                metrics[int(row["gpu"])] = row
    gpus = []
    for row in rows:
        index = int(row["gpu"])
        total = _amount(_walk_value(row.get("vram", {}), "size"))
        if total is None:
            raise ValueError(f"amd-smi gpu {index} has no vram size")
        name = str(_walk_value(row.get("asic", {}), "market_name") or f"AMD GPU {index}")
        free = _amount(_walk_value(metrics.get(index, {}), "free_vram"))
        gpus.append(Gpu(index, "amd", name, total, min(free, total) if free is not None else total))
    return gpus


def parse_rocm_smi(text: str) -> list[Gpu]:
    """Parses `rocm-smi --showproductname --showmeminfo vram --json` (`card0`, `card1`, ...)."""
    gpus = []
    for key, row in sorted(json.loads(text).items()):
        found = re.fullmatch(r"card(\d+)", key)
        if not found or not isinstance(row, dict):
            continue
        total = _to_mib(row.get("VRAM Total Memory (B)"), "b")
        used = _to_mib(row.get("VRAM Total Used Memory (B)", 0), "b")
        if total is None:
            raise ValueError(f"rocm-smi {key} has no VRAM Total Memory (B)")
        name = str(row.get("Card Series") or row.get("Card series") or f"AMD GPU {found.group(1)}")
        gpus.append(Gpu(int(found.group(1)), "amd", name, total, max(total - (used or 0), 0)))
    return gpus


def _amd(run: Runner, notes: list[str]) -> list[Gpu]:
    errors = (OSError, proc.ProcTimeout, ValueError, subprocess.CalledProcessError)
    try:
        static = run(["amd-smi", "static", "--asic", "--vram", "--json"], SMI_TIMEOUT_S)
        try:
            metric = run(["amd-smi", "metric", "--mem-usage", "--json"], SMI_TIMEOUT_S)
        except errors:
            metric = None
        return parse_amd_smi(static, metric)
    except FileNotFoundError:
        pass
    except (*errors, KeyError, TypeError, AttributeError) as exc:
        notes.append(f"amd-smi unreadable: {exc!r}")
    try:
        out = run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"], SMI_TIMEOUT_S)
        return parse_rocm_smi(out)
    except FileNotFoundError:
        pass
    except (*errors, KeyError, TypeError, AttributeError) as exc:
        notes.append(f"rocm-smi unreadable: {exc!r}")
    return []


def read_meminfo(path: Path = MEMINFO) -> tuple[int, int]:
    """`(total, available)` RAM in MiB from a `/proc/meminfo` file; raises `OSError` or `ValueError`."""
    values = {}
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        name, _, rest = line.partition(":")
        if name in ("MemTotal", "MemAvailable"):
            values[name] = int(rest.split()[0]) // KIB_PER_MIB
    if "MemTotal" not in values:
        raise ValueError(f"{path} has no MemTotal")
    return values["MemTotal"], values.get("MemAvailable", 0)


def is_wsl2(osrelease: Path = OSRELEASE) -> bool:
    """True when the kernel release names Microsoft, as WSL2 kernels do."""
    try:
        return "microsoft" in osrelease.read_text(encoding="ascii", errors="replace").lower()
    except OSError:
        return False


def detect(
    run: Runner | None = None,
    *,
    meminfo: Path = MEMINFO,
    osrelease: Path = OSRELEASE,
    which: Callable[[str], str | None] = shutil.which,
) -> Hardware:
    """Detects GPUs, memory, CPU threads and WSL2. Never raises; see `Hardware.notes`.

    `run` defaults to `default_runner`, looked up at call time so a test can replace it.
    """
    run = run or default_runner
    notes: list[str] = []
    gpus: list[Gpu] = []
    try:
        gpus = _nvidia(run, notes)
        if not gpus:
            gpus = _amd(run, notes)
    except Exception as exc:  # noqa: BLE001 - detection must report, never raise
        log.warning("GPU detection failed: %r", exc)
        notes.append(f"GPU detection failed: {exc!r}")
    if not gpus and not notes:
        tools = [t for t in ("nvidia-smi", "amd-smi", "rocm-smi") if which(t)]
        notes.append("no GPU tool answered" if not tools else f"{', '.join(tools)} found but no GPU listed")
    try:
        total, free = read_meminfo(meminfo)
    except (OSError, ValueError, IndexError) as exc:
        total = free = 0
        notes.append(f"cannot read RAM size: {exc}")
    threads = os.cpu_count() or 0
    if not threads:
        notes.append("cannot read the CPU thread count")
    return Hardware(tuple(gpus), total, free, threads, is_wsl2(osrelease), tuple(notes))
