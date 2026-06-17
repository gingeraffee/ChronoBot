"""Tests for the /event hub in chromie.py.

Covers the shared add_event_core() logic (tier limits, parsing, dm_opt_in default)
and smoke-constructs the hub's views/selects/modals — no Discord connection.

Run from repo root:  python tests/test_event_hub.py
"""

import os
import sys
import asyncio
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Dedicated, freshly-cleared state file: add_event_core() calls save_state(), and
# this suite asserts exact event counts, so it must not inherit state another
# suite (or a prior run) persisted to the shared temp file.
_DATA_PATH = os.path.join(tempfile.gettempdir(), "chromie_test_event_hub_state.json")
try:
    os.remove(_DATA_PATH)
except OSError:
    pass
os.environ["CHROMIE_DATA_PATH"] = _DATA_PATH

import chromie  # noqa: E402

# Force a deterministic, network-free "not voted" result.
chromie.TOPGG_TOKEN = ""
chromie.TOPGG_FAIL_OPEN = False


async def _no_channel(_cid):
    return None


chromie.get_text_channel = _no_channel  # never touch Discord in tests

_gid = iter(range(600000, 699999))


def fresh_gid():
    return next(_gid)


class FakeUser:
    def __init__(self, uid=11, name="tester"):
        self.id = uid
        self.name = name

    def __str__(self):
        return self.name


class FakeMember:
    def __init__(self, dn="Tester"):
        self.display_name = dn


class FakeGuild:
    name = "Test Guild"


def _add(gid, cid, *, date, time="09:00", name="Party"):
    g = chromie.get_guild_state(gid)
    cs = chromie.get_channel_state(gid, cid)
    return asyncio.run(chromie.add_event_core(
        FakeGuild(), g, cs, cid,
        actor=FakeUser(), member=FakeMember(), date=date, time=time, name=name,
    ))


# ---- add_event_core ----

def test_add_event_core_appends_and_defaults_dm_off():
    gid, cid = fresh_gid(), 1
    cs = chromie.get_channel_state(gid, cid)
    msg, nudge = _add(gid, cid, date="12/31/2099", name="NYE")
    assert "Added event" in msg
    assert len(cs["events"]) == 1
    ev = cs["events"][0]
    assert ev["name"] == "NYE"
    assert ev["dm_opt_in"] is False  # owner DMs opt-in default off


def test_add_event_core_free_limit_is_one():
    # Free tier is 1 event/channel (Supporter=3, Pro=∞ live in test_event_limits.py).
    gid, cid = fresh_gid(), 1
    msg, _ = _add(gid, cid, date="12/01/2099")
    assert "Added event" in msg
    msg, _ = _add(gid, cid, date="12/02/2099")
    assert "limit reached" in msg.lower()
    assert len(chromie.get_channel_state(gid, cid)["events"]) == 1


def test_add_event_core_rejects_past_and_bad_dates():
    gid, cid = fresh_gid(), 1
    past, _ = _add(gid, cid, date="01/01/2000")
    assert "past" in past.lower()
    bad, _ = _add(gid, cid, date="not-a-date")
    assert "date/time" in bad.lower()
    assert chromie.get_channel_state(gid, cid)["events"] == []


# ---- embeds ----

def test_event_detail_embed_renders():
    gid, cid = fresh_gid(), 1
    _add(gid, cid, date="12/31/2099", name="Launch")
    cs = chromie.get_channel_state(gid, cid)
    g = chromie.get_guild_state(gid)
    e = chromie.build_event_detail_embed(cs, g, cs["events"][0])
    blob = (e.title or "") + " ".join(f.name + str(f.value) for f in e.fields)
    assert "Launch" in blob
    assert "Owner DMs" in blob


# ---- view / select / modal construction ----

def test_event_hub_view_has_list_and_add():
    gid, cid = fresh_gid(), 1
    _add(gid, cid, date="12/31/2099", name="A")
    view = chromie.EventHubView(gid, cid)
    kinds = [type(c).__name__ for c in view.children]
    assert "EventListSelect" in kinds
    assert "EventAddButton" in kinds


def test_event_action_select_has_all_actions():
    sel = chromie.EventActionSelect()
    values = {o.value for o in sel.options}
    assert values == {"edit", "milestones", "reminder", "owner", "dmtoggle",
                      "silence", "banner", "repeat", "dupe", "delete"}


def test_event_detail_and_subviews_construct():
    gid, cid = fresh_gid(), 1
    _add(gid, cid, date="12/31/2099", name="A")
    ev = chromie.get_channel_state(gid, cid)["events"][0]
    chromie.EventDetailView(gid, cid, ev)
    chromie.EventMilestonesView(gid, cid, ev)
    chromie.EventReminderView(gid, cid, ev)
    chromie.EventOwnerView(gid, cid, ev)
    chromie.EventBannerView(gid, cid, ev)
    chromie.EventRepeatView(gid, cid, ev)
    chromie.EventDeleteView(gid, cid, ev)
    # modals
    chromie.EventEditModal(gid, cid, ev, None)
    chromie.EventMilestonesModal(gid, cid, ev, None)
    chromie.EventReminderModal(gid, cid, ev, None)
    chromie.EventBannerModal(gid, cid, ev, None)
    chromie.EventRepeatModal(gid, cid, ev, None)
    chromie.EventDupeModal(gid, cid, ev, None)
    chromie.EventAddModal(gid, cid, None)


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
