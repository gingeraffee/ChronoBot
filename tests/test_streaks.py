"""Tests for streak-mode foundations in chromie.py (Phase 1 + Phase 2).

Covers the additive data model (event `type`, channel `kind`), the
`is_streak_*` helpers, and prune-exemption for streak-type events.

Run from the repo root:
    python tests/test_streaks.py
or with pytest:
    python -m pytest tests/test_streaks.py -v
"""

import os
import sys
import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Dedicated, freshly-cleared state file so we never touch real data.
_DATA_PATH = os.path.join(tempfile.gettempdir(), "chromie_test_streaks_state.json")
try:
    os.remove(_DATA_PATH)
except OSError:
    pass
os.environ["CHROMIE_DATA_PATH"] = _DATA_PATH

import chromie  # noqa: E402  (import AFTER env setup so startup runs against our state)

# Network-free, deterministic voting behavior (no Top.gg calls in tests).
chromie.TOPGG_TOKEN = ""
chromie.TOPGG_FAIL_OPEN = False

_gid_seq = iter(range(810000, 819999))


def fresh_gid():
    return next(_gid_seq)


# Module-level fakes for exercising core functions without a Discord connection.
class _FakeUser:
    id = 11
    name = "tester"

    def __str__(self):
        return self.name


class _FakeMember:
    display_name = "Tester"


class _FakeGuild:
    name = "Test Guild"


async def _no_channel(_cid):
    return None


chromie.get_text_channel = _no_channel  # never touch Discord in these tests


# ---------------------------------------------------------------------------
# Phase 1 — data model + helpers
# ---------------------------------------------------------------------------

def test_new_channel_defaults_to_countdown_kind():
    gid = fresh_gid()
    cs = chromie.get_channel_state(gid, 1)
    assert cs["kind"] == "countdown"
    assert chromie.is_streak_channel(cs) is False


def test_existing_channel_without_kind_is_backfilled():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    # Simulate an OLD per-channel bucket saved before `kind` existed.
    g.setdefault("channels", {})["1234"] = {"events": []}
    cs = chromie.get_channel_state(gid, 1234)
    assert cs["kind"] == "countdown"  # backfilled by get_channel_state


def test_is_streak_channel_detects_streak_kind():
    assert chromie.is_streak_channel({"kind": "streak"}) is True
    assert chromie.is_streak_channel({"kind": "countdown"}) is False
    assert chromie.is_streak_channel({}) is False  # missing => countdown


def test_is_streak_event_detects_streak_type():
    assert chromie.is_streak_event({"type": "streak"}) is True
    assert chromie.is_streak_event({"type": "countdown"}) is False
    assert chromie.is_streak_event({}) is False  # missing => countdown


def test_addevent_tags_events_as_countdown():
    # New countdown events should carry an explicit type for clarity/symmetry.
    class FakeUser:
        id = 7
        name = "tester"

    class FakeMember:
        display_name = "Tester"

    class FakeGuild:
        name = "Test Guild"

    async def _no_channel(_cid):
        return None

    chromie.get_text_channel = _no_channel

    gid, cid = fresh_gid(), 1
    g = chromie.get_guild_state(gid)
    cs = chromie.get_channel_state(gid, cid)
    msg, _nudge = asyncio.run(chromie.add_event_core(
        FakeGuild(), g, cs, cid,
        actor=FakeUser(), member=FakeMember(),
        date="12/31/2099", time="09:00", name="NYE",
    ))
    assert "Added event" in msg
    assert cs["events"][0]["type"] == "countdown"


# ---------------------------------------------------------------------------
# Phase 2 — prune exemption
# ---------------------------------------------------------------------------

def test_streak_survives_prune_while_past_countdown_is_removed():
    past = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())
    bucket = {
        "timezone": "UTC",
        "events": [
            {"name": "smoke-free", "type": "streak", "timestamp": past},
            {"name": "old party", "type": "countdown", "timestamp": past},
        ],
    }
    future_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    removed = chromie.prune_past_events(bucket, now=future_now)

    assert removed == 1
    names = [e["name"] for e in bucket["events"]]
    assert "smoke-free" in names      # streak is exempt
    assert "old party" not in names   # countdown is pruned


