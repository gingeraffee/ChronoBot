"""Tests for onboarding / churn-diagnostics helpers in chromie.py.

The on-join guild command sync and the on_guild_join/remove handlers need a live
Discord connection, so those are validated by re-adding the bot to the test guild.
This covers the pure logic: the time-in-server formatter used in the leave log.

Run from the repo root:
    python tests/test_onboarding.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("CHROMIE_DATA_PATH", os.path.join(tempfile.gettempdir(), "chromie_onboarding.json"))

import chromie  # noqa: E402


def test_tenure_unknown_without_join():
    assert chromie._format_guild_tenure(None) == "unknown"
    assert chromie._format_guild_tenure(0) == "unknown"     # falsy / never recorded


def test_tenure_buckets():
    base = 1_000_000.0
    assert chromie._format_guild_tenure(base, now=base + 5) == "5s"
    assert chromie._format_guild_tenure(base, now=base + 30) == "30s"
    assert chromie._format_guild_tenure(base, now=base + 120) == "2m"      # the churn signal
    assert chromie._format_guild_tenure(base, now=base + 3 * 3600) == "3h"
    assert chromie._format_guild_tenure(base, now=base + 2 * 86400) == "2d"


def test_tenure_clamps_negative_clock_skew():
    assert chromie._format_guild_tenure(1000, now=500) == "0s"


def test_mark_stint_activation_stamps_once_then_counts():
    # First add stamps activated_at + counts; later adds keep the first-add
    # timestamp (so time-to-activate stays honest) but keep climbing the count.
    g = {}
    chromie._mark_stint_activation(g)
    first = g["activated_at"]
    assert first and g["events_created"] == 1
    chromie._mark_stint_activation(g)
    assert g["activated_at"] == first   # not re-stamped on the 2nd add
    assert g["events_created"] == 2     # but the count climbs


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {_name}: {e}")
            except Exception as e:
                failures += 1
                print(f"ERROR {_name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
