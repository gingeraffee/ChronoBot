"""Validation for the theme dicts in chromie.py.

A theme is coherent only if it exists in all three dicts (THEMES for message
pools + gating, THEME_LAYOUTS for the embed, _THEME_LABELS for the picker) and
every template string formats cleanly. This guards the hand-written content for
the new themes (birthday/wedding/gamelaunch/exam) and the existing ones.

Run from repo root:  python tests/test_themes.py
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ["CHROMIE_DATA_PATH"] = os.path.join(tempfile.gettempdir(), "chromie_test_themes_state.json")

import chromie  # noqa: E402

NEW = ("birthday", "wedding", "gamelaunch", "exam")
POOL_KEYS = ("pin_title_pool", "event_emoji_pool", "milestone_emoji_pool",
             "repeat_templates", "remindall_templates", "start_blast_templates")
MILESTONE_SUBKEYS = ("default", "one_day", "zero_day")
FMT = dict(emoji="⏰", event="Test Event", days=5, time_left="5 days", date="January 1, 2026")


def test_three_dicts_cover_the_same_themes():
    themes = set(chromie.THEMES)
    layouts = set(chromie.THEME_LAYOUTS)
    labels = set(chromie._THEME_LABELS)
    assert themes == layouts == labels, (
        f"mismatch: only in THEMES={themes - layouts - labels}, "
        f"only in LAYOUTS={layouts - themes}, only in LABELS={labels - themes}"
    )


def test_new_themes_present_and_supporter_only():
    for k in NEW:
        assert k in chromie.THEMES and k in chromie.THEME_LAYOUTS and k in chromie._THEME_LABELS
        assert chromie.THEMES[k].get("supporter_only") is True


def test_every_theme_has_required_pools_nonempty():
    for tid, t in chromie.THEMES.items():
        for key in POOL_KEYS:
            assert isinstance(t.get(key), list) and t[key], f"{tid}.{key} missing/empty"
        mt = t.get("milestone_templates")
        assert isinstance(mt, dict), f"{tid}.milestone_templates not a dict"
        for sub in MILESTONE_SUBKEYS:
            assert isinstance(mt.get(sub), list) and mt[sub], f"{tid}.milestone_templates.{sub} missing/empty"
        assert "color" in t and "label" in t


def test_every_layout_has_required_keys():
    for tid, lay in chromie.THEME_LAYOUTS.items():
        for key in ("title", "subtitle", "footer", "color", "emoji"):
            assert key in lay, f"{tid} layout missing {key}"


def test_every_template_formats_cleanly():
    # Deterministically format EVERY string in EVERY pool — a stray/typo'd
    # placeholder would raise KeyError/IndexError here.
    for tid, t in chromie.THEMES.items():
        strings = []
        strings += t["pin_title_pool"]
        strings += t["repeat_templates"] + t["remindall_templates"] + t["start_blast_templates"]
        for sub in MILESTONE_SUBKEYS:
            strings += t["milestone_templates"][sub]
        for s in strings:
            try:
                s.format(**FMT)
            except (KeyError, IndexError, ValueError) as e:
                raise AssertionError(f"{tid}: template failed to format ({type(e).__name__}: {e}): {s!r}")


def test_message_builders_and_embed_work_for_new_themes():
    for tid in NEW:
        gs = {"theme": tid}
        assert chromie.build_milestone_message(gs, event_name="E", days_left=3, time_left="3 days", date_str="Jan 1")
        assert chromie.build_repeat_message(gs, event_name="E", time_left="3 days", date_str="Jan 1")
        assert chromie.build_remindall_message(gs, event_name="E", time_left="3 days", date_str="Jan 1")
        assert chromie.build_start_blast_message(gs, event_name="E")
        # embed renders via the layout path
        cs = {"theme": tid, "events": [], "timezone": "UTC", "time_unit": "discord"}
        embed = chromie.build_embed_for_channel(cs, {})
        assert embed.title and embed.description


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
