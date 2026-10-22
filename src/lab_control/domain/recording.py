"""Recording completion evidence independent of workers, sessions and filesystems."""

from dataclasses import dataclass

from lab_control.domain.instruments import DeviceError


@dataclass(frozen=True)
class RecordingOutcome:
    paths: tuple[str, ...]
    received: int
    committed: int
    converted: int
    binary_error: str = ""
    csv_error: str = ""
    pause_reason: str = ""

    @property
    def error(self) -> str:
        return self.binary_error or self.csv_error


class RecordingError(DeviceError):
    """A failed recording with evidence of the data that was actually committed."""

    def __init__(self, outcome: RecordingOutcome, code: str = "recording_failed") -> None:
        super().__init__(
            outcome.error or outcome.pause_reason or "Recording buffer full; acquisition paused", code
        )
        self.outcome = outcome
