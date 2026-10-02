from __future__ import annotations

import unittest

from oopz_capture.identity import build_identity_mappings
from oopz_capture.models import OopzParticipant, ProbeSnapshot


class IdentityMappingTests(unittest.TestCase):
    def test_data_stream_mapping_is_verified(self) -> None:
        participants = [OopzParticipant("oopz-a", "Alice", "1001")]
        snapshot = ProbeSnapshot(
            remote_users=[{"uid": "1001"}],
            voice_states=[{"uid": "oopz-a", "cid": 1001}],
        )

        result = build_identity_mappings(participants, snapshot)

        self.assertEqual(result[0].agora_uid, 1001)
        self.assertEqual(result[0].status, "verified_data_stream")
        self.assertTrue(result[0].verified)

    def test_remote_uid_matching_person_pid_is_verified(self) -> None:
        participants = [OopzParticipant("oopz-b", "Bob", "2002")]
        snapshot = ProbeSnapshot(remote_users=[{"uid": 2002}])

        result = build_identity_mappings(participants, snapshot)

        self.assertEqual(result[0].status, "verified_remote_user_pid")

    def test_person_pid_without_remote_observation_is_inferred(self) -> None:
        participants = [OopzParticipant("oopz-c", "Carol", "3003")]

        result = build_identity_mappings(participants, ProbeSnapshot())

        self.assertEqual(result[0].agora_uid, 3003)
        self.assertEqual(result[0].status, "inferred_person_pid")
        self.assertFalse(result[0].verified)

    def test_non_numeric_pid_is_unresolved(self) -> None:
        participants = [OopzParticipant("oopz-d", "Dan", "not-a-number")]

        result = build_identity_mappings(participants, ProbeSnapshot())

        self.assertIsNone(result[0].agora_uid)
        self.assertEqual(result[0].status, "unresolved")

    def test_self_mapping_can_be_verified_without_remote_user(self) -> None:
        participants = [OopzParticipant("self-uid", "Recorder", "4004")]

        result = build_identity_mappings(
            participants,
            ProbeSnapshot(),
            self_oopz_uid="self-uid",
            self_agora_uid="4004",
        )

        self.assertEqual(result[0].status, "verified_local_join")

    def test_observed_cid_wins_and_mismatch_is_reported(self) -> None:
        participants = [OopzParticipant("oopz-e", "Eve", "5005")]
        snapshot = ProbeSnapshot(
            remote_users=[{"uid": 5999}],
            voice_states=[{"uid": "oopz-e", "cid": 5999}],
        )

        result = build_identity_mappings(participants, snapshot)

        self.assertEqual(result[0].agora_uid, 5999)
        self.assertTrue(any("differs" in item for item in result[0].evidence))


if __name__ == "__main__":
    unittest.main()
