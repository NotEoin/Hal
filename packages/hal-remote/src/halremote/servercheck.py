"""Read-only desktop checks SC01 to SC22 for `hal remote server-check` (design 07).

The checks never write, except SC22 with `fix=True` (it deletes delivery files past their TTL).
`sudo` is called only with `sshd_t=True`, and then only as `sudo -n`. Every subprocess has a
timeout. SC14 emulates sshd's launch of the forced command (REQ-RMT-17) through a small Python
launcher, so stdin is a real socket. A check whose input is unreadable or unknowable reports
`unknown`, never `pass`: the sshd_config reader returns `None` for a keyword it cannot settle and
the checks pass that on.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import shlex
import stat
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import authkeys, sshdconf
from .keys import Runner
from .model import Role  # noqa: F401 - re-exported for callers that type results by role

log = logging.getLogger(__name__)

Severity = Literal["critical", "warn", "info"]
Status = Literal["pass", "fail", "skip", "unknown"]

SSHD_PATH = Path("/usr/sbin/sshd")
SYSTEMCTL_TIMEOUT_S = 10  # a status query; the design allows 30 s for control calls
SS_TIMEOUT_S = 10
UFW_TIMEOUT_S = 10
SSHD_T_TIMEOUT_S = 15
GATEWAY_PROBE_TIMEOUT_S = 5
GATEWAY_URL = "http://127.0.0.1:8741/v1/models"
# 00-overview ports table plus Ollama; all must listen on loopback only (REQ-RMT-41).
HAL_PORTS = (8740, 8741, 8742, 8080, 11434, 9880, 8737)
LOOPBACK_PREFIXES = ("127.", "[::1]", "::1")
GROUP_OTHER_WRITE = stat.S_IWGRP | stat.S_IWOTH
DEFAULT_AUTHORIZED_KEYS_FILE = ".ssh/authorized_keys .ssh/authorized_keys2"  # OpenSSH default

HttpStatus = Callable[[str, float], int | None]


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check."""

    id: str
    title: str
    severity: Severity
    status: Status
    detail: str
    fix: str


class _Source:
    """Looks up effective sshd values from the static reader or from `sshd -T` output."""

    def __init__(self, cfg: sshdconf.SshdConfig | None, t_values: dict[str, str] | None) -> None:
        self.cfg = cfg
        self.t_values = t_values

    def get(self, key: str, default: str) -> str | None:
        if self.t_values is not None:
            return self.t_values.get(key, default)
        assert self.cfg is not None
        return sshdconf.effective(self.cfg, key, default)

    def allow_users(self) -> tuple[str, ...] | None:
        if self.t_values is not None:
            return tuple(self.t_values.get("allowusers", "").split())
        assert self.cfg is not None
        if self.cfg.unreadable or "allowusers" in self.cfg.conditional:
            return None
        return self.cfg.allow_users


