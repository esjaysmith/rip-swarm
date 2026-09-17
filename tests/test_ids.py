import re
import unittest
from datetime import datetime, timezone
from rip_swarm.ids import new_ulid, new_task_id, new_claim_id, new_msg_id

_CROCKFORD = re.compile(r"^[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{26}$")


class TestIds(unittest.TestCase):
    def test_ulid_shape_and_time_ordering(self):
        t1 = datetime(2026, 9, 17, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 17, 9, 0, 1, tzinfo=timezone.utc)
        a, b = new_ulid(t1), new_ulid(t2)
        self.assertRegex(a, _CROCKFORD)
        self.assertLess(a, b)

    def test_prefixes(self):
        self.assertTrue(new_task_id().startswith("task_"))
        self.assertTrue(new_claim_id().startswith("clm_"))
        self.assertTrue(new_msg_id().startswith("msg_"))
        self.assertEqual(len(new_task_id()), 5 + 26)


if __name__ == "__main__":
    unittest.main()
