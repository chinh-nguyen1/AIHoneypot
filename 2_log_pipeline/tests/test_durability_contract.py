import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

from durability_contract import (
    DirectorySyncState,
    DurabilityError,
    DurabilityOutcome,
    PublicationState,
)


class DurabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.destination = Path("temporary") / "event.json"

    def outcome(self, publication, directory_sync):
        return DurabilityOutcome(
            operation="inbox_write",
            publication_state=publication,
            directory_sync_state=directory_sync,
            destination=self.destination,
        )

    def test_valid_state_combinations(self):
        combinations = (
            (PublicationState.NOT_PUBLISHED, DirectorySyncState.NOT_ATTEMPTED),
            (
                PublicationState.PUBLISHED_UNCONFIRMED,
                DirectorySyncState.NOT_ATTEMPTED,
            ),
            (
                PublicationState.PUBLISHED_UNCONFIRMED,
                DirectorySyncState.UNSUPPORTED,
            ),
            (
                PublicationState.PUBLISHED_UNCONFIRMED,
                DirectorySyncState.FAILED,
            ),
            (PublicationState.DURABLE, DirectorySyncState.SYNCED),
        )
        for publication, directory_sync in combinations:
            with self.subTest(publication=publication, directory_sync=directory_sync):
                self.outcome(publication, directory_sync)

    def test_rejects_invalid_state_combinations(self):
        combinations = (
            (PublicationState.NOT_PUBLISHED, DirectorySyncState.SYNCED),
            (PublicationState.NOT_PUBLISHED, DirectorySyncState.UNSUPPORTED),
            (PublicationState.NOT_PUBLISHED, DirectorySyncState.FAILED),
            (
                PublicationState.PUBLISHED_UNCONFIRMED,
                DirectorySyncState.SYNCED,
            ),
            (PublicationState.DURABLE, DirectorySyncState.NOT_ATTEMPTED),
            (PublicationState.DURABLE, DirectorySyncState.UNSUPPORTED),
            (PublicationState.DURABLE, DirectorySyncState.FAILED),
        )
        for publication, directory_sync in combinations:
            with self.subTest(publication=publication, directory_sync=directory_sync):
                with self.assertRaises(ValueError):
                    self.outcome(publication, directory_sync)

    def test_durable_requires_synced_directory(self):
        with self.assertRaisesRegex(ValueError, "requires directory sync"):
            self.outcome(
                PublicationState.DURABLE,
                DirectorySyncState.NOT_ATTEMPTED,
            )

    def test_unconfirmed_publication_is_not_durable_success(self):
        for directory_sync in (
            DirectorySyncState.NOT_ATTEMPTED,
            DirectorySyncState.UNSUPPORTED,
            DirectorySyncState.FAILED,
        ):
            result = self.outcome(
                PublicationState.PUBLISHED_UNCONFIRMED,
                directory_sync,
            )
            self.assertTrue(result.published)
            self.assertFalse(result.is_durable_success)

    def test_not_published_cannot_report_published_true(self):
        result = self.outcome(
            PublicationState.NOT_PUBLISHED,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        self.assertFalse(result.published)
        self.assertFalse(result.is_durable_success)

    def test_published_outcome_preserves_operation_and_destination(self):
        result = self.outcome(
            PublicationState.DURABLE,
            DirectorySyncState.SYNCED,
        )
        self.assertEqual(result.operation, "inbox_write")
        self.assertEqual(result.destination, self.destination)
        self.assertTrue(result.published)

    def test_outcome_is_immutable(self):
        result = self.outcome(
            PublicationState.NOT_PUBLISHED,
            DirectorySyncState.NOT_ATTEMPTED,
        )
        with self.assertRaises(FrozenInstanceError):
            result.operation = "checkpoint_write"

    def test_exception_preserves_original_cause_and_outcome(self):
        cause = OSError("directory sync failed")
        error = DurabilityError(
            "inbox event published but not confirmed durable",
            operation="inbox_write",
            publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
            directory_sync_state=DirectorySyncState.FAILED,
            destination=self.destination,
            original_cause=cause,
        )

        self.assertIs(error.original_cause, cause)
        self.assertIs(error.__cause__, cause)
        self.assertEqual(error.operation, "inbox_write")
        self.assertEqual(error.destination, self.destination)
        self.assertTrue(error.published)
        self.assertTrue(error.after_publication)
        self.assertFalse(error.before_publication)
        self.assertEqual(error.outcome.publication_state, error.publication_state)

    def test_exception_distinguishes_pre_and_post_publication(self):
        cause = OSError("file fsync failed")
        before = DurabilityError(
            "write failed before publication",
            operation="checkpoint_write",
            publication_state=PublicationState.NOT_PUBLISHED,
            directory_sync_state=DirectorySyncState.NOT_ATTEMPTED,
            destination=self.destination,
            original_cause=cause,
        )
        after = DurabilityError(
            "directory sync failed after publication",
            operation="checkpoint_write",
            publication_state=PublicationState.PUBLISHED_UNCONFIRMED,
            directory_sync_state=DirectorySyncState.FAILED,
            destination=self.destination,
            original_cause=cause,
        )

        self.assertTrue(before.before_publication)
        self.assertFalse(before.after_publication)
        self.assertFalse(before.published)
        self.assertTrue(after.after_publication)
        self.assertFalse(after.before_publication)
        self.assertTrue(after.published)


if __name__ == "__main__":
    unittest.main()