def test_untyped_past_event_still_prunes_as_countdown():
    past = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())
    bucket = {"timezone": "UTC", "events": [{"name": "legacy", "timestamp": past}]}
    removed = chromie.prune_past_events(bucket, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert removed == 1
    assert bucket["events"] == []


# ---------------------------------------------------------------------------
# Phase 3 — channel-slot limits (own slot per kind; streaks skip the vote-boost)
# ---------------------------------------------------------------------------

def _make_channel(gid, cid, kind="countdown"):
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = kind
    return cs


def test_counts_are_kind_aware():
    gid = fresh_gid()
    _make_channel(gid, 1, "countdown")
    _make_channel(gid, 2, "streak")
    _make_channel(gid, 3, "countdown")
    g = chromie.get_guild_state(gid)
    assert chromie.count_countdown_channels(g) == 2
    assert chromie.count_streak_channels(g) == 1


def test_streak_channel_does_not_consume_countdown_slot():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    _make_channel(gid, 2, "streak")  # free server's one streak channel
    assert chromie.count_countdown_channels(g) == 0
    assert chromie.can_add_countdown_channel(g, 99) is True  # countdown slot still free
    assert chromie.can_add_streak_channel(g, 2) is True      # already a streak channel


def test_free_server_capped_at_one_streak_channel():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    _make_channel(gid, 1, "streak")
    assert chromie.can_add_streak_channel(g, 1) is True      # the existing streak channel
    assert chromie.can_add_streak_channel(g, 999) is False   # a 2nd streak channel is blocked on free


def test_pro_server_unlimited_streak_channels():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    g["pro"] = {"discord_subscription": True}
    _make_channel(gid, 1, "streak")
    assert chromie.can_add_streak_channel(g, 999) is True    # pro: unlimited


def test_default_streak_milestones_sorted_ascending():
    m = chromie.DEFAULT_STREAK_MILESTONES
    assert m == sorted(m)
    assert m[0] == 1     # Day-1 kickoff
    assert 365 in m      # one-year anniversary in the ladder


# ---------------------------------------------------------------------------
# Phase 3 — add_streak_core (creation, past-date rule, backdate seeding, limits)
# ---------------------------------------------------------------------------

def _add_streak(gid, cid, *, date, name="Smoke-free"):
    g = chromie.get_guild_state(gid)
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    res = asyncio.run(chromie.add_streak_core(
        _FakeGuild(), g, cs, cid,
        actor=_FakeUser(), member=_FakeMember(), date=date, name=name,
    ))
    return res, cs


def test_add_streak_creates_streak_type():
    gid, cid = fresh_gid(), 1
    (msg, _nudge), cs = _add_streak(gid, cid, date="01/01/2020", name="Sober")
    assert "Streak" in msg
    assert len(cs["events"]) == 1
    ev = cs["events"][0]
    assert ev["type"] == "streak"
    assert chromie.is_streak_event(ev)


def test_add_streak_rejects_future_start():
    gid, cid = fresh_gid(), 1
    (msg, _nudge), cs = _add_streak(gid, cid, date="12/31/2099")
    assert "future" in msg.lower()
    assert cs["events"] == []


def test_add_streak_backdate_seeds_passed_milestones():
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%m/%d/%Y")
    gid, cid = fresh_gid(), 1
    (_msg, _nudge), cs = _add_streak(gid, cid, date=start, name="100 days")
    announced = cs["events"][0]["announced_milestones"]
    assert 1 in announced and 90 in announced and 100 in announced  # already passed -> seeded
    assert 180 not in announced and 365 not in announced            # still ahead -> will fire live


def test_add_streak_free_limited_to_one():
    gid, cid = fresh_gid(), 1
    (msg1, _), cs = _add_streak(gid, cid, date="01/01/2020", name="One")
    assert "Streak" in msg1
    (msg2, _), cs = _add_streak(gid, cid, date="01/01/2021", name="Two")
    assert "Pro" in msg2  # second streak is gated on free
    assert len([e for e in cs["events"] if chromie.is_streak_event(e)]) == 1


def test_add_streak_pro_allows_multiple():
    gid, cid = fresh_gid(), 1
    g = chromie.get_guild_state(gid)
    g["pro"] = {"discord_subscription": True}
    _add_streak(gid, cid, date="01/01/2020", name="One")
    (msg2, _), cs = _add_streak(gid, cid, date="01/01/2021", name="Two")
    assert "Streak" in msg2
    assert len([e for e in cs["events"] if chromie.is_streak_event(e)]) == 2


def test_streak_board_embed_shows_day_count():
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=42)).strftime("%m/%d/%Y")
    gid, cid = fresh_gid(), 1
    (_msg, _nudge), cs = _add_streak(gid, cid, date=start, name="Gym streak")
    g = chromie.get_guild_state(gid)
    embed = chromie.build_streak_embed_for_channel(cs, g)
    blob = (embed.title or "") + (embed.description or "")
    assert "Gym streak" in blob
    assert "42" in blob  # the days-since count is rendered


