"""Tests for the departed-server state prune (_departed_guild_ids / _prune_departed)."""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("CHROMIE_DATA_PATH", os.path.join(tempfile.gettempdir(), "chromie_prune_test.json"))

import chromie  # noqa: E402


def _sample():
    return {
        "guilds": {
            "1": {"channels": {}},      # active
            "2": {"channels": {}},      # departed
            "3": {"channels": {}},      # active
            "bogus": {"channels": {}},  # malformed key -> prune
        },
        "user_links": {
            "u_active": 1,     # points at kept guild
            "u_departed": 2,   # points at pruned guild
        },
    }


def test_departed_ids_identifies_left_and_malformed():
    departed = set(chromie._departed_guild_ids(_sample(), {1, 3}))
    assert departed == {"2", "bogus"}, departed


def test_prune_removes_departed_and_their_links():
    data = _sample()
    result = chromie._prune_departed(data, {1, 3})
    assert set(data["guilds"]) == {"1", "3"}, data["guilds"].keys()
    assert result["removed_guilds"] == 2          # "2" and "bogus"
    assert result["kept_guilds"] == 2
    # the user link pointing at the departed guild is gone; the active one stays
    assert "u_departed" not in data["user_links"]
    assert "u_active" in data["user_links"]
    assert result["removed_links"] == 1


def test_prune_keeps_everything_when_all_current():
    data = _sample()
    # everything (except the malformed key) is "current"
    result = chromie._prune_departed(data, {1, 2, 3})
    assert set(data["guilds"]) == {"1", "2", "3"}   # only "bogus" removed
    assert result["removed_guilds"] == 1


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
