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
- `resolve_event_channel(guild_state, channel_id)` — picks the channel a command acts
  on (current channel → else the single channel → else None); `no_channel_guidance(...)`
  renders the 0/multiple-channel nudge.

## ✅ COMMAND SWEEP — DONE (committed on the branch)
- `906f744` Step 1 — **Setup/CRUD** flipped per-channel: `/seteventchannel` (gating +
  orphan adoption + builds pin), `/addevent`, `/listevents`, `/nextevent`,
  `/eventinfo`*, `/removeevent`*, `/editevent`*, `/remindall`, autocomplete.
  (* later folded into `/event` hub — see Step 6.) +`tests/test_resolve_channel.py`.
- `e72e7fa` Step 2 — **`/countdown` hub** (Select + Modal/Buttons): theme, timezone,
  time format, mention role, auto-delete, title/desc (Pro), digest (Pro), remove
  channel. Removed old `digest` + `countdown` groups. +`tests/test_countdown_hub.py`.
- `bcf44db` Step 3 — **`/event` hub** (pick event → actions) + **per-event owner-DM
  opt-in (default OFF)**; extracted `add_event_core()` shared by `/addevent` + hub.
  +`tests/test_event_hub.py`.
- Step 6 (this commit) — **retired ~20 granular commands** (theme/timezone_*/timeformat/
  mentionrole/resetchannel/eventinfo/editevent/removeevent/setmilestones/silence/
  owner/banner/repeat/dupe/reminder-time + milestones & banner groups), folded
  auto-delete into `/countdown`, flipped the remaining utilities to per-channel
  (template save/load, archivepast, healthcheck, purgeevents, update_countdown,
  owner_unlock), removed the 4 legacy wrappers, refreshed help/onboarding copy.
  Dropped the redundant `@require_vote` on `/template save|load`.

Command surface is now ~22 (was ~50): hubs `/countdown` + `/event`; fast top-level
`/addevent /listevents /nextevent /remindall /chronohelp /vote /pro_status
/seteventchannel`; `/template save|load`; admin/maintenance the rest.

## ▶️ REMAINING
- **Themes revamp** (the "LAST phase"): preview-before-apply, seasonal/limited Pro
  themes, new themes (Birthday/Baby, Wedding, Game Launch, School/Exam), Pro
  build-your-own. The `/countdown` → Theme Select is the natural surface.
- **Go-live** (see procedure below) once themes are in or deferred.

## Tests & local dev
```bash
pip install "discord.py>=2.0,<3.0" "pytz>=2024.1"   # one-time
python tests/test_migrate_per_channel.py            # migration (10)
python tests/test_accessors.py                      # accessors/gating (8)
python tests/test_engine.py                         # startup migration + loop (2)
python tests/test_resolve_channel.py                # channel resolver (10)
python tests/test_countdown_hub.py                  # /countdown hub (7)
python tests/test_event_hub.py                      # /event hub + add_event_core (7)
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
