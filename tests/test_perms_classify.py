"""Tests for classify_missing_perms() in chromie.py.

Drives the /seteventchannel warning logic: blocking perms (countdown can't post)
must be separated from degraded perms (posts fine, just no pin/cleanup) so the
command only sounds the alarm when the countdown is actually broken.

Run from repo root:  python tests/test_perms_classify.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_test_state.json")

import chromie  # noqa: E402


def test_critical_is_subset_of_recommended():
    assert set(chromie.CRITICAL_CHANNEL_PERMS) <= set(chromie.RECOMMENDED_CHANNEL_PERMS)


def test_blocking_perms_classified_as_blocking():
    blocking, degraded = chromie.classify_missing_perms(["send_messages", "embed_links"])
    assert blocking == ["send_messages", "embed_links"]
    assert degraded == []


def test_pin_and_history_are_degraded_not_blocking():
    # This is the verify finding: read_message_history was missing yet the
    # countdown posted + pinned, so it must NOT be treated as blocking.
    blocking, degraded = chromie.classify_missing_perms(["read_message_history", "manage_messages"])
    assert blocking == []
    assert degraded == ["read_message_history", "manage_messages"]


def test_mixed_splits_both_ways():
    blocking, degraded = chromie.classify_missing_perms(
        ["view_channel", "manage_messages", "embed_links", "read_message_history"]
    )
    assert blocking == ["view_channel", "embed_links"]
    assert degraded == ["manage_messages", "read_message_history"]


def test_empty_missing_yields_empty():
    assert chromie.classify_missing_perms([]) == ([], [])


def test_every_recommended_perm_is_labelled():
    # The warning names perms via PERM_LABELS — every recommended perm needs a label.
    for p in chromie.RECOMMENDED_CHANNEL_PERMS:
        assert p in chromie.PERM_LABELS


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
