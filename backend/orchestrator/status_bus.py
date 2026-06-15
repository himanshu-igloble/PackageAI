"""In-process pub/sub for live agent status, keyed by case_id.

Used by the orchestrator and tools to surface what is happening, without
exposing private chain-of-thought. Subscribers receive structured events:

    {
      "ts":             "2026-05-12T10:30:01Z",
      "stage":          "executing",
      "active_agent":   "material",
      "action":         "lookup",
      "tool":           "material_db",
      "source":         "db",
      "confidence":     "verified",
      "awaiting":       null,
      "summary":        "Looked up 'HDPE' in the material DB"
    }

This is *not* a database. It's a hot stream the UI subscribes to via SSE while
the case is being worked on. The audit log (in models.AuditEvent) is the
durable record.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


class StatusBus:
    """One queue per (case_id, subscriber). Survives the lifetime of the
    process. emit() is non-blocking and safe to call from any thread; SSE
    consumers see events in order."""

    def __init__(self) -> None:
        # case_id -> list of asyncio.Queue (one per subscriber)
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        # Recent history per case so a late-joining subscriber gets context.
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._history_cap = 50
        self._lock = asyncio.Lock()

    def emit(self, case_id: str, event: dict[str, Any]) -> None:
        """Fire-and-forget. Safe to call from sync code."""
        evt = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **event,
        }
        # Buffer to history (bounded)
        hist = self._history[case_id]
        hist.append(evt)
        if len(hist) > self._history_cap:
            del hist[: len(hist) - self._history_cap]

        # Push to live subscribers
        for q in list(self._subs.get(case_id, [])):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # Drop oldest if a slow client backs up.
                try:
                    _ = q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    pass

    def history(self, case_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(case_id, []))

    async def subscribe(self, case_id: str) -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        # Replay recent history so a freshly opened UI shows context.
        for evt in self._history.get(case_id, [])[-20:]:
            await q.put(evt)
        async with self._lock:
            self._subs[case_id].append(q)
        return q

    async def unsubscribe(self, case_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subs.get(case_id, []):
                self._subs[case_id].remove(q)


# Single process-wide bus.
status_bus = StatusBus()


def emit_status(case_id: str, *, stage: str, active_agent: str, action: str,
                tool: str | None = None, source: str | None = None,
                confidence: str | None = None, awaiting: str | None = None,
                summary: str = "", **extra: Any) -> None:
    """Convenience wrapper so call sites read cleanly."""
    status_bus.emit(case_id, {
        "stage": stage,
        "active_agent": active_agent,
        "action": action,
        "tool": tool,
        "source": source,
        "confidence": confidence,
        "awaiting": awaiting,
        "summary": summary,
        **extra,
    })


def sse_format(evt: dict[str, Any]) -> str:
    """Encode an event as a single SSE frame."""
    return f"data: {json.dumps(evt, default=str)}\n\n"
