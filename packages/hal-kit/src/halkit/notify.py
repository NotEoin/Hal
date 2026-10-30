"""Notification router: one path from any daemon to the desktop, the phone and the speaker.

Moved from `halnotify/tools.py` so every daemon shares it. It reads the `[notify]` table, sends
to the channels asked for, keeps a history and honours quiet hours and do-not-disturb. It is
host-side pack code: a model reaches it only through the `notify_send` tool, under policy.

Design decisions:

- Desktop goes through `notify-send` via `halkit.proc.run`; push goes through
  `halkit.netguard.fetch` to `[notify].ntfy_server`, https only and limited to that host, so a
  mistyped or hostile server value cannot reach a private address; speech runs
  `[notify].say_command` with the text as one argument. Every call has a timeout.
- A channel that fails is left out of `reached` and logged; `send` never raises for a delivery
  failure, because an alert path that crashes its caller is worse than a missed alert.
- Quiet time (quiet hours or do-not-disturb) silences the phone and the speaker unless the
  alert is `urgent`; the desktop is always sent.
- Telegram is not a channel here: the hal-telegram pack owns its token and chat.
- History keeps the title and body in `state_dir()/notify/history.db`, timestamps in UTC.
"""

from __future__ import annotations

import logging
import shlex
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from halkit import config, netguard, paths, proc
from halkit.timeutil import iso_z, parse_iso_z, utc_now

log = logging.getLogger(__name__)

CHANNELS = ("desktop", "phone", "voice")
DESKTOP_TIMEOUT_S = 5.0  # notify-send returns at once; this only bounds a hung notification daemon
SAY_TIMEOUT_S = 30.0  # a spoken alert is a few sentences
NTFY_TIMEOUT_S = 10.0
NTFY_MAX_BYTES = 64 * 1024  # ntfy answers with a short JSON receipt
DB_TIMEOUT_S = 5.0
MAX_HISTORY = 500
DEFAULT_DND_MINUTES = 60

TABLE = config.register(
    config.Table(
        "notify",
        (
            config.Key("channels", list, ["desktop", "phone"], doc="Default delivery channels"),
            config.Key("quiet_from", int, 22, doc="Hour quiet hours start (local time)"),
            config.Key("quiet_until", int, 8, doc="Hour quiet hours end (local time)"),
            config.Key("ntfy_topic", str, "", doc="ntfy topic for the phone channel; empty disables it"),
            config.Key("ntfy_server", str, "https://ntfy.sh", doc="ntfy server URL (https only)"),
            config.Key(
                "say_command", str, "", doc="Command that speaks its last argument; empty disables it"
            ),
        ),
        owner="hal-kit",
    )
)


def settings() -> Mapping[str, object]:
    """The validated `[notify]` settings; raises `halkit.config.ConfigError` on a bad file."""
    return config.load(TABLE)


def _dir() -> Path:
    path = paths.state_dir() / "notify"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(_dir() / "history.db", timeout=DB_TIMEOUT_S)
    con.execute(
        "CREATE TABLE IF NOT EXISTS sent (at TEXT, title TEXT, body TEXT, urgent INTEGER, channels TEXT)"
    )
    return con


def _dnd_until() -> datetime | None:
    file = _dir() / "dnd"
    if not file.exists():
        return None
    try:
        until = parse_iso_z(file.read_text().strip())
    except ValueError:
        log.warning("ignoring a malformed do-not-disturb file %s", file)
        file.unlink(missing_ok=True)
        return None
    if until <= utc_now():
        file.unlink(missing_ok=True)
        return None
    return until


def quiet(now: datetime | None = None) -> bool:
    """True during quiet hours (local time) or while do-not-disturb is on."""
    cfg = settings()
    hour = (now or utc_now().astimezone()).hour
    start, end = int(cfg["quiet_from"]), int(cfg["quiet_until"])  # type: ignore[call-overload]  # int keys
    in_hours = (hour >= start or hour < end) if start > end else start <= hour < end
    return in_hours or _dnd_until() is not None


def _desktop(title: str, body: str, urgent: bool) -> bool:
    argv = ["notify-send", "-a", "Hal", *(["-u", "critical"] if urgent else []), title, body]
    try:
        return proc.run(argv, timeout=DESKTOP_TIMEOUT_S, check=False).returncode == 0
    except (OSError, proc.ProcTimeout) as exc:
        log.warning("desktop notification failed: %s", exc)
        return False