def _parse_sshd_t(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, _, rest = line.strip().partition(" ")
        if not key:
            continue
        key = key.lower()
        values[key] = (
            f"{values[key]} {rest}".strip()
            if key == "allowusers" and key in values
            else values.get(key, rest)
        )
    return values


def _result(cid: str, title: str, sev: Severity, ok: bool | None, detail: str, fix: str) -> CheckResult:
    status: Status = "unknown" if ok is None else ("pass" if ok else "fail")
    return CheckResult(cid, title, sev, status, detail, fix if status != "pass" else "")


def _keyword(
    src: _Source,
    cid: str,
    title: str,
    sev: Severity,
    key: str,
    default: str,
    accept: Collection[str],
    fix: str,
) -> CheckResult:
    value = src.get(key, default)
    if value is None:
        return _result(cid, title, sev, None, f"{key} cannot be settled from the static files", fix)
    return _result(cid, title, sev, value.lower() in accept, f"{key} is {value}", fix)


def _loopback(addr: str) -> bool:
    return addr.startswith(LOOPBACK_PREFIXES)


def exposed_ports(ss_output: str, ports: Collection[int] = HAL_PORTS) -> list[str]:
    """Returns `addr:port` of listeners on `ports` that are not on a loopback address."""
    bad: list[str] = []
    for line in ss_output.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        addr, _, port = local.rpartition(":")
        if port.isdigit() and int(port) in ports and not _loopback(addr):
            bad.append(local)
    return bad


def _http_status(url: str, timeout_s: float) -> int | None:
    """HTTP status of an unauthenticated GET, or `None` when the connection fails."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - fixed loopback URL
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def _run(
    runner: Runner, argv: list[str], timeout: float, env: dict[str, str] | None = None
) -> tuple[int, str] | None:
    """Runs `argv` without raising on a non-zero exit; `None` when the program is missing."""
    try:
        proc = runner(argv, timeout=timeout, check=False, **({"env": env} if env is not None else {}))
    except FileNotFoundError:
        return None
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _mode_problems(home: Path, uid: int) -> list[str]:
    ssh = home / ".ssh"
    akeys = ssh / "authorized_keys"
    problems: list[str] = []
    for path, bad_bits, name in (
        (home, GROUP_OTHER_WRITE, "home"),
        (ssh, 0o077, ".ssh"),
        (akeys, 0o077, "authorized_keys"),
    ):
        try:
            st = path.stat()
        except OSError as exc:
            problems.append(f"{name}: {exc.strerror or exc}")
            continue
        if st.st_mode & bad_bits:
            problems.append(f"{name} mode is {stat.S_IMODE(st.st_mode):04o}")
        if st.st_uid != uid:
            problems.append(f"{name} is owned by uid {st.st_uid}")
    return problems


def _options_problem(line: authkeys.HalKeyLine, registry_any: Collection[str]) -> str | None:
    opts = line.options
    name = f"{line.client}:{line.role}"
    if "restrict" not in opts:
        return f"{name} lacks restrict"
    if not any(o.startswith("from=") for o in opts) and line.client not in registry_any:
        return f"{name} lacks from="
    if line.role == "tunnel":
        if "port-forwarding" not in opts:
            return f"{name} lacks port-forwarding"
        opens = [o for o in opts if o.startswith("permitopen=")]
        if not opens or not all(o.startswith('permitopen="127.0.0.1:') for o in opens):
            return f"{name} permitopen is missing or not 127.0.0.1"
        if f'permitlisten="{authkeys.PERMITLISTEN}"' not in opts:
            return f"{name} lacks permitlisten"
        if 'command="/bin/false"' not in opts:
            return f"{name} command is not /bin/false"
    else:
        if any(o == "port-forwarding" or o.startswith("permit") for o in opts):
            return f"{name} allows forwarding"
        cmd = [o for o in opts if o.startswith("command=")]
        if len(cmd) != 1 or " serve --client " not in cmd[0]:
            return f"{name} has no serve command"
    return None


def _command_paths(line: authkeys.HalKeyLine) -> list[str]:
    for opt in line.options:
        if opt.startswith('command="') and line.role == "mcp":
            try:
                argv = shlex.split(opt[len('command="') : -1])
            except ValueError:
                return []
            paths = [argv[0]] if argv else []
            if "--hub" in argv and argv.index("--hub") + 1 < len(argv):
                paths.append(argv[argv.index("--hub") + 1])
            return paths
    return []


# Starts the shell the way sshd does: a socket as stdin makes bash read `.bashrc`, which is what
# the check must catch (REQ-RMT-17). A pipe or /dev/null would hide a noisy rc file.
_SOCKET_LAUNCHER = (
    "import os, socket, sys\n"
    "a, b = socket.socketpair()\n"
    "os.dup2(a.fileno(), 0)\n"
    "os.execv(sys.argv[1], [sys.argv[1], '-c', sys.argv[2]])\n"
)
SC14_TIMEOUT_S = 20
SC14_DETAIL_CHARS = 120
FAKE_PEER = "127.0.0.1 50000"


def _command_value(line: authkeys.HalKeyLine) -> str | None:
    for opt in line.options:
        if opt.startswith('command="') and line.role == "mcp":
            return opt[len('command="') : -1]
    return None


def _forced_command_problem(
    runner: Runner, shell: str, command: str, client: str, home: Path, user: str
) -> str | None:
    """Runs `command` with verb `check` like sshd; returns a problem text, `None` when it passed."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "USER": user,
        "LOGNAME": user,
        "SHELL": shell,
        "SSH_ORIGINAL_COMMAND": "check",
        "SSH_CLIENT": f"{FAKE_PEER} 22",
        "SSH_CONNECTION": f"{FAKE_PEER} 127.0.0.1 22",
    }
    argv = [sys.executable, "-c", _SOCKET_LAUNCHER, shell, command]
    res = _run(runner, argv, SC14_TIMEOUT_S, env)
    if res is None:
        return f"{client}: cannot start {shell}"
    first = res[1].split("\n", 1)[0]
    try:
        msg = json.loads(first)
    except ValueError:
        return f"{client}: first output line is not JSON: {first[:SC14_DETAIL_CHARS]!r}"
    if not isinstance(msg, dict) or msg.get("ok") is not True:
        return f"{client}: check reported {str(msg)[:SC14_DETAIL_CHARS]}"
    return None


