# Design: instant coaching (rules, events, presence, extra state)

Why: a remote coach that polls the feed every few minutes reacts late. Move instant reactions into the addon, push events
out immediately, and expose state the coach could not see. This document is the contract between the addon, the bridge
(the Mac helper that reads the screen strip), the server and the docs. Read it fully before changing anything.

## What the Forever client allows (decisions already made)

* An addon has **no network access**. "The addon POSTs a webhook" is implemented as: addon emits structured events over the
  existing pixel strip, the **bridge** forwards them immediately (debounced) to a configurable webhook, and the server
  serves them as `GET .../events?since=`.
* Combat log events are restricted, so fights are reconstructed from `UNIT_SPELLCAST_*`, `UNIT_COMBAT` (amounts are plain),
  `PLAYER_REGEN_*`, nameplate units and XP-kill chat messages. Exact killer is not available: best effort from hostile
  units that were targeting the player.
* Health/power are secret in combat; "at pull" values are the last out-of-combat sample.
* Level and XP are **never shown** on the overlay (they may appear in feed data for the coach's own logic).
* No gameplay automation, no auto-equip, no preview-equip of items (BoE items can bind on Forever). Display and advice only.
* Everything uses `pcall` and feature checks; an API missing in Forever must degrade to "unknown", never raise.

## Pipeline

```
addon (Coach.lua)  --pixel strip msg "evj" (JSON array of events)-->  bridge  --POST /ui/<s>/events/push-->  server (ring buffer, seq)
                                                                       |--> webhook POST (batched, debounced 20 s, HMAC optional)
agent  --GET /ui/<s>/events?since=<eid>  or  GET /<read-token>/events?since=<eid>-->  {events:[...], next:<eid>}
overlay ops (rules, coach messages, waypoints) --> doc --> addon applies and evaluates rules locally
```

### Event envelope (addon -> bridge)

`{"seq":12,"t":1794300000,"type":"death","data":{...}}`. `seq` is monotonic per addon load; `t` is `time()`.
The addon sends events in a strip message of type `evj` (same framing as sections:
`evj\31<id>\31<part>\31<parts>\31<json array>`), batching what it has each tick, at most one message per tick
(payload size limit: `MAXPAY`). Events that do not fit stay queued (queue capped at 200, oldest dropped).
The bridge assigns the global `eid` (server side) and attaches `boot` (the addon's `uiboot`) and `received_at`.

### Event types and `data`

| type | when | data |
| --- | --- | --- |
| `login` | PLAYER_ENTERING_WORLD after login | `char, realm, map, zone, subzone, bind` |
| `logout` | PLAYER_LOGOUT / PLAYER_LEAVING_WORLD (best effort, may not reach the strip) | `{}` |
| `level_up` | PLAYER_LEVEL_UP | `level` |
| `death` | PLAYER_DEAD | `killer{name,level,classification,elite}, attackers, attackers_list[{name,level,classification}], hp_at_pull, hpmax, mana_at_pull, manamax, map, x, y, zone, subzone, deaths_in_subzone_10m` |
| `death_released` | PLAYER_ALIVE / corpse release | `durability_pct` |
| `quest_complete` | quest becomes ready to turn in (QUEST_LOG_UPDATE diff) | `quest, title` |
| `quest_turned_in` | QUEST_TURNED_IN | `quest, title` |
| `spell_learned` | LEARNED_SPELL_IN_TAB | `spell, name, rank` |
| `item_equipped` | PLAYER_EQUIPMENT_CHANGED | `slot, link, name, quality, bind, soulbound` |
| `item_looted` | CHAT_MSG_LOOT (own loot), quality >= 2 | `link, name, quality, count, bind, equip_slot, armor_type, price, boe_keep: true for BoE green+` |
| `zone_changed` | ZONE_CHANGED* | `map, zone, subzone` |
| `window_open` | MERCHANT_SHOW, TRAINER_SHOW, QUEST_COMPLETE (reward choice), LOOT_OPENED | `window: merchant|trainer|quest_reward|loot, npc{name,id,map,x,y}, choices[]` (see below) |
| `bags_full` | BAG_UPDATE, free slots <= 2 (edge triggered) | `free` |
| `durability_critical` | UPDATE_INVENTORY_DURABILITY, any slot <= 20% (edge triggered) | `pct, slot` |
| `rule_fired` | a rule executed | `rule, actions[], ctx{}` |
| `coach_question` | player typed `/coach <text>` | `id, text` |
| `ack` | toast / coach line / chat line was actually shown | `kind: toast|coach|chat, id, shown_at` |
| `fight` | PLAYER_REGEN_ENABLED after a fight | `start, end, duration, result: won|died|fled, casts[{t,spell,name,rank,target,wasted}], duplicate_dots[{spell,name,target,at}], damage_taken, damage_dealt, mana_spent_est, kills` |
| `npc_seen` | target/gossip/merchant/trainer/quest NPC | `name, id, map, x, y, kind` (x,y = player position at interaction, within ~5 yards) |
| `buff_missing` | built-in check each 5 s out of combat for the player's configured buffs (rule-driven) | `name` |
| `lower_rank_on_bar` | action bar scan found a lower rank than known | `spell, name, bar_rank, known_rank, slot` |

`quest_reward` window `choices[]`: `{index, link, name, quality, equip_slot, armor_type, bind, stats{...}, price}`.

### Presence (bridge computes; addon contributes)

`session.state`: `in_world | loading | afk | logged_out | game_closed`.
* `game_closed`: no WoW window.
* `in_world`: strip decoded within 15 s and addon KV says not afk.
* `afk`: KV `afk=1`.
* `loading`: window exists, strip stale < 60 s and last event not `logout`.
* `logged_out`: window exists, strip absent/stale >= 20 s after a `logout` event, or stale >= 90 s with window present.
Plus `last_login_at`, `last_logout_at`, `foreground` (true/false/unknown: WoW is the front window per CoreGraphics),
`uploader_alive_at` (bridge heartbeat, independent of game data), `seconds_since_last_game_update`.
The bridge emits events `game_started`, `game_closed`, `state_changed {from,to}` and `foreground_changed`.

## New feed sections (text, `--- NAME (updated Ns ago) ---`)

Addon sections (via the existing `push(section, lines, maxAge)`):
`ACTIONBARS` (slot, kind, id, name, rank, macro text; then `known_not_on_bar:` and `lower_rank_on_bar:` lists),
`PET` (family, level, happiness, auras with remaining seconds, pet spells with autocast),
`AURAS` extended (player buffs/debuffs with remaining seconds; pet auras in `PET`),
`TARGET` (name, level, classification, creature type, attackable, hostile nameplate count),
`FIGHTS` (last 5 fights, one line each plus casts), `DEATHS` (last 5 death records),
`ITEMS` (bag items quality >= 1 with link, bind, soulbound, slot, armor type, stats, price; BoE green+ flagged KEEP FOR AH),
`REWARDS` (quest reward window choices while open), `TRAINER` (services and costs while open),
`NPCS` (seen NPC cache, newest 100), `RULES` (rules loaded, last fire time, fire history), `COACH_INBOX` (last 3 coach messages,
with `shown` flags), `COACH_QUESTIONS` (open questions), `SESSION_ADDON` (afk, in_combat, loading flag, last login/logout).
Bridge sections: `SESSION_STATE` (the presence object as key=value lines) and `EVENTS_TAIL` (last 20 events, one line each).

All values the game hides are written as `unknown`. Level and XP stay out of the overlay but remain in `SUMMARY` KV.

## Server API additions (all under `/ui/<secret>/`, GET writes use `?b=<base64url JSON>`)

* `POST events/push` (bridge) `{boot, events:[...]}` -> stores with `eid`; ring buffer 500, persisted.
* `GET events?since=<eid>&types=a,b&limit=100` -> `{ok, events:[{eid, t, received_at, boot, type, data}], next}`.
  Also `GET /<read-token>/events?since=` (read-only, same shape).
* `GET npcs[?name=]` -> seen NPC cache `{name,id,map,x,y,kind,count,last_seen}` aggregated from `npc_seen`.
* `GET session` -> the presence object.
* `POST/GET coach/questions` -> open questions; `ops` accepts `{"op":"coach_answer","id":..,"text":..}` as sugar for `coach`
  that also closes question `id`.
* Overlay ops already implemented in `uidoc.py`: `rule`, `rule_remove`, `rules_clear`, `coach`; `ops` accepts `opId` (idempotent
  retries) and `partial` (apply valid ops, return `rejected:[{index,op,errors}]`); responses include `applied_rev`.

## Webhook (configured on the Mac: `wowmentor webhook set <url> [--secret S]`, stored in `config.json` as `webhook_url`, `webhook_secret`, optional `webhook_auth` sent as the `Authorization` header: `--bearer KEY` or `--auth VALUE`)

POST `application/json`, header `X-WoWMentor-Signature: sha256=<hmac of body with the secret>` when a secret is set,
`X-WoWMentor-Event-Count`. Debounce: at most one POST per 20 s, batching everything since the last push; the first event after
a quiet period is sent after at most 20 s; urgent types (`death`, `logout`, `game_closed`, `bags_full`, `durability_critical`,
`coach_question`) shorten the wait to 3 s. Retries with backoff (5, 20, 60 s), queue capped at 500 events.

```json
{"v":1,"sent_at":1794300123,"session":{"state":"in_world","foreground":true,"last_login_at":1794290000,"last_logout_at":null,"uploader_alive_at":1794300122},
 "events":[{"eid":412,"t":1794300100,"type":"death","data":{"zone":"Elwynn Forest","subzone":"Fargodeep Mine","attackers":2,"killer":{"name":"Kobold Miner","level":6,"elite":false},"deaths_in_subzone_10m":2}}],
 "next":412}
```

## Rules engine (addon, `Coach.lua`)

Rules arrive in `doc.rules` (schema in `uidoc.py`: `WHEN_TYPES`, `DO_TYPES`). Evaluated on the event that can change the condition and
on a 5 s tick for polled conditions (buff missing, bags, durability). Edge-triggered: a rule fires when its condition goes
false -> true (or on each qualifying event for event rules), then respects `cooldown_s`; `once` rules fire once per session and
are remembered in memory (SavedVariables is unreliable on Forever). Template placeholders in action text come from the
firing context: `{count} {subzone} {zone} {name} {free} {pct} {slot} {spell} {rank} {killer}`. Unknown placeholders stay literal.
Every fire is recorded in `RULES` and emitted as `rule_fired`. Actions never touch positions or alpha of panels.

Checklist items may carry `auto` (schema in `uidoc.py`); the addon evaluates them on relevant events and every 2 s, ticks the
item exactly like a player click (so it is reported through `tick` lines and the bridge turns it into a `check` op), only once.

Waypoints may carry `clear_yards` (default 12) and `task` (`panel/item`): the addon shows a small HUD with distance and a
direction arrow (TomTom's distance when available, otherwise own math from map coordinates), and removes the waypoint when the
player is within `clear_yards` or the linked task is done.

## Acks and the coach inbox

`coach` ops go to `doc.inbox`. The addon shows the newest 3 in a built-in panel (`sys-inbox`, positions and alpha are the
player's), prints a chat line when `chat` is true, a toast when `toast` is true, and emits `ack` events when each was shown.
`/coach <question>` records a question (event `coach_question`, section `COACH_QUESTIONS`) and prints "Sent to your coach".

## File ownership while building

* Addon workstream: `wowmentor/addon/WoWFeed/*`, `tests/*.lua`, `tests/wowstub.lua`, `tests/fixtures/*.json`.
* Bridge/server workstream: `wowmentor/server/*` (except the schema parts of `uidoc.py`, which exist), `wowmentor/bridge/*`,
  `wowmentor/agent/wowui.py`, `wowmentor/cli.py`, `wowmentor/config.py`, `wowmentor/doctor.py`, `tests/test_*.py`, `tests/helpers.py`.
* Docs workstream: `docs/*.md` (except this file), `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.github/**`.


## Revisions made during review (the code is the source of truth; these supersede anything above)

* Event envelope is `{"seq","boot","t","type","data"}`; `boot` equals the `uiboot` of the KV line. Every event is sent twice: once
  immediately and once at least 2.5 s later, only when no section is waiting for the strip (fresh events always go first). The repeat
  has the same `seq` and `boot`, so the bridge drops it. An event whose repeat finds no spare capacity within 30 s is dropped. The
  bridge gives a login or logout to presence only once.
* After a `/reload` the KV `uiboot` can lag the new addon load by a few seconds; the bridge labels events from the new load with an
  opaque boot string such as `"1794262931+1"` until the KV catches up. Treat `boot` as opaque.
* SUMMARY KV order: `afk, loading, uirev, uiboot, chat, combat`, then the fields the bridge needs, then the rest. Tokens that do not
  fit the strip width continue on `KV2`, `KV3` lines; the bridge only parses the first line starting with `KV `. Values with spaces use
  underscores (`Night_Elf`).
* `death` events carry `last_hits` (the last 3 damage events from the game's death recap when readable: spell, amount, source) and
  `killer.source` is `recap`, `nameplate` or `unknown`. If the recap is not ready at death, the DEATHS section is enriched 1.5 s later
  (the event already sent is not changed).
* Presence: `loading` is kept up to 90 s of silence; after that, with the window still present, the state is `logged_out`.
* A request may carry at most 100 ops; more is a 400 (they were silently dropped before). `opId` longer than 100 characters is a 400;
  the same `opId` with different ops is a 409.
* `GET /<read-token>/events` redacts every secret (ui, write, read token, webhook URL and secret) as `[redacted]`.
* The server limits to 64 simultaneous connections and a 20 s socket timeout; `config.json` is created with mode 0600.