# ---------------------------------------------------------------------------
# Phase 3 — resolve_streak_channel (which streak board a command acts on)
# ---------------------------------------------------------------------------

def test_resolve_streak_channel_uses_current_if_streak():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    _make_channel(gid, 5, "streak")
    cid, cs = chromie.resolve_streak_channel(g, 5)
    assert cid == 5 and cs is not None


def test_resolve_streak_channel_uses_sole_streak_when_run_elsewhere():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    _make_channel(gid, 7, "streak")
    _make_channel(gid, 8, "countdown")
    cid, cs = chromie.resolve_streak_channel(g, 999)  # 999 isn't a streak channel
    assert cid == 7  # falls back to the one streak channel


def test_resolve_streak_channel_ambiguous_returns_none():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    _make_channel(gid, 7, "streak")
    _make_channel(gid, 9, "streak")
    cid, cs = chromie.resolve_streak_channel(g, 999)
    assert cid is None and cs is None


def test_countdown_command_does_not_resolve_to_streak_channel():
    # A streak channel must never be picked by the countdown resolver, and vice versa.
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    _make_channel(gid, 7, "streak")
    cid, cs = chromie.resolve_event_channel(g, 999)  # countdown resolver
    assert cs is None  # the lone channel is a streak channel, so no countdown match


# ---------------------------------------------------------------------------
# Phase 5 — milestone engine (due calc, celebratory copy, the streak cycle)
# ---------------------------------------------------------------------------

def test_streak_milestones_due_basic():
    due = chromie.streak_milestones_due(30, chromie.DEFAULT_STREAK_MILESTONES, [])
    assert due == [1, 7, 30]


def test_streak_milestones_due_skips_announced():
    due = chromie.streak_milestones_due(30, chromie.DEFAULT_STREAK_MILESTONES, [1, 7])
    assert due == [30]


def test_streak_milestones_due_yearly_anniversaries():
    due_400 = chromie.streak_milestones_due(400, chromie.DEFAULT_STREAK_MILESTONES, [])
    assert 365 in due_400 and 730 not in due_400
    due_800 = chromie.streak_milestones_due(800, chromie.DEFAULT_STREAK_MILESTONES, [])
    assert 365 in due_800 and 730 in due_800  # two anniversaries passed


def test_streak_milestone_message_celebrates():
    msg = chromie.build_streak_milestone_message(event_name="Smoke-free", days=100, milestone=100)
    assert "Smoke-free" in msg and "100" in msg


def test_backdated_streak_seeds_yearly_anniversary():
    from datetime import timedelta
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%m/%d/%Y")
    gid, cid = fresh_gid(), 1
    (_m, _n), cs = _add_streak(gid, cid, date=start, name="Year+")
    announced = cs["events"][0]["announced_milestones"]
    assert 365 in announced  # passed 1-year anniversary is seeded -> won't replay live


def test_run_streak_cycle_fires_due_milestone():
    from datetime import timedelta

    sent = []

    class _FakeMsg:
        id = 12345

    class _FakeChannel:
        id = 1
        guild = None

        async def send(self, text, allowed_mentions=None):
            sent.append(text)
            return _FakeMsg()

    async def _fake_pin(channel, cs, gs, allow_create=False):
        return None

    _orig_pin = chromie.get_or_create_pinned_message_for_channel
    _orig_mention = chromie.build_milestone_mention
    try:
        chromie.get_or_create_pinned_message_for_channel = _fake_pin
        chromie.build_milestone_mention = lambda channel, gs: ("", None)

        gid, cid = fresh_gid(), 1
        start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%m/%d/%Y")
        (_m, _n), cs = _add_streak(gid, cid, date=start, name="Test")
        # Day 7 gets seeded on creation; un-seed it to simulate the milestone just arriving.
        cs["events"][0]["announced_milestones"] = [1]
        g = chromie.get_guild_state(gid)
        asyncio.run(chromie._run_streak_cycle(gid, cs, _FakeChannel(), None, g))
    finally:
        chromie.get_or_create_pinned_message_for_channel = _orig_pin
        chromie.build_milestone_mention = _orig_mention

    assert any("7" in s for s in sent)                  # the 7-day milestone fired
    assert 7 in cs["events"][0]["announced_milestones"]  # and was recorded


