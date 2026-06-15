"""Smoke tests for the /countdown hub UI in chromie.py.

discord.ui components can be constructed without a live client, so we can verify
the hub's views/selects/modals + the settings embed wire up correctly and that
the Pro/Supporter theme-locking logic is right — without a Discord connection.

Run from repo root:  python tests/test_countdown_hub.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_test_state.json")

import chromie  # noqa: E402

_gid = iter(range(700000, 799999))


def fresh_gid():
    return next(_gid)


# ---- settings embed ----

def test_settings_embed_shows_current_values():
    gid, cid = fresh_gid(), 4242
    cs = chromie.get_channel_state(gid, cid)
    cs["theme"] = "football"
    cs["timezone"] = "US/Eastern"
    cs["time_unit"] = "weeks"
    cs["mention_role_id"] = 555
    g = chromie.get_guild_state(gid)
    e = chromie.build_countdown_settings_embed(cs, g, cid)
    blob = (e.title or "") + (e.description or "") + " ".join(f.name + str(f.value) for f in e.fields)
    assert "Football" in blob
    assert "US/Eastern" in blob
    assert "weeks" in blob.lower()
    assert "555" in blob  # role mention rendered
    assert str(cid) in blob


def test_settings_embed_marks_pro_fields_locked_for_free():
    gid, cid = fresh_gid(), 1
    cs = chromie.get_channel_state(gid, cid)
    g = chromie.get_guild_state(gid)  # free
    e = chromie.build_countdown_settings_embed(cs, g, cid)
    names = " ".join(f.name for f in e.fields)
    assert "🔒" in names  # title/description/digest carry the lock for free servers


# ---- hub views / selects ----

def test_hub_view_has_setting_select_with_all_options():
    view = chromie.CountdownHubView(fresh_gid(), 1)
    selects = [c for c in view.children if isinstance(c, chromie.CountdownSettingSelect)]
    assert len(selects) == 1
    values = {o.value for o in selects[0].options}
    assert values == {"theme", "timezone", "timeformat", "role", "title", "description", "digest", "remove"}


def test_time_format_select_matches_unit_labels():
    sel = chromie.CountdownTimeFormatSelect()
    assert {o.value for o in sel.options} == set(chromie._TIME_UNIT_LABELS.keys())


def test_theme_select_locks_supporter_themes_for_free():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)  # free, no vote
    sel = chromie.CountdownThemeSelect(g)
    by_val = {o.value: o for o in sel.options}
    # classic is free -> no lock; a supporter theme -> locked description
    assert by_val["classic"].description is None
    assert "🔒" in (by_val["football"].description or "")


def test_theme_select_unlocked_for_pro():
    gid = fresh_gid()
    g = chromie.get_guild_state(gid)
    g["pro"] = {"discord_subscription": True}  # is_pro -> True
    assert chromie.is_pro(g) is True
    sel = chromie.CountdownThemeSelect(g)
    by_val = {o.value: o for o in sel.options}
    assert by_val["football"].description is None  # no lock for Pro


def test_sub_and_modal_views_construct():
    gid, cid = fresh_gid(), 9
    chromie.get_channel_state(gid, cid)
    # views
    chromie.CountdownRoleView(gid, cid)
    chromie.CountdownDigestView(gid, cid)
    chromie.CountdownRemoveView(gid, cid)
    chromie.CountdownSubView(gid, cid, chromie.CountdownTimeFormatSelect())
    # modals (parent interaction not needed for construction)
    chromie.CountdownTimezoneModal(gid, cid, None)
    chromie.CountdownTextModal(gid, cid, None, field="countdown_title_override",
                               modal_title="t", label="l", max_len=256,
                               style=chromie.discord.TextStyle.short)


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
