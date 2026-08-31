"""Tests for unattended repair.

These cover the decisions that make automatic repair safe rather than
reckless: knowing when NOT to repair, and refusing to keep repairing
something that keeps breaking.

Run with:  python3 tests/test_repair.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_anchor import repair                              # noqa: E402
from drive_anchor.config import Config, Drive, RepairConfig  # noqa: E402
from drive_anchor.verify import (                            # noqa: E402
    DEVICE_ABSENT, EMPTY_STUB, NOT_MOUNTED, Problem)


def drive(n):
    return Drive(name=f"d{n}", uuid=f"uuid-{n}", path=f"/volume1/d{n}")


class Classification(unittest.TestCase):
    """Deciding whether repairing is the right move at all."""

    def test_no_problems_is_healthy(self):
        self.assertEqual(repair.classify([], 3), repair.HEALTHY)

    def test_all_drives_absent_is_wholesale(self):
        """Every drive gone means the enclosure is off or a restore is
        running -- not a stale bind. Re-binding would wait the full timeout
        for each drive and fix nothing."""
        problems = [Problem(drive(i), NOT_MOUNTED) for i in range(3)]
        self.assertEqual(repair.classify(problems, 3), repair.WHOLESALE)

    def test_mixed_absent_reasons_still_wholesale(self):
        problems = [Problem(drive(0), NOT_MOUNTED),
                    Problem(drive(1), DEVICE_ABSENT)]
        self.assertEqual(repair.classify(problems, 2), repair.WHOLESALE)

    def test_some_drives_healthy_is_partial(self):
        """One broken out of three is the stale-bind signature."""
        problems = [Problem(drive(0), DEVICE_ABSENT)]
        self.assertEqual(repair.classify(problems, 3), repair.PARTIAL)

    def test_all_broken_but_one_is_empty_stub_is_PARTIAL(self):
        """An empty stub means that device IS present and only the bind is
        wrong -- so the hardware is around and re-binding can work. This must
        not be mistaken for a wholesale outage."""
        problems = [Problem(drive(0), NOT_MOUNTED),
                    Problem(drive(1), EMPTY_STUB)]
        self.assertEqual(repair.classify(problems, 2), repair.PARTIAL)


class RateLimit(unittest.TestCase):
    """The cap that stops self-healing from hiding a dying drive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(drives=[drive(0)],
                          repair=RepairConfig(max_per_hour=3,
                                              state_dir=self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starts_at_zero(self):
        self.assertEqual(repair.repairs_last_hour(self.cfg), 0)

    def test_records_and_counts(self):
        repair.record_repair(self.cfg)
        repair.record_repair(self.cfg)
        self.assertEqual(repair.repairs_last_hour(self.cfg), 2)

    def test_old_entries_are_not_counted(self):
        path = os.path.join(self.tmp, "repair_history")
        old = time.time() - 7200          # two hours ago
        recent = time.time() - 60
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{old}\n{old}\n{recent}\n")
        self.assertEqual(repair.repairs_last_hour(self.cfg), 1)

    def test_old_entries_are_pruned_from_the_file(self):
        """Otherwise the history grows without bound on a scheduled job."""
        path = os.path.join(self.tmp, "repair_history")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(str(time.time() - 7200) for _ in range(50)))
        repair.repairs_last_hour(self.cfg)
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "")

    def test_corrupt_history_does_not_crash(self):
        path = os.path.join(self.tmp, "repair_history")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not a number\nalso not\n")
        self.assertEqual(repair.repairs_last_hour(self.cfg), 0)

    def test_one_corrupt_entry_does_not_zero_the_whole_count(self):
        """Regression: the cap must fail CLOSED, not open.

        A single unparseable token used to make the count 0, silently
        disabling the limit while appearing to work -- which is worse than
        having no limit at all.
        """
        path = os.path.join(self.tmp, "repair_history")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{now - 10}\ngarbage.5.5\n{now - 20}\n")
        self.assertEqual(repair.repairs_last_hour(self.cfg), 2)

    def test_prune_leaves_a_trailing_newline_so_appends_stay_separate(self):
        """Regression: the real cause of the corrupt entry above.

        repairs_last_hour() rewrites the file to prune old entries, and
        record_repair() appends to it. Without a trailing newline the last
        pruned timestamp and the next appended one merge into one token.
        """
        path = os.path.join(self.tmp, "repair_history")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{now - 7200}\n{now - 30}\n")   # one old, one recent
        self.assertEqual(repair.repairs_last_hour(self.cfg), 1)  # prunes
        repair.record_repair(self.cfg)                            # appends
        self.assertEqual(repair.repairs_last_hour(self.cfg), 2,
                         "the appended entry merged with the pruned one")

    def test_a_missing_state_dir_is_simply_created(self):
        """This tool runs as root, so a not-yet-existing directory is not a
        failure -- it gets made. Only a genuinely impossible path degrades."""
        target = os.path.join(self.tmp, "does", "not", "exist", "yet")
        cfg = Config(drives=[drive(0)], repair=RepairConfig(state_dir=target))
        repair.record_repair(cfg)
        self.assertEqual(repair.repairs_last_hour(cfg), 1)

    def test_unwritable_state_dir_degrades_instead_of_failing(self):
        """A state dir that cannot exist must not stop a repair -- a single
        run can only repair once anyway. It just cannot enforce the cap
        across runs, and it warns about that.

        Uses a path whose parent is a FILE, which makedirs cannot create even
        as root. An ordinary missing path would simply be created, so it does
        not test anything here.
        """
        blocker = os.path.join(self.tmp, "a-file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        cfg = Config(drives=[drive(0)],
                     repair=RepairConfig(state_dir=os.path.join(blocker, "sub")))
        self.assertEqual(repair.repairs_last_hour(cfg), 0)
        repair.record_repair(cfg)  # must not raise
        self.assertEqual(repair.repairs_last_hour(cfg), 0)


class OutcomeReporting(unittest.TestCase):
    """Exit codes are the interface -- the caller is usually a scheduler."""

    def test_healthy_needs_no_attention(self):
        o = repair.Outcome(repair.HEALTHY, [], [])
        self.assertFalse(o.needs_attention)
        self.assertEqual(o.exit_code, 0)

    def test_successful_repair_needs_no_attention(self):
        o = repair.Outcome(repair.PARTIAL, ["d0"], [])
        self.assertFalse(o.needs_attention)
        self.assertEqual(o.exit_code, 0)

    def test_remaining_problems_need_attention(self):
        o = repair.Outcome(repair.PARTIAL, [], [Problem(drive(0), NOT_MOUNTED)])
        self.assertTrue(o.needs_attention)
        self.assertEqual(o.exit_code, 1)

    def test_a_refusal_needs_attention_even_with_nothing_remaining(self):
        """Refusing to repair is not success. A scheduler must be able to
        tell 'I fixed it' from 'I declined to try'."""
        o = repair.Outcome(repair.WHOLESALE, [], [], refused_reason="drives off")
        self.assertTrue(o.needs_attention)
        self.assertEqual(o.exit_code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
