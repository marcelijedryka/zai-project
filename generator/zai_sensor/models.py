"""Plain data types shared by the generator modules."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class Point:
    """One value to send. ``timestamp`` is timezone-aware UTC, or ``None`` for "server now"."""

    value: float
    timestamp: datetime | None = None


class Kind(StrEnum):
    ACCEPTED = "accepted"  # 201
    REJECTED = "rejected"  # 422: the server refused the value, the next point may still work
    KEY_REJECTED = "key_rejected"  # 401: sensor unregistered or key invalid, nothing will work
    FAILED = "failed"  # network errors, 429/503 after the retries, other statuses
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    status: int | None = None
    measurement_id: int | None = None
    series_id: int | None = None
    timestamp: str | None = None  # as stored by the server
    message: str = ""  # problem detail or failure description


@dataclass
class Summary:
    sent: int = 0
    accepted: int = 0
    rejected: int = 0
    failed: int = 0
    key_rejected: bool = False

    def add(self, outcome: Outcome) -> None:
        self.sent += 1
        match outcome.kind:
            case Kind.ACCEPTED:
                self.accepted += 1
            case Kind.REJECTED:
                self.rejected += 1
            case Kind.FAILED:
                self.failed += 1
            case Kind.KEY_REJECTED:
                self.failed += 1
                self.key_rejected = True
            case Kind.DRY_RUN:
                pass

    def line(self) -> str:
        return (
            f"sent={self.sent} accepted={self.accepted} "
            f"rejected={self.rejected} failed={self.failed}"
        )

    @property
    def exit_code(self) -> int:
        """0 = every point accepted, 1 = some rejected/failed, 2 = the sensor key was refused."""
        if self.key_rejected:
            return 2
        return 1 if self.rejected or self.failed else 0