# ---------------------------------------------------------------------------
# Phase 6 — custom milestone ladder (Pro)
# ---------------------------------------------------------------------------

def test_parse_streak_milestones_valid():
    assert chromie.parse_streak_milestones("7, 30, 100") == [7, 30, 100]
    assert chromie.parse_streak_milestones("100 7 30") == [7, 30, 100]  # sorted, any separator
    assert chromie.parse_streak_milestones("30,30,7") == [7, 30]        # de-duped


def test_parse_streak_milestones_invalid():
    assert chromie.parse_streak_milestones("") is None
    assert chromie.parse_streak_milestones("abc") is None
    assert chromie.parse_streak_milestones("0 -5") is None  # no positive values


def test_custom_streak_ladder_used_by_new_streaks():
    gid, cid = fresh_gid(), 1
    g = chromie.get_guild_state(gid)
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    cs["default_streak_milestones"] = [5, 50]  # what a Pro /setstreakmilestones would store
    today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    asyncio.run(chromie.add_streak_core(
        _FakeGuild(), g, cs, cid,
        actor=_FakeUser(), member=_FakeMember(), date=today, name="Custom",
    ))
    assert cs["events"][0]["milestones"] == [5, 50]  # new streak adopts the custom ladder


def test_ordered_streaks_longest_first_and_excludes_countdowns():
    gid, cid = fresh_gid(), 1
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    cs["events"] = [
        {"name": "newer", "type": "streak", "timestamp": 2000},
        {"name": "older", "type": "streak", "timestamp": 1000},
        {"name": "a countdown", "type": "countdown", "timestamp": 1500},
    ]
    ordered = chromie._ordered_streaks(cs)
    assert [e["name"] for e in ordered] == ["older", "newer"]  # smallest ts (longest) first; countdown dropped


def test_remove_streak_event_drops_target_and_keeps_others():
    gid, cid = fresh_gid(), 1
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    cs["events"] = [
        {"name": "newer", "type": "streak", "timestamp": 2000},
        {"name": "older", "type": "streak", "timestamp": 1000},
        {"name": "a countdown", "type": "countdown", "timestamp": 1500},
    ]
    target = chromie._ordered_streaks(cs)[0]  # board #1 (longest-running) == "older"
    assert target["name"] == "older"
    assert chromie.remove_streak_event(cs, target) is True
    names = [e["name"] for e in cs["events"]]
    assert "older" not in names
    assert "newer" in names and "a countdown" in names  # the other streak + the countdown survive


def test_remove_streak_event_returns_false_when_not_present():
    gid, cid = fresh_gid(), 1
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    cs["events"] = [{"name": "only", "type": "streak", "timestamp": 1000}]
    stranger = {"name": "only", "type": "streak", "timestamp": 1000}  # value-equal, not in the list
    assert chromie.remove_streak_event(cs, stranger) is False  # identity miss -> nothing removed
    assert len(cs["events"]) == 1


def test_remove_streak_event_is_identity_safe_with_duplicates():
    # Two streaks identical by value: a naive cs["events"].remove(target) would delete
    # the FIRST value-equal match (the wrong one). The is-filter drops the picked object.
    gid, cid = fresh_gid(), 1
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    a = {"name": "dup", "type": "streak", "timestamp": 1000}
    b = {"name": "dup", "type": "streak", "timestamp": 1000}
    cs["events"] = [a, b]
    assert chromie.remove_streak_event(cs, b) is True
    assert cs["events"] == [a] and cs["events"][0] is a


def test_build_streak_removed_message_names_streak_and_pluralizes_days():
    msg = chromie.build_streak_removed_message("Smoke-free", 42)
    assert "Smoke-free" in msg and "42 days" in msg
    one = chromie.build_streak_removed_message("Sober", 1)
    assert "1 day" in one and "1 days" not in one  # singular at one day


def test_build_event_removed_message_names_event():
    assert "Launch" in chromie.build_event_removed_message("Launch")


def test_streak_reset_message_is_supportive_and_named():
    msg = chromie.build_streak_reset_message("Smoke-free")
    assert "Smoke-free" in msg and "Day 0" in msg


# ---------------------------------------------------------------------------
# Streak templates (curated catalog, Pro-gated except the free "sober")
# ---------------------------------------------------------------------------

