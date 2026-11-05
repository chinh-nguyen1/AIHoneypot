"""Shared publication and directory-durability outcome types."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union


Destination = Union[str, Path]


class PublicationState(str, Enum):
    NOT_PUBLISHED = "NOT_PUBLISHED"
    PUBLISHED_UNCONFIRMED = "PUBLISHED_UNCONFIRMED"
    DURABLE = "DURABLE"


class DirectorySyncState(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    SYNCED = "SYNCED"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


def _validate_states(
    publication_state: PublicationState,
    directory_sync_state: DirectorySyncState,
) -> None:
    if not isinstance(publication_state, PublicationState):
        raise DurabilityValidationError(
            "publication_state must be a PublicationState"
        )
    if not isinstance(directory_sync_state, DirectorySyncState):
        raise DurabilityValidationError(
            "directory_sync_state must be a DirectorySyncState"
        )

    if publication_state is PublicationState.NOT_PUBLISHED:
        if directory_sync_state is not DirectorySyncState.NOT_ATTEMPTED:
            raise DurabilityValidationError(
                "directory sync cannot be attempted before publication"
            )
        return

    if publication_state is PublicationState.PUBLISHED_UNCONFIRMED:
        if directory_sync_state is DirectorySyncState.SYNCED:
            raise DurabilityValidationError(
                "a synced publication must use the DURABLE state"
            )
        return

    if directory_sync_state is not DirectorySyncState.SYNCED:
        raise DurabilityValidationError(
            "DURABLE requires directory sync state SYNCED"
        )


def _normalize_destination(destination: Destination) -> Path:
    if not isinstance(destination, (str, Path)):
        raise DurabilityValidationError("destination must be a non-empty path")
    if not str(destination):
        raise DurabilityValidationError("destination must be a non-empty path")
    return Path(destination)


@dataclass(frozen=True)
class DurabilityOutcome:
    """Immutable publication and directory-sync result for one operation."""

    operation: str
    publication_state: PublicationState
    directory_sync_state: DirectorySyncState
    destination: Path

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise DurabilityValidationError("operation must be a non-empty string")
        object.__setattr__(
            self, "destination", _normalize_destination(self.destination)
        )
        _validate_states(self.publication_state, self.directory_sync_state)

    @property
    def published(self) -> bool:
        """Whether the destination has been atomically published."""
        return self.publication_state is not PublicationState.NOT_PUBLISHED

    @property
    def is_durable_success(self) -> bool:
        """Whether the strict durability contract permits acknowledgment."""
        return (
            self.publication_state is PublicationState.DURABLE
            and self.directory_sync_state is DirectorySyncState.SYNCED
        )


class DurabilityError(RuntimeError):
    """Filesystem failure with explicit publication and sync outcome."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        publication_state: PublicationState,
        directory_sync_state: DirectorySyncState,
        destination: Destination,
        original_cause: Optional[BaseException] = None,
    ) -> None:
        outcome = DurabilityOutcome(
            operation=operation,
            publication_state=publication_state,
            directory_sync_state=directory_sync_state,
            destination=_normalize_destination(destination),
        )
        super().__init__(message)
        self.operation = outcome.operation
        self.publication_state = outcome.publication_state
        self.directory_sync_state = outcome.directory_sync_state
        self.destination = outcome.destination
        self.published = outcome.published
        self.original_cause = original_cause
        if original_cause is not None:
            self.__cause__ = original_cause
            self.__suppress_context__ = True

    @property
    def before_publication(self) -> bool:
        """Whether the failure occurred before the destination was published."""
        return not self.published

    @property
    def after_publication(self) -> bool:
        """Whether the destination was published before the failure."""
        return self.published

    @property
    def outcome(self) -> DurabilityOutcome:
        return DurabilityOutcome(
            operation=self.operation,
            publication_state=self.publication_state,
            directory_sync_state=self.directory_sync_state,
            destination=self.destination,
        )


class DurabilityValidationError(ValueError):
    """Raised when a durability outcome violates the shared contract."""
