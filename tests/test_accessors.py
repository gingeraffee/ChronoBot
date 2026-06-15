"""Tests for the per-channel accessor layer in chromie.py.

Imports the real chromie module (with a throwaway CHROMIE_DATA_PATH so it never
touches production state), then exercises the accessor + gating helpers.

Run from repo root:  python tests/test_accessors.py
"""

import os
import sys
import json
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Point chromie at a throwaway state file BEFORE importing it.
os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_test_state.json")

import chromie  # noqa: E402
from migrate_per_channel import migrate_state, UNASSIGNED_KEY  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "pre_migration_state.json"

_gid = iter(range(900000, 999999))  # unique guild ids so tests don't collide


def fresh_gid():
    return next(_gid)


# ---- get_channel_state ----

def test_get_channel_state_creates_bucket_with_defaults():
    gid, cid = fresh_gid(), 12345
    cs = chromie.get_channel_state(gid, cid)
    assert cs["events"] == []
    assert cs["theme"] == chromie.DEFAULT_THEME_ID
    assert cs["timezone"] == "UTC"
    assert cs["pinned_message_id"] is None
    assert cs["digest"] == {"enabled": False, "channel_id": None, "last_sent_date": None}
    # the bucket is stored under the string channel id
    assert str(cid) in chromie.get_guild_state(gid)["channels"]


def test_get_channel_state_is_stable():
    gid, cid = fresh_gid(), 222
    a = chromie.get_channel_state(gid, cid)
    a["events"].append({"name": "x", "timestamp": 1})
    b = chromie.get_channel_state(gid, cid)
    assert b is a, "same channel must return the same bucket object"
    assert len(b["events"]) == 1


def test_get_channel_state_backfills_old_bucket():
    gid, cid = fresh_gid(), 333
    # simulate an older bucket missing newer fields
    guild = chromie.get_guild_state(gid)
    guild["channels"][str(cid)] = {"events": [{"name": "old", "timestamp": 1}]}
    cs = chromie.get_channel_state(gid, cid)
    assert cs["theme"] == chromie.DEFAULT_THEME_ID  # backfilled
    assert cs["timezone"] == "UTC"                   # backfilled
    assert cs["events"][0]["name"] == "old"          # preserved


# ---- counting / iterating ----

def test_count_and_iter_skip_unassigned():
    guild = {"channels": {
        "100": {"events": []},
        "200": {"events": []},
        UNASSIGNED_KEY: {"events": [{"name": "orphan", "timestamp": 1}]},
    }}
    assert chromie.count_countdown_channels(guild) == 2  # sentinel not counted
    ids = sorted(cid for cid, _ in chromie.iter_channel_states(guild))
    assert ids == [100, 200]  # sentinel skipped, ids are ints


# ---- gating (1 free, Plus = unlimited) ----

def test_free_server_capped_at_one_channel():
    guild = {"channels": {}, "pro": {"pro_active": False}}
    assert chromie.can_add_countdown_channel(guild, 1) is True   # first is free
    guild["channels"]["1"] = {"events": []}
    assert chromie.can_add_countdown_channel(guild, 2) is False  # second blocked
    assert chromie.can_add_countdown_channel(guild, 1) is True   # existing always ok


def test_pro_server_unlimited_channels():
    guild = {"channels": {"1": {}, "2": {}, "3": {}},
             "pro": {"discord_subscription": True}}  # is_pro -> True
    assert chromie.is_pro(guild) is True
    assert chromie.can_add_countdown_channel(guild, 99) is True


# ---- additive: legacy behavior preserved ----

def test_get_guild_state_is_additive():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    # new scaffolding present...
    assert g["channels"] == {}
    # ...and legacy fields still present (current commands keep working)
    for legacy in ("event_channel_id", "events", "theme", "pro", "templates", "welcomed"):
        assert legacy in g


# ---- integration with the migration output ----

def test_accessors_work_on_migrated_state():
    data = migrate_state(json.loads(FIXTURE.read_text(encoding="utf-8")))
    g1001 = data["guilds"]["1001"]   # had event_channel_id 555001 + 2 events
    assert chromie.count_countdown_channels(g1001) == 1
    cid, cs = next(chromie.iter_channel_states(g1001))
    assert cid == 555001
    assert len(cs["events"]) == 2
    # 1001 is Pro in the fixture -> can add more channels
    assert chromie.can_add_countdown_channel(g1001, 999) is True

    g1002 = data["guilds"]["1002"]   # orphan events under sentinel, not pro
    assert chromie.count_countdown_channels(g1002) == 0  # sentinel doesn't count
    assert chromie.can_add_countdown_channel(g1002, 1) is True   # 1st free
    g1002["channels"]["1"] = {"events": []}
    assert chromie.can_add_countdown_channel(g1002, 2) is False  # 2nd needs Plus


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
