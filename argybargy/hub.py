"""Live delivery layer: presence, long-poll wakeups, per-agent rate limiting, and
atomic claims — on top of a durable MessageStore.

Messages live in the store (SQLite); the hub keeps only ephemeral, live state. SQLite
calls are offloaded with ``asyncio.to_thread`` so a slow/contended query never blocks the
event loop; the in-memory presence/waiter/rate-limit state is touched only on the loop
thread (and ``asyncio.Event.set`` is called there, after the await). Single-process by
design — for multi-process scale, front it with a shared backend (see ROADMAP.md).
"""
from __future__ import annotations

import asyncio
import time

from .settings import settings

ONLINE_WINDOW_SECONDS = settings.online_window


class Hub:
    def __init__(self, store) -> None:
        self.store = store
        self._last_seen: dict = {}    # room -> {peer: monotonic ts}
        self._status: dict = {}       # room -> {peer: {"state": str | None, "note": str | None}}
        self._waiters: dict = {}      # room -> list[asyncio.Event]
        self._post_times: dict = {}   # rate-limit key -> list[monotonic ts]

    # ----- in-memory, loop-thread only -----

    def touch(self, room: str, peer: str) -> bool:
        """Mark a peer present; returns True the first time we ever see this peer."""
        seen = self._last_seen.setdefault(room, {})
        is_new = peer not in seen
        seen[peer] = time.monotonic()
        return is_new

    def set_status(self, room: str, peer: str, *, state: str | None, note: str | None,
                    state_provided: bool, note_provided: bool) -> None:
        """Write a status update. `state_provided`/`note_provided` distinguish 'key
        absent from the request' (leave alone) from 'key sent as null' (clear) —
        POST /presence's absent-vs-null contract. A bare heartbeat (neither
        provided) never allocates an entry."""
        if not (state_provided or note_provided):
            return
        entry = self._status.setdefault(room, {}).setdefault(peer, {"state": None, "note": None})
        if state_provided:
            entry["state"] = state
        if note_provided:
            entry["note"] = note

    def clear_status(self, room: str, peer: str) -> None:
        """Drop a peer's status entry — call when their code is revoked, so a name
        reissued in the same room doesn't inherit the previous incarnation's status
        note. `_last_seen` needs no equivalent: it self-corrects on the next touch,
        where a stale note would otherwise just sit there unwritten."""
        self._status.get(room, {}).pop(peer, None)

    def status_for(self, room: str, peer: str) -> dict:
        """A single peer's status, with staleness derived the same way peers() does."""
        now = time.monotonic()
        seen = self._last_seen.get(room, {}).get(peer)
        online = seen is not None and (now - seen) <= ONLINE_WINDOW_SECONDS
        entry = self._status.get(room, {}).get(peer, {})
        return {"status": entry.get("state"), "status_note": entry.get("note"), "status_stale": not online}

    def peers(self, room: str) -> list:
        now = time.monotonic()
        out = []
        for name, seen in sorted(self._last_seen.get(room, {}).items()):
            ago = now - seen
            online = ago <= ONLINE_WINDOW_SECONDS
            status = self._status.get(room, {}).get(name, {})
            out.append({
                "name": name,
                "online": online,
                "seconds_since_seen": round(ago, 1),
                "status": status.get("state"),
                "status_note": status.get("note"),
                "status_stale": not online,
            })
        return out

    def all_peers(self) -> dict:
        return {room: self.peers(room) for room in sorted(self._last_seen)}

    def allow(self, key: str, max_n: int, window: float) -> bool:
        """Sliding-window rate limit; only counts allowed posts so a block doesn't extend the window."""
        now = time.monotonic()
        times = self._post_times.setdefault(key, [])
        cutoff = now - window
        while times and times[0] < cutoff:
            times.pop(0)
        if len(times) >= max_n:
            return False
        times.append(now)
        return True

    def _wake(self, room: str) -> None:
        for ev in list(self._waiters.get(room, [])):
            ev.set()

    # ----- durable store access (offloaded to a threadpool) -----

    async def post(self, room, frm, to, text, expects_reply="none") -> dict:
        msg = await asyncio.to_thread(self.store.add, room, frm, to, text, expects_reply)
        self._wake(room)
        return msg

    async def claim(self, room, seq, peer) -> dict:
        return await asyncio.to_thread(self.store.claim, room, seq, peer)

    async def history(self, room, limit) -> list:
        return await asyncio.to_thread(self.store.history, room, limit)

    async def read(self, room, peer, since, wait):
        deadline = time.monotonic() + max(0.0, wait)
        waiters = self._waiters.setdefault(room, [])
        while True:
            msgs = await asyncio.to_thread(self.store.since, room, peer, since)
            if msgs or wait <= 0:
                return msgs, await asyncio.to_thread(self.store.room_seq, room)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return [], await asyncio.to_thread(self.store.room_seq, room)
            ev = asyncio.Event()
            waiters.append(ev)
            try:
                await asyncio.wait_for(ev.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return [], await asyncio.to_thread(self.store.room_seq, room)
            finally:
                try:
                    waiters.remove(ev)
                except ValueError:
                    pass
