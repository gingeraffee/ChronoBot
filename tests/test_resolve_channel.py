"""Tests for resolve_event_channel() + no_channel_guidance() in chromie.py.

These drive the per-channel CRUD command sweep: every event command resolves
which countdown channel it acts on through resolve_event_channel().

Run from repo root:  python tests/test_resolve_channel.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_test_state.json")

import chromie  # noqa: E402
from migrate_per_channel import UNASSIGNED_KEY  # noqa: E402


# ---- resolve_event_channel ----

def test_resolves_current_channel_when_it_is_a_countdown_channel():
    guild = {"channels": {"100": {"events": [1]}, "200": {"events": [2]}},
             "pro": {"discord_subscription": True}}
    cid, cs = chromie.resolve_event_channel(guild, 200)
    assert cid == 200
    assert cs is guild["channels"]["200"]


def test_falls_back_to_single_channel_for_other_channel():
    # Command run in a NON-countdown channel; guild has exactly one countdown
    # channel -> use it (back-compat for single-channel servers).
    guild = {"channels": {"100": {"events": []}}}
    cid, cs = chromie.resolve_event_channel(guild, 999999)
    assert cid == 100
    assert cs is guild["channels"]["100"]


def test_falls_back_to_single_channel_for_dm_none():
    # DM path passes channel_id=None; still resolves the one channel.
    guild = {"channels": {"100": {"events": []}}}
    cid, cs = chromie.resolve_event_channel(guild, None)
    assert cid == 100


def test_zero_channels_returns_none():
    guild = {"channels": {}}
    assert chromie.resolve_event_channel(guild, 123) == (None, None)


def test_multiple_channels_ambiguous_returns_none():
    # 2+ channels and the current channel isn't one of them -> ambiguous.
    guild = {"channels": {"100": {}, "200": {}}}
    assert chromie.resolve_event_channel(guild, 999999) == (None, None)
    # but running INSIDE one of them resolves fine
    cid, _cs = chromie.resolve_event_channel(guild, 100)
    assert cid == 100


def test_unassigned_sentinel_is_not_resolvable_and_not_counted():
    # An orphan bucket alone means 0 real channels -> not resolvable.
    guild = {"channels": {UNASSIGNED_KEY: {"events": [{"name": "orphan", "timestamp": 1}]}}}
    assert chromie.resolve_event_channel(guild, None) == (None, None)
    # One real channel + a sentinel -> still resolves to the real one.
    guild["channels"]["100"] = {"events": []}
    cid, _cs = chromie.resolve_event_channel(guild, None)
    assert cid == 100


# ---- no_channel_guidance ----

def test_guidance_zero_channels_points_to_seteventchannel():
    guild = {"channels": {}}
    msg = chromie.no_channel_guidance(guild, "/addevent")
    assert "/seteventchannel" in msg


def test_guidance_multiple_channels_points_to_running_inside():
    guild = {"channels": {"1": {}, "2": {}}}
    msg = chromie.no_channel_guidance(guild, "/addevent")
    assert "multiple countdown channels" in msg.lower()
    assert "/addevent" in msg


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
