"""Track in-flight recorders while retaining the latest batch for legacy inspection."""

from concurrent.futures import Future
from typing import Any


def retain_recording(recordings: list[Any], recording: Any, lock: Any) -> None:
    """Keep active work and the latest recorder, never an unbounded batch history."""
    with lock:
        recordings.append(recording)

    def completed(_future: Future[Any]) -> None:
        with lock:
            latest = recordings[-1] if recordings else None
            recordings[:] = [item for item in recordings if item is latest or not item.finished.done()]

    recording.finished.add_done_callback(completed)
