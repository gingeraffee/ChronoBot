"""Verifies per-channel event limits: Free=1, Supporter=3, Pro=unlimited.

Drives the shared add_event_core helper with the tier checks + Discord calls
faked, so it exercises the real limit logic without a live bot.

Run from repo root:  python tests/test_event_limits.py
"""

import os
import sys
import time
import asyncio
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_limits_test.json")

import chromie  # noqa: E402


class FakeUser:
    id = 42
    name = "tester"
    display_name = "Tester"

    def __str__(self):
        return "tester#0001"


class FakeGuild:
    name = "Test Guild"
    id = 1


def _afn(value):
    async def _inner(*a, **k):
        return value
    return _inner


def _channel_state(n_future_events):
    base = int(time.time()) + 365 * 86400  # ~1 year out, all future
    return {
        "events": [{"name": f"e{i}", "timestamp": base + i * 86400} for i in range(n_future_events)],
        "timezone": "UTC",
        "default_milestones": [1],
    }


def _add(cs, *, voted, pro):
    """Call add_event_core with tier checks + Discord faked. Returns (msg, nudge)."""
    chromie.topgg_has_voted = _afn(voted)
    chromie.is_pro = lambda gs: pro
    chromie.get_text_channel = _afn(None)  # skip pin rebuild
    return asyncio.run(chromie.add_event_core(
        FakeGuild(), {"pro": {}, "supporter": {}}, cs, 12345,
        actor=FakeUser(), member=FakeUser(),
        date="12/31/2030", time="12:00", name="New Event",
    ))


def test_free_limit_is_one():
    # 0 events -> allowed
    msg, nudge = _add(_channel_state(0), voted=False, pro=False)
    assert "Added event" in msg, msg
    assert nudge is True, "free user at their 1-event cap should be nudged"
    # 1 event -> blocked
    msg, _ = _add(_channel_state(1), voted=False, pro=False)
    assert "Event limit reached" in msg and "Free tier limit" in msg, msg


def test_supporter_limit_is_three():
    # 2 events -> allowed
    msg, _ = _add(_channel_state(2), voted=True, pro=False)
    assert "Added event" in msg, msg
    # 3 events -> blocked
    msg, _ = _add(_channel_state(3), voted=True, pro=False)
    assert "Event limit reached" in msg and "Supporter tier limit" in msg, msg


def test_pro_is_unlimited():
    msg, _ = _add(_channel_state(25), voted=False, pro=True)
    assert "Added event" in msg, msg
    assert "limit reached" not in msg.lower(), msg


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
