"""Decision memory — what Jarvis called, what happened, what it learned.

Right now Jarvis fires a signal and forgets it, so it can repeat the same
mistake forever. This records every signal, resolves it against live price
once it hits TP or SL, and keeps a one-line lesson written after the fact.
Those lessons are then read back before the next call.

Two rules the design turns on:

* **Only resolved entries teach.** A lesson is written after the outcome is
  known and is only ever read back from a resolved entry, so nothing learns
  from a trade that has not finished yet. This is the same look-ahead
  discipline the backtest uses.
* **Recording must never break the scanner.** Every public function swallows
  storage errors and logs them. A full disk should cost the memory, not the
  signal.

Storage is JSON Lines at ``MEMORY_PATH``. See ``docs/environment.md`` for the
Railway persistence caveat — a container's filesystem does not survive a
redeploy unless a volume is mounted there.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("jarvis.memory")

MEMORY_PATH = os.environ.get("MEMORY_PATH", "/tmp/jarvis_memory.jsonl")

# A signal that has neither hit TP nor SL after this long is written off.
# Without it, pending entries accumulate forever and the hit rate flatters
# itself by only ever counting the trades that resolved.
PENDING_TTL_SECS = int(os.environ.get("MEMORY_PENDING_TTL", 24 * 3600))

# Writes come from the scanner thread and reads from the Telegram event loop.
_lock = threading.Lock()


@dataclass
class Entry:
    """One signal and, eventually, what became of it."""

    id: str
    ts: float
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    session: str
    outcome: str = "pending"          # pending | TP | SL | EXPIRED
    resolved_at: float | None = None
    exit_price: float | None = None
    lesson: str = ""
    tags: list = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.outcome != "pending"

    @property
    def r_multiple(self) -> float:
        """Realised return in units of risk. Ignores costs — this is a
        directional record, not a P&L statement."""
        risk = abs(self.entry - self.sl)
        if not risk or self.exit_price is None:
            return 0.0
        move = self.exit_price - self.entry
        if self.direction == "SELL":
            move = -move
        return move / risk


def _read_all() -> list:
    if not os.path.exists(MEMORY_PATH):
        return []
    entries = []
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(Entry(**json.loads(line)))
            except (ValueError, TypeError) as e:
                # One malformed line must not blind the whole log.
                log.warning(f"Skipping unreadable memory line: {e}")
    return entries


def _write_all(entries: list) -> None:
    tmp = f"{MEMORY_PATH}.tmp"
    os.makedirs(os.path.dirname(MEMORY_PATH) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(asdict(e)) + "\n")
    os.replace(tmp, MEMORY_PATH)  # atomic, so a crash cannot truncate the log


def load() -> list:
    with _lock:
        try:
            return _read_all()
        except OSError as e:
            log.error(f"Could not read memory: {e}")
            return []


def record_signal(sig, now: float = None) -> str:
    """Log a fired signal as pending. Returns its id, or "" if it could not
    be stored — the caller sends the signal either way."""
    now = time.time() if now is None else now
    entry = Entry(
        id=f"{sig.symbol}-{int(now)}",
        ts=now,
        symbol=sig.symbol,
        direction=sig.direction,
        entry=sig.entry,
        sl=sig.sl,
        tp=sig.tp,
        session=sig.session,
    )
    with _lock:
        try:
            os.makedirs(os.path.dirname(MEMORY_PATH) or ".", exist_ok=True)
            with open(MEMORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry)) + "\n")
            return entry.id
        except OSError as e:
            log.error(f"Could not record signal: {e}")
            return ""


def _hit(entry: Entry, price: float) -> str | None:
    """Which level this price reached first, if either.

    Only one price per scan is seen, so a bar that traded through both levels
    is judged on the one it is showing. TP is checked first, which flatters
    the record — that bias is stated rather than hidden.
    """
    if entry.direction == "BUY":
        if price >= entry.tp:
            return "TP"
        if price <= entry.sl:
            return "SL"
    else:
        if price <= entry.tp:
            return "TP"
        if price >= entry.sl:
            return "SL"
    return None


def resolve(symbol: str, price: float, now: float = None) -> list:
    """Close out any pending signal on this symbol that price has settled.

    Returns the entries resolved by this call, so the caller can write their
    lessons. Also expires anything older than PENDING_TTL_SECS.
    """
    now = time.time() if now is None else now
    resolved = []
    with _lock:
        try:
            entries = _read_all()
        except OSError as e:
            log.error(f"Could not read memory: {e}")
            return []

        changed = False
        for e in entries:
            if e.resolved:
                continue
            if e.symbol == symbol:
                outcome = _hit(e, price)
                if outcome:
                    e.outcome = outcome
                    e.exit_price = price
                    e.resolved_at = now
                    resolved.append(e)
                    changed = True
                    continue
            if now - e.ts > PENDING_TTL_SECS:
                e.outcome = "EXPIRED"
                e.resolved_at = now
                changed = True

        if changed:
            try:
                _write_all(entries)
            except OSError as err:
                log.error(f"Could not save resolutions: {err}")
                return []
    return resolved


def save_lesson(entry_id: str, lesson: str) -> bool:
    with _lock:
        try:
            entries = _read_all()
        except OSError:
            return False
        for e in entries:
            if e.id == entry_id:
                e.lesson = lesson.strip()
                try:
                    _write_all(entries)
                    return True
                except OSError as err:
                    log.error(f"Could not save lesson: {err}")
                    return False
    return False


def recent_lessons(symbol: str = None, limit: int = 5) -> list:
    """Lessons from finished trades, newest first.

    Pending entries are never included — a trade still running has taught
    nothing yet.
    """
    entries = [e for e in load() if e.resolved and e.lesson]
    if symbol:
        entries = [e for e in entries if e.symbol == symbol]
    entries.sort(key=lambda e: e.resolved_at or e.ts, reverse=True)
    return entries[:limit]


def lessons_prompt(symbol: str = None, limit: int = 5) -> str:
    """Past lessons formatted for injection into an analysis prompt."""
    lessons = recent_lessons(symbol, limit)
    if not lessons:
        return ""
    lines = "\n".join(
        f"- [{e.symbol} {e.direction} → {e.outcome}] {e.lesson}" for e in lessons
    )
    return (
        "\n\nWhat you learned from your own recent calls "
        "(these are finished trades, so treat them as evidence):\n" + lines
    )


def stats(symbol: str = None) -> dict:
    entries = [e for e in load() if e.resolved and e.outcome != "EXPIRED"]
    if symbol:
        entries = [e for e in entries if e.symbol == symbol]
    wins = [e for e in entries if e.outcome == "TP"]
    losses = [e for e in entries if e.outcome == "SL"]
    total_r = sum(e.r_multiple for e in entries)
    return {
        "resolved": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(entries) * 100) if entries else 0.0,
        "total_r": total_r,
        "pending": len([e for e in load() if not e.resolved]),
    }
