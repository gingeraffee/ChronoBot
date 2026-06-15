"""Integration tests for the per-channel countdown engine in chromie.py.

Seeds a v1 (un-migrated) state file, imports chromie (which runs the startup
migration), and verifies the update loop iterates per channel. Discord calls are
faked so nothing touches the network.

Run from repo root:  python tests/test_engine.py
"""

import os
import sys
import json
import asyncio
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Seed a v1 (pre-migration) state file BEFORE importing chromie, so the startup
# migration runs against it.
_fixture = json.loads((REPO / "tests" / "fixtures" / "pre_migration_state.json").read_text(encoding="utf-8"))
_tmp = Path(tempfile.gettempdir()) / "chromie_engine_test_state.json"
_tmp.write_text(json.dumps(_fixture), encoding="utf-8")
os.environ["CHROMIE_DATA_PATH"] = str(_tmp)

import chromie  # noqa: E402  (import after env setup)
from migrate_per_channel import TARGET_SCHEMA_VERSION  # noqa: E402


def test_startup_migrated_the_state():
    guilds = chromie.state.get("guilds", {})
    assert guilds, "no guilds loaded"
    for gid, g in guilds.items():
        assert "channels" in g, f"guild {gid} was not migrated to per-channel"
    # guild 1001 had event_channel_id 555001 with two events
    ch = chromie.state["guilds"]["1001"]["channels"]
    assert "555001" in ch
    assert len(ch["555001"]["events"]) == 2
    assert chromie.state["guilds"]["1001"].get("_schema_version") == TARGET_SCHEMA_VERSION


def test_update_loop_runs_one_cycle_per_real_channel():
    calls = []

    class FakeGuild:
        id = 1

    class FakeChannel:
        def __init__(self, cid):
            self.id = cid
            self.guild = FakeGuild()

    async def fake_get_text_channel(cid):
        return FakeChannel(int(cid))

    async def fake_get_bot_member(guild):
        return object()  # truthy

    async def fake_cycle(guild_id, channel_state, channel, bot_member, server_state):
        calls.append((guild_id, channel.id))

    # Patch the module globals the loop resolves at call time.
    orig = (chromie.get_text_channel, chromie.get_bot_member, chromie._run_countdown_cycle)
    chromie.get_text_channel = fake_get_text_channel
    chromie.get_bot_member = fake_get_bot_member
    chromie._run_countdown_cycle = fake_cycle
    try:
        asyncio.run(chromie.update_countdowns.coro())
    finally:
        chromie.get_text_channel, chromie.get_bot_member, chromie._run_countdown_cycle = orig

    chan_ids = sorted(cid for _, cid in calls)
    # fixture: 1001->555001, 1004->555004, 1005->555005 (already migrated);
    # 1002 has only the "unassigned" sentinel (skipped), 1003 has no channels.
    assert 555001 in chan_ids
    assert 555004 in chan_ids
    assert 555005 in chan_ids
    assert chan_ids == [555001, 555004, 555005], f"unexpected cycles: {chan_ids}"
    # sentinel bucket must never be turned into a real channel id
    assert all(isinstance(cid, int) for _, cid in calls)


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
