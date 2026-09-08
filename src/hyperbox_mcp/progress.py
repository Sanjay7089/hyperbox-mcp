"""Saying what is happening while it happens.

A command that prints nothing for four minutes is indistinguishable from a
command that has hung, and a person resolves that ambiguity by killing it.
Pulls and builds are the slow ones, and both engines already emit exactly
the events needed — layer ids, byte counts, build steps — which the SDKs
mostly hid and the REST driver hands over directly.

Degrades on purpose. On a TTY this renders live progress; piped to a file
or run in CI it prints one line per meaningful step, because a progress bar
written to a log is noise and the log is where a failure gets read.
"""

from __future__ import annotations

import sys
from typing import Any

#: A word, not a picture, when the terminal cannot be trusted with one.
_ICONS = {
    "search": ("🔍", "..."),
    "pull": ("📦", ">>>"),
    "build": ("🔨", ">>>"),
    "pack": ("🗜 ", ">>>"),
    "ok": ("✔ ", "OK "),
    "fail": ("✖ ", "!! "),
    "info": ("ℹ ", "-- "),
}


def _fancy() -> bool:
    """Whether stdout is a terminal that can take live output."""
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def say(message: str, icon: str = "info") -> None:
    glyph = _ICONS.get(icon, _ICONS["info"])[0 if _fancy() else 1]
    print(f"{glyph} {message}", flush=True)


class EngineProgress:
    """Renders an engine's own pull/build event stream.

    Docker and Podman emit newline-delimited JSON: `status` plus an
    optional `id` for pulls, `stream` for build steps, `error` when it goes
    wrong. This turns that into either a live display or a readable
    transcript, and deliberately does not interpret it beyond that — an
    engine's own words about its own work are more useful than a summary.
    """

    def __init__(self, what: str) -> None:
        self.what = what
        self._live: Any = None
        self._progress: Any = None
        self._tasks: dict[str, Any] = {}
        self._seen: set[str] = set()

    def __enter__(self) -> "EngineProgress":
        if not _fancy():
            return self
        try:
            from rich.progress import (
                BarColumn, Progress, SpinnerColumn, TextColumn,
            )

            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=28),
                TextColumn("{task.fields[note]}"),
                transient=True,
            )
            self._progress.start()
        except Exception:  # noqa: BLE001 - never let display break the work
            self._progress = None
        return self

    def __exit__(self, *exc) -> None:
        if self._progress is not None:
            try:
                self._progress.stop()
            except Exception:  # noqa: BLE001
                pass

    def update(self, event: dict) -> None:
        """Consume one engine event."""
        if not isinstance(event, dict):
            return
        if "error" in event:
            return  # the caller reports failures; this only shows progress

        # Build output: one line per step, always worth keeping.
        stream = (event.get("stream") or "").rstrip()
        if stream:
            if self._progress is None:
                if stream.startswith(("Step ", "Successfully", " --->")):
                    print(f"  {stream}", flush=True)
            else:
                self._progress.console.print(f"  {stream}")
            return

        status = event.get("status") or ""
        layer = event.get("id") or ""
        if not status:
            return

        if self._progress is None:
            # One line per distinct phase, not per byte: a log does not
            # need ten thousand "Downloading" lines.
            key = f"{status}:{layer}"
            phase = status if not layer else f"{status} {layer}"
            if status in ("Downloading", "Extracting") and layer:
                key = status  # collapse per-layer chatter
                phase = f"{status}..."
            if key not in self._seen:
                self._seen.add(key)
                print(f"  {phase}", flush=True)
            return

        detail = event.get("progressDetail") or {}
        current, total = detail.get("current"), detail.get("total")
        task_id = self._tasks.get(layer or status)
        note = ""
        if current and total:
            note = f"{current / 1e6:.0f}/{total / 1e6:.0f} MB"
        description = f"{status} {layer[:12]}" if layer else status
        if task_id is None:
            task_id = self._progress.add_task(
                description, total=total or None, note=note
            )
            self._tasks[layer or status] = task_id
        self._progress.update(
            task_id, description=description, completed=current or None,
            total=total or None, note=note,
        )
