# ChronoBot — Per-Channel Countdowns: Session Handoff

> **To resume:** open a new session and say **"continue the command sweep"**.
> Branch: `feature/per-channel-countdowns`. This file + the Claude memory files
> hold the full plan. Delete this file before merging to `main`.

## ⚠️ Live system — read first
- ChronoBot ("Chromie") is **live on 600+ Discord servers**, hosted on **Render**.
- Production state: `/var/data/chromie_state.json` on a Render **persistent disk**
  (env `CHROMIE_DATA_PATH`; access via Render dashboard → service → **Shell**).
- **Never work on `main`.** Stay on `feature/per-channel-countdowns`.
- **Do not pull real prod data to this machine** (too big to copy-paste reliably;
  also minimizes moving 600 servers' PII). We validate on Render at go-live.

## The feature
Turn ChronoBot from **one countdown per server** into **one countdown per channel**.
Monetization: **1 countdown channel is free** (same as today); **additional channels
require ChronoBot Plus** ($2.99/mo, the existing `is_pro` gate).

### Locked decisions
- Free tier: **1 channel**; Plus = unlimited. Each channel fully independent settings.
- **Digest → per-channel.** **Templates → server-wide** shared library.
- Billing (`pro`/`supporter`), `templates`, `welcomed` stay **guild-level**.
- Owner DMs → change to a **per-event opt-in toggle, default OFF** (build in `/event` hub).
- Build approach: **build the new `/event` + `/countdown` hubs directly** on the
  per-channel model and retire old commands — don't flip-then-delete the old 50.
- Themes revamp (preview, seasonal/new themes, Pro build-your-own) = LAST phase.

## Data model
```
state["guilds"][gid] = {
  "_schema_version": 2,
  "channels": { "<channel_id>": { per-channel bucket } },   # see _default_channel_state()
  # server-level (unchanged): "pro", "supporter", "templates", "welcomed"
}
```
Per-channel bucket fields: pinned_message_id, mention_role_id, events, theme,
timezone, time_unit, digest, default_milestones, auto_delete_milestones,
countdown_title_override, countdown_description_override, audit fields.
Orphan events (no channel) are preserved under the sentinel key `"unassigned"`.

## ✅ DONE (committed on the branch)
- `3ca86c9` Foundation: `migrate_per_channel.py` (idempotent, lossless, auto-backup
  migration) + additive accessor layer in `chromie.py`.
- `5f0cf2b` Per-channel **engine**: `build_embed_for_channel`,
  `rebuild_pinned_message_for_channel`, `get_or_create_pinned_message_for_channel`
  (each old fn kept as a **legacy wrapper**), and `update_countdowns` now iterates
  channels via `iter_channel_states` → `_run_countdown_cycle(...)`. Migration runs
  at startup (idempotent, error-wrapped).
- `94f5493` Per-channel **weekly digest**.

### Accessor helpers (in chromie.py)
- `get_channel_state(guild_id, channel_id)` — get/create a channel bucket (backfills).
- `iter_channel_states(guild_state)` — yields (cid:int, bucket); skips `unassigned`.
- `count_countdown_channels(guild_state)`; `can_add_countdown_channel(guild_state, cid)`
  (1 free via `FREE_CHANNEL_LIMIT`, Plus unlimited; existing channel always allowed).

## ▶️ NEXT: the command sweep (do as one coherent batch)
The ~50 commands still read guild-level fields, which are empty after migration —
so they must be flipped together to avoid a broken half-state.
1. **Setup/CRUD first** (gives working end-to-end): `/seteventchannel` (add gating via
   `can_add_countdown_channel`, build pin via `rebuild_pinned_message_for_channel`),
   `/addevent`, `/listevents`, `/removeevent`, `/editevent`. Pattern per command:
   resolve `cs = get_channel_state(guild_id, interaction.channel_id)` and use `cs`
   where the old code used `guild_state` for events/theme/etc.
2. **`/countdown` hub** (channel settings: theme, timezone, time format, mention role,
   title/desc overrides, digest) — Select + Modal, modeled on existing `HelpView`.
3. **`/event` hub** (Select an event → modal/buttons for all fields) incl. the
   **per-event DM opt-in toggle (default off)**.
4. Retire/redirect remaining old commands; remove now-unused legacy wrappers
   (`build_embed_for_guild`, `rebuild_pinned_message`, `get_or_create_pinned_message`,
   `refresh_countdown_message`) once nothing calls them.
5. Server-level commands stay on `get_guild_state`: `/vote`, `/pro_status`,
   `/sync_subscription`, `/template save|load` (shared), supporter stuff.
6. Themes revamp last.

### Cleanup noticed (do during sweep)
- `/template save|load` and `/digest enable` carry BOTH `@require_pro` and
  `@require_vote` — vote decorator is redundant (Pro auto-passes it). Remove it.
- `/banner clear` and `/milestones autodelete` require a vote even to turn the
  feature OFF — make the off/clear direction free.

## Tests & local dev
```bash
pip install "discord.py>=2.0,<3.0" "pytz>=2024.1"   # one-time
python tests/test_migrate_per_channel.py            # migration (10)
python tests/test_accessors.py                      # accessors/gating (8)
python tests/test_engine.py                         # startup migration + loop (2)
python -m py_compile chromie.py                     # syntax check
```
Tests import `chromie` with a **temp `CHROMIE_DATA_PATH`** so they never touch prod.
`chromie.py` imports `migrate_per_channel` at startup → **deploy BOTH files** to Render.

## Go-live procedure (when feature is complete)
1. Finish + test all command/hub work on the branch.
2. On Render Shell: run the migration once, manually:
   `python migrate_per_channel.py /var/data/chromie_state.json`
   (writes a timestamped `.bak` first; idempotent).
3. Verify the migrated state looks right.
4. Deploy the new `chromie.py` + `migrate_per_channel.py` together.
   (Startup also runs the migration defensively — harmless no-op if already done.)
