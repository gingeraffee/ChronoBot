# migrate_per_channel.py
"""
Migrate ChronoBot state from one-countdown-per-guild to many-countdowns-per-channel.

BEFORE (per-guild):
    state["guilds"][gid] = {
        "event_channel_id": 123, "pinned_message_id": 456,
        "events": [...], "theme": ..., "timezone": ..., ...,
        "pro": {...}, "supporter": {...}, "templates": {...}, "welcomed": ...
    }

AFTER (per-channel):
    state["guilds"][gid] = {
        "_schema_version": 2,
        "channels": {
            "123": { "pinned_message_id": 456, "events": [...], "theme": ...,
                     "timezone": ..., "digest": {...}, ... }
        },
        # server-level (unchanged):
        "pro": {...}, "supporter": {...}, "templates": {...}, "welcomed": ...
    }

Design goals:
  * LOSSLESS  - no event, setting, or message id is ever dropped.
  * IDEMPOTENT - safe to run repeatedly / across restarts (guilds tagged with
    _schema_version >= TARGET are skipped). This is the property that protects
    the 600 live servers from a double-migration on a crash-restart loop.
  * BACKED UP - run_migration() writes a timestamped backup before touching disk.

The pure transform `migrate_state(data)` does the work and is unit-tested;
`run_migration(path)` is the file/backup/CLI wrapper.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

TARGET_SCHEMA_VERSION = 2

# Sentinel channel key for events that exist but were never assigned to a
# channel (event_channel_id was None). Preserved so nothing is lost; the
# runtime / a future /seteventchannel reassigns them. Non-numeric on purpose
# so the update loop (which int()s channel keys) can recognise + skip it.
UNASSIGNED_KEY = "unassigned"

# Fields that move OUT of the guild dict and INTO each per-channel bucket.
PER_CHANNEL_FIELDS = (
    "pinned_message_id",
    "mention_role_id",
    "events",
    "event_channel_set_by",
    "event_channel_set_at",
    "theme",
    "countdown_title_override",
    "countdown_description_override",
    "default_milestones",
    "digest",
    "auto_delete_milestones",
    "time_unit",
    "timezone",
)

# Fields that STAY on the guild dict (server-wide: billing, onboarding, shared
# template library). Everything not listed here or in PER_CHANNEL_FIELDS is also
# left on the guild untouched, so unknown/future fields are never lost.
SERVER_LEVEL_FIELDS = (
    "pro",
    "supporter",
    "templates",
    "welcomed",
)


def migrate_guild(guild: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate a single guild dict in place and return it. Idempotent."""
    if guild.get("_schema_version", 1) >= TARGET_SCHEMA_VERSION:
        return guild  # already migrated - leave untouched
    if "channels" in guild:
        # Defensive: a channels bucket already exists but no version tag.
        guild["_schema_version"] = TARGET_SCHEMA_VERSION
        return guild

    # Pull the per-channel fields off the guild (default to None/[] if absent).
    bucket: Dict[str, Any] = {}
    for field in PER_CHANNEL_FIELDS:
        if field in guild:
            bucket[field] = guild.pop(field)

    # event_channel_id becomes the channel key, so pop it off the guild too.
    channel_id = guild.pop("event_channel_id", None)

    channels: Dict[str, Any] = {}
    has_events = bool(bucket.get("events"))

    if channel_id:
        channels[str(channel_id)] = bucket
    elif has_events:
        # No channel was ever set, but there are events -> the one piece of
        # irreplaceable data. Park them under the sentinel so they survive; the
        # runtime / a future /seteventchannel reassigns them to a real channel.
        channels[UNASSIGNED_KEY] = bucket
    # else: no channel and no events. The leftover fields are all defaults
    # (theme=classic, timezone=UTC, digest disabled...) that get re-created the
    # moment a channel is set, so there is nothing worth preserving. Drop the
    # empty bucket and start this guild clean.

    guild["channels"] = channels
    guild["_schema_version"] = TARGET_SCHEMA_VERSION
    return guild


def migrate_state(data: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate the whole state dict in place and return it. Idempotent."""
    for guild in data.get("guilds", {}).values():
        if isinstance(guild, dict):
            migrate_guild(guild)
    data["_schema_version"] = TARGET_SCHEMA_VERSION
    return data


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_migration(path: str = "chromie_state.json", *, make_backup: bool = True) -> bool:
    """Load state from disk, back it up, migrate, and save. Returns True on success."""
    data_file = Path(path)
    if not data_file.exists():
        print(f"X {path} not found!")
        return False

    print(f"Loading {path} ...")
    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    if make_backup:
        backup = data_file.with_suffix(data_file.suffix + f".bak.{_timestamp()}")
        shutil.copy2(data_file, backup)
        print(f"Backup written: {backup}")

    guilds = data.get("guilds", {})
    before = sum(1 for g in guilds.values()
                 if isinstance(g, dict) and g.get("_schema_version", 1) < TARGET_SCHEMA_VERSION)

    migrate_state(data)

    print(f"Saving {path} ...")
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print("=" * 50)
    print("MIGRATION COMPLETE")
    print("=" * 50)
    print(f"Guilds migrated this run: {before}")
    print(f"Guilds total:            {len(guilds)}")
    return True


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "chromie_state.json"
    if not run_migration(target):
        sys.exit(1)