def _ntfy(title: str, body: str, urgent: bool, silent: bool, url: str = "") -> bool:
    cfg = settings()
    topic = str(cfg["ntfy_topic"])
    if not topic:
        return False
    server = str(cfg["ntfy_server"]).rstrip("/")
    host = netguard.canonical_url(server).split("/")[2]
    headers = {
        "Title": title.encode("utf-8").decode("latin-1"),  # http.client sends header text as latin-1
        "Tags": "robot",
        "Priority": "high" if urgent else ("min" if silent else "default"),
    }
    if url:
        headers["Click"] = url
    try:
        response = netguard.fetch(
            f"{server}/{topic}",
            domains=(host,),
            timeout=NTFY_TIMEOUT_S,
            max_bytes=NTFY_MAX_BYTES,
            method="POST",
            headers=headers,
            body=body.encode(),
        )
    except (OSError, ValueError) as exc:  # BlockedURL is a ValueError, FetchError an OSError
        log.warning("push notification failed: %s", exc)
        return False
    return 200 <= response.status < 300


def _speak(title: str, body: str) -> bool:
    command = str(settings()["say_command"]).strip()
    if not command:
        return False
    try:
        argv = [*shlex.split(command), f"{title}. {body}"]
        return proc.run(argv, timeout=SAY_TIMEOUT_S, check=False).returncode == 0
    except (OSError, ValueError, proc.ProcTimeout) as exc:
        log.warning("spoken notification failed: %s", exc)
        return False


def send(
    title: str,
    body: str = "",
    *,
    urgent: bool = False,
    speak: bool = False,
    channels: Sequence[str] | None = None,
    url: str = "",
    source: str = "",
) -> dict[str, object]:
    """Sends an alert and returns `{"reached": [...], "quiet": bool}`.

    `urgent` breaks through quiet time. `speak` adds the voice channel (never during quiet time
    unless urgent). `channels` restricts delivery to some of desktop, phone and voice, and
    raises `ValueError` for any other name. `url` is opened when the phone notification is
    tapped. `source` names the sender and is kept in the log line, not sent.
    """
    wanted = set(channels if channels is not None else settings()["channels"])  # type: ignore[arg-type]
    unknown = wanted - set(CHANNELS)
    if unknown:
        raise ValueError(f"unknown notification channels {sorted(unknown)}; use {', '.join(CHANNELS)}")
    hush = quiet() and not urgent
    reached = []
    if "desktop" in wanted and _desktop(title, body, urgent):
        reached.append("desktop")
    if "phone" in wanted and _ntfy(title, body, urgent, hush, url):
        reached.append("phone")
    if (speak or "voice" in wanted) and not hush and _speak(title, body):
        reached.append("voice")
    with _db() as con:
        con.execute(
            "INSERT INTO sent VALUES (?, ?, ?, ?, ?)",
            (iso_z(), title, body, int(urgent), ",".join(reached)),
        )
        con.execute(
            "DELETE FROM sent WHERE rowid NOT IN (SELECT rowid FROM sent ORDER BY rowid DESC LIMIT ?)",
            (MAX_HISTORY,),
        )
    log.info("notification %r from %s reached %s", title, source or "unknown", reached or "nothing")
    return {"reached": reached, "quiet": hush}


def dnd(minutes: int = DEFAULT_DND_MINUTES) -> dict[str, object]:
    """Turns do-not-disturb on for `minutes`; 0 or less turns it off. Returns the new `state()`."""
    file = _dir() / "dnd"
    if int(minutes) <= 0:
        file.unlink(missing_ok=True)
    else:
        file.write_text(iso_z(utc_now() + timedelta(minutes=int(minutes))))
    return state()


def state() -> dict[str, object]:
    """Whether it is quiet now, when do-not-disturb ends (UTC) and the quiet hours."""
    cfg = settings()
    until = _dnd_until()
    return {
        "quiet": quiet(),
        "dnd_until": iso_z(until) if until else None,
        "quiet_hours": f"{cfg['quiet_from']}:00 to {cfg['quiet_until']}:00",
    }


def history(limit: int = 20) -> list[dict[str, object]]:
    """The last `limit` alerts, newest first."""
    rows = _db().execute("SELECT * FROM sent ORDER BY rowid DESC LIMIT ?", (int(limit),)).fetchall()
    return [
        {
            "at": r[0],
            "title": r[1],
            "body": r[2],
            "urgent": bool(r[3]),
            "channels": r[4].split(",") if r[4] else [],
        }
        for r in rows
    ]
