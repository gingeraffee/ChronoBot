"""Tests for the per-channel data-model migration.

Run from repo root:  python -m pytest tests/test_migrate_per_channel.py -v
Or without pytest:    python tests/test_migrate_per_channel.py
"""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrate_per_channel import (  # noqa: E402
    TARGET_SCHEMA_VERSION,
    UNASSIGNED_KEY,
    migrate_state,
    PER_CHANNEL_FIELDS,
    SERVER_LEVEL_FIELDS,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pre_migration_state.json"


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)


def test_assigned_channel_moves_into_bucket():
    data = migrate_state(load_fixture())
    g = data["guilds"]["1001"]
    assert g["_schema_version"] == TARGET_SCHEMA_VERSION
    assert "event_channel_id" not in g, "old key must be removed"
    assert "555001" in g["channels"], "events keyed by old event_channel_id"
    ch = g["channels"]["555001"]
    assert len(ch["events"]) == 2
    assert ch["pinned_message_id"] == 666001
    assert ch["theme"] == "football"
    assert ch["timezone"] == "America/Chicago"
    assert ch["digest"]["enabled"] is True


def test_server_level_fields_stay_on_guild():
    data = migrate_state(load_fixture())
    g = data["guilds"]["1001"]
    for field in SERVER_LEVEL_FIELDS:
        assert field in g, f"{field} must remain server-level"
    # ...and must NOT have leaked into the channel bucket.
    ch = g["channels"]["555001"]
    for field in SERVER_LEVEL_FIELDS:
        assert field not in ch, f"{field} should not be per-channel"
    assert g["pro"]["pro_active"] is True
    assert g["templates"] == {"party": {"name": "Party", "milestones": [7]}}


def test_orphan_events_preserved_under_sentinel():
    data = migrate_state(load_fixture())
    g = data["guilds"]["1002"]
    assert UNASSIGNED_KEY in g["channels"], "orphan events must survive"
    assert g["channels"][UNASSIGNED_KEY]["events"][0]["name"] == "Orphan Event"
    # sentinel key is non-numeric so the runtime loop can skip it
    assert not UNASSIGNED_KEY.isdigit()


def test_empty_guild_gets_empty_channels():
    data = migrate_state(load_fixture())
    g = data["guilds"]["1003"]
    assert g["channels"] == {}
    assert g["_schema_version"] == TARGET_SCHEMA_VERSION
    # server-level fields still intact
    assert g["welcomed"] is True


def test_legacy_sparse_guild():
    """Guild 1004 is missing most fields (old saved state)."""
    data = migrate_state(load_fixture())
    g = data["guilds"]["1004"]
    assert "555004" in g["channels"]
    assert g["channels"]["555004"]["events"][0]["name"] == "Legacy Event"
    assert "event_channel_id" not in g


def test_already_migrated_guild_untouched():
    before = load_fixture()["guilds"]["1005"]
    data = migrate_state(load_fixture())
    after = data["guilds"]["1005"]
    assert after == before, "already-migrated guild must not change"


def test_idempotent():
    once = migrate_state(load_fixture())
    twice = migrate_state(copy.deepcopy(once))
    assert once == twice, "running migration twice must be a no-op the 2nd time"


def test_no_event_is_lost():
    raw = load_fixture()

    def count_events(state):
        total = 0
        for g in state["guilds"].values():
            if "channels" in g:
                for ch in g["channels"].values():
                    total += len(ch.get("events", []))
            else:
                total += len(g.get("events", []))
        return total

    before = count_events(raw)
    after = count_events(migrate_state(copy.deepcopy(raw)))
    assert before == after, f"event count changed: {before} -> {after}"


def test_top_level_state_versioned_and_user_links_intact():
    data = migrate_state(load_fixture())
    assert data["_schema_version"] == TARGET_SCHEMA_VERSION
    assert data["user_links"] == {"42": 1001}


def test_field_partition_is_disjoint():
    assert not (set(PER_CHANNEL_FIELDS) & set(SERVER_LEVEL_FIELDS))


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
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