def run_checks(
    *,
    home: Path,
    etc_ssh: Path = sshdconf.DEFAULT_ROOT,
    runner: Runner,
    sshd_t: bool = False,
    fix: bool = False,
    user: str | None = None,
    uid: int | None = None,
    sshd_path: Path = SSHD_PATH,
    registry_clients: Collection[str] | None = None,
    registry_from_any: Collection[str] = (),
    delivery_dir: Path | None = None,
    delivery_ttl_s: int = 86400,
    now: float | None = None,
    http_status: HttpStatus = _http_status,
    login_shell: str | None = None,
) -> list[CheckResult]:
    """Runs SC01 to SC22 and returns one result each, in id order.

    `registry_clients` is the set of registered client names (`None`: no registry yet, SC19 is
    skipped). `now` is a Unix time, injectable for tests.
    """
    user = user or os.environ.get("USER", "")
    uid = os.getuid() if uid is None else uid
    now = time.time() if now is None else now
    out: list[CheckResult] = []

    def add(*args: object) -> None:
        out.append(_result(*args))  # type: ignore[arg-type]  # call sites pass the typed tuple

    ok_sshd = sshd_path.is_file() and os.access(sshd_path, os.X_OK)
    add("SC01", "sshd installed", "critical", ok_sshd, str(sshd_path), "install openssh-server")

    res = _run(runner, ["systemctl", "is-active", "ssh.socket", "ssh.service"], SYSTEMCTL_TIMEOUT_S)
    active = None if res is None else "active" in res[1].split()
    add(
        "SC02",
        "ssh service active",
        "critical",
        active,
        "ssh.socket or ssh.service",
        "sudo systemctl enable --now ssh",
    )

    t_values = None
    if sshd_t:
        t = _run(
            runner,
            ["sudo", "-n", str(sshd_path), "-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"],
            SSHD_T_TIMEOUT_S,
        )
        if t is not None and t[0] == 0:
            t_values = _parse_sshd_t(t[1])
    src = _Source(None if t_values is not None else sshdconf.read(etc_ssh), t_values)
    hardening = "run scripts/host-setup.sh --ssh"
    out.append(
        _keyword(
            src,
            "SC03",
            "PasswordAuthentication no",
            "critical",
            "passwordauthentication",
            "yes",
            ("no",),
            hardening,
        )
    )
    out.append(
        _keyword(
            src,
            "SC04",
            "KbdInteractiveAuthentication no",
            "critical",
            "kbdinteractiveauthentication",
            "yes",
            ("no",),
            hardening,
        )
    )
    out.append(
        _keyword(
            src,
            "SC05",
            "PermitRootLogin limited",
            "warn",
            "permitrootlogin",
            "prohibit-password",
            ("no", "prohibit-password", "without-password"),
            hardening,
        )
    )
    users = src.allow_users()
    in_list = None if users is None else any(fnmatch.fnmatchcase(user, p.partition("@")[0]) for p in users)
    add(
        "SC06",
        "AllowUsers contains the user",
        "warn",
        in_list,
        f"AllowUsers: {' '.join(users or ())}",
        f"add 'AllowUsers {user}' to sshd_config",
    )
    fwd_fix = "run scripts/host-setup.sh --ssh --ssh-forwarding-limits"
    out.append(
        _keyword(
            src,
            "SC07",
            "AllowTcpForwarding local",
            "warn",
            "allowtcpforwarding",
            "yes",
            ("local", "no"),
            fwd_fix,
        )
    )
    out.append(
        _keyword(
            src,
            "SC08",
            "AllowStreamLocalForwarding no",
            "warn",
            "allowstreamlocalforwarding",
            "yes",
            ("no", "local"),
            fwd_fix,
        )
    )
    out.append(
        _keyword(
            src,
            "SC09",
            "PermitUserEnvironment no",
            "critical",
            "permituserenvironment",
            "no",
            ("no",),
            hardening,
        )
    )
    akf = src.get("authorizedkeysfile", DEFAULT_AUTHORIZED_KEYS_FILE)
    wanted = {".ssh/authorized_keys", "%h/.ssh/authorized_keys", str(home / ".ssh/authorized_keys")}
    add(
        "SC10",
        "AuthorizedKeysFile includes .ssh/authorized_keys",
        "critical",
        None if akf is None else bool(wanted & set(akf.split())),
        f"AuthorizedKeysFile: {akf}",
        "set AuthorizedKeysFile .ssh/authorized_keys",
    )

    problems = _mode_problems(home, uid)
    add(
        "SC11",
        "home and .ssh modes",
        "critical",
        not problems,
        "; ".join(problems) or "modes are strict",
        "chmod go-w ~; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys",
    )

    akeys_path = home / ".ssh" / "authorized_keys"
    try:
        text = akeys_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    try:
        owned, _ = authkeys.parse(text)
    except ValueError as exc:
        owned = []
        add(
            "SC12",
            "hal-remote lines are restricted",
            "critical",
            False,
            f"unparseable line: {exc}",
            "run hal remote authorize again",
        )
    else:
        bad = [p for p in (_options_problem(ln, registry_from_any) for ln in owned) if p]
        add(
            "SC12",
            "hal-remote lines are restricted",
            "critical",
            not bad,
            "; ".join(bad) or f"{len(owned)} lines checked",
            "run hal remote authorize again for the client",
        )

    missing = [
        p for ln in owned for p in _command_paths(ln) if not (os.path.isabs(p) and os.access(p, os.X_OK))
    ]
    add(
        "SC13",
        "forced-command paths exist",
        "critical",
        not missing,
        ", ".join(sorted(set(missing))) or "paths are executable",
        "reinstall hal-remote and hal-hub, then authorize again",
    )

    mcp_lines = [(ln, _command_value(ln)) for ln in owned if ln.role == "mcp"]
    if not mcp_lines:
        out.append(
            CheckResult(
                "SC14", "forced command starts cleanly", "critical", "skip", "no mcp key is authorized", ""
            )
        )
    else:
        login = login_shell or _login_shell(uid)
        found = [
            p
            for ln, cmd in mcp_lines
            if cmd and (p := _forced_command_problem(runner, login, cmd, ln.client, home, user))
        ]
        add(
            "SC14",
            "forced command starts cleanly",
            "critical",
            not found,
            "; ".join(found) or "check answered ok",
            "make ~/.bashrc print nothing before its interactive check",
        )

    sshrc = etc_ssh / "sshrc"
    add(
        "SC15",
        "/etc/ssh/sshrc absent",
        "warn",
        not sshrc.exists(),
        str(sshrc),
        "remove it or make it print nothing",
    )

    ss = _run(runner, ["ss", "-ltnH"], SS_TIMEOUT_S)
    exposed = None if ss is None else exposed_ports(ss[1])
    add(
        "SC16",
        "Hal ports bound to loopback",
        "critical",
        None if exposed is None else not exposed,
        ", ".join(exposed or []) or "all on loopback",
        "bind the listed ports to 127.0.0.1",
    )

    code = http_status(GATEWAY_URL, GATEWAY_PROBE_TIMEOUT_S)
    if code is None:
        add(
            "SC17",
            "gateway TCP rejects a request without a token",
            "critical",
            False,
            "connection refused",
            "run `hal remote authorize` again or turn on `[gateway].tcp`",
        )
    else:
        add(
            "SC17",
            "gateway TCP rejects a request without a token",
            "critical",
            code == 401,
            f"HTTP {code} without a token",
            "update hal-gateway; the listener must answer 401",
        )

    if t_values is not None:
        add("SC18", "no Match blocks affect the checks", "warn", True, "checked on the effective config", "")
    else:
        keys = sorted(src.cfg.conditional) if src.cfg else []
        add(
            "SC18",
            "no Match blocks affect the checks",
            "warn",
            not keys,
            ", ".join(keys) or "no Match blocks",
            "run server-check with --sshd-t",
        )

    if registry_clients is None:
        out.append(
            CheckResult("SC19", "registry and authorized_keys agree", "warn", "skip", "no registry", "")
        )
    else:
        tagged = {ln.client for ln in owned}
        reg = set(registry_clients)
        diff = [f"{c}: no lines" for c in sorted(reg - tagged)] + [
            f"{c}: no registry entry" for c in sorted(tagged - reg)
        ]
        add(
            "SC19",
            "registry and authorized_keys agree",
            "warn",
            not diff,
            "; ".join(diff) or "in agreement",
            "hal remote revoke the stray client, or authorize it again",
        )

    plain = sum(
        1
        for ln in text.splitlines()
        if ln.strip()
        and not ln.lstrip().startswith("#")
        and authkeys.TAG_PREFIX not in ln
        and "restrict" not in ln
        and "command=" not in ln
    )
    add(
        "SC20",
        "unrestricted keys",
        "info",
        plain == 0,
        f"{plain} keys without restrict or command=",
        "review the keys with ssh-keygen -lf",
    )

    ufw = _run(runner, ["ufw", "status"], UFW_TIMEOUT_S)
    if ufw is None:
        out.append(CheckResult("SC21", "firewall", "info", "skip", "ufw is not installed", ""))
    else:
        add(
            "SC21",
            "firewall",
            "info",
            "inactive" not in ufw[1].lower(),
            ufw[1].splitlines()[0] if ufw[1] else "no output",
            "sudo ufw limit from <lan> to any port 22",
        )

    stale = _stale_deliveries(delivery_dir, delivery_ttl_s, now)
    if fix:
        for path in stale:
            path.unlink(missing_ok=True)
    add(
        "SC22",
        "expired token deliveries",
        "info",
        not stale or fix,
        f"{len(stale)} past the TTL" + (" (deleted)" if fix and stale else ""),
        "run server-check --fix",
    )
    return sorted(out, key=lambda r: r.id)


def _login_shell(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_shell or "/bin/sh"
    except (ImportError, KeyError):
        return "/bin/sh"


def _stale_deliveries(directory: Path | None, ttl_s: int, now: float) -> list[Path]:
    if directory is None or not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.json") if now - p.stat().st_mtime > ttl_s)


def has_critical_failure(results: Collection[CheckResult], accepted: Collection[str] = ()) -> bool:
    """True when a critical check failed and its id is not in `accepted`."""
    return any(r.severity == "critical" and r.status == "fail" and r.id not in accepted for r in results)