def test_streak_catalog_structure_and_free_split():
    cat = chromie.STREAK_TEMPLATES
    assert len(cat) >= 12
    free = [k for k, v in cat.items() if not v["pro"]]
    assert free == ["sober"]  # exactly Sober is gifted to Free; the rest are Pro
    for tid, t in cat.items():
        for key in ("label", "emoji", "category", "default_name", "blurb", "milestones", "copy"):
            assert t.get(key) not in (None, ""), (tid, key)
        ml = t["milestones"]
        assert ml == sorted(set(ml)) and all(isinstance(m, int) and m > 0 for m in ml), tid
        for day, line in t["copy"].items():
            assert isinstance(day, int)
            assert "{name}" in line          # every line is personalized
            line.format(name="X")            # and has no stray braces


def test_streak_template_locked_rules():
    free_guild = chromie.get_guild_state(fresh_gid())
    pro_guild = chromie.get_guild_state(fresh_gid())
    pro_guild["pro"] = {"discord_subscription": True}
    assert chromie.streak_template_locked("sober", free_guild) is False   # gifted to Free
    assert chromie.streak_template_locked("gym", free_guild) is True       # Pro on a Free guild
    assert chromie.streak_template_locked("gym", pro_guild) is False       # unlocked on Pro
    assert chromie.streak_template_locked("nope", free_guild) is False     # unknown id never locks


def test_milestone_message_uses_template_copy_then_falls_back():
    # marquee day -> bespoke template line
    msg = chromie.build_streak_milestone_message(event_name="Q", days=1, milestone=1, template_id="sober")
    assert "Day 1" in msg and "Q" in msg
    # a day NOT in the template copy -> generic fallback still celebrates (and names the streak)
    fb = chromie.build_streak_milestone_message(event_name="Q", days=60, milestone=60, template_id="sober")
    assert "Q" in fb and "60" in fb
    # no template -> original generic behavior, unchanged
    gen = chromie.build_streak_milestone_message(event_name="Q", days=7, milestone=7)
    assert "Q" in gen and "7" in gen


def _add_streak_tmpl(gid, cid, *, date, template, name=None, pro=False):
    g = chromie.get_guild_state(gid)
    if pro:
        g["pro"] = {"discord_subscription": True}
    cs = chromie.get_channel_state(gid, cid)
    cs["kind"] = "streak"
    res = asyncio.run(chromie.add_streak_core(
        _FakeGuild(), g, cs, cid,
        actor=_FakeUser(), member=_FakeMember(), date=date, name=name, template=template,
    ))
    return res, cs


def test_add_streak_free_template_seeds_id_name_and_ladder():
    gid, cid = fresh_gid(), 1
    (msg, _), cs = _add_streak_tmpl(gid, cid, date="01/01/2024", template="sober")  # no name given
    assert "Streak" in msg
    ev = cs["events"][0]
    assert ev["template"] == "sober"
    assert ev["name"] == "Sober"                                          # template's default name
    assert ev["milestones"] == chromie.STREAK_TEMPLATES["sober"]["milestones"]


def test_add_streak_custom_name_overrides_template_default():
    gid, cid = fresh_gid(), 1
    (_msg, _), cs = _add_streak_tmpl(gid, cid, date="01/01/2024", template="sober", name="My Journey")
    assert cs["events"][0]["name"] == "My Journey"
    assert cs["events"][0]["template"] == "sober"


def test_add_streak_pro_template_gated_on_free_guild():
    gid, cid = fresh_gid(), 1
    (msg, _), cs = _add_streak_tmpl(gid, cid, date="01/01/2024", template="gym")  # Free guild
    assert "Pro" in msg
    assert cs["events"] == []                                              # nothing created


def test_add_streak_pro_template_works_on_pro_guild():
    gid, cid = fresh_gid(), 1
    (msg, _), cs = _add_streak_tmpl(gid, cid, date="01/01/2024", template="gym", pro=True)
    assert "Streak" in msg
    assert cs["events"][0]["template"] == "gym"
    assert cs["events"][0]["milestones"] == chromie.STREAK_TEMPLATES["gym"]["milestones"]


def test_add_streak_unknown_template_rejected():
    gid, cid = fresh_gid(), 1
    (msg, _), cs = _add_streak_tmpl(gid, cid, date="01/01/2024", template="bogus", name="X")
    assert "template" in msg.lower()
    assert cs["events"] == []


def test_plain_addstreak_still_works_without_template():
    # Regression: the template param is optional; the original name-only path is unchanged.
    gid, cid = fresh_gid(), 1
    (msg, _), cs = _add_streak_tmpl(gid, cid, date="01/01/2024", template=None, name="Custom thing")
    assert "Streak" in msg
    assert cs["events"][0]["name"] == "Custom thing"
    assert cs["events"][0]["template"] is None


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
