# Rules, events and webhooks

**What this is for:** the full reference for the instant-reaction features: rules that run inside the game, the events the game reports back, the coach inbox, checklist auto-complete, waypoint clearing and the webhook. **Who it is for:** AI agent authors and tinkerers. Players can use the five ready-made presets without reading the rest.

Everything here is new in 1.0.0. The schema is enforced by the server (`wowmentor/server/uidoc.py`); `wowui schema` prints the current, authoritative list for your version.

## The idea

An AI that checks your game every few minutes reacts late: you were already dead when it noticed. A **rule** is a small instruction ("when my bags have 2 or fewer free slots, show a warning") that is sent into the game once and then runs there, on the exact game event, with no AI, no computer and no network involved. When a rule fires, the addon reports an **event** so your AI can learn about it.

```text
 your AI  --rule-->  server  --overlay document-->  bridge  -->  game (addon evaluates the rule)
 your AI  <--events--  server  <--bridge  <--pixel strip--  game (addon reports what happened)
                                     \--> webhook (optional, your URL)
```

## Rule schema

A rule is a JSON object. Add it as an overlay op with `"op": "rule"`.

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `op` | `"rule"` | yes (in ops) | | `wowui rule add` fills it in for you |
| `id` | string | yes | | 1 to 40 characters: letters, digits, `_ . : -`. Adding a rule with an existing id replaces it |
| `when` | object | yes | | One condition, or `all` / `any` of 2 to 4 conditions. See the table below |
| `then` | list | yes | | 1 to 4 actions, run in order |
| `cooldown_s` | number | no | `60` | Seconds before this rule may fire again (0 to 86400) |
| `once` | boolean | no | `false` | Fire at most once per game session. This is remembered in memory, because saved variables are not reliable on Forever. A `/reload` or relog starts a new session |
| `enabled` | boolean | no | `true` | `false` keeps the rule but never fires it |

At most **40 rules**. Unknown fields are rejected with a message that names them.

**How and when a rule fires**

- Rules about a **state** (`bags_free_lte`, `durability_lte`, `buff_missing`) are checked every few seconds and fire on the change from false to true (edge triggered), then respect the cooldown. If the game cannot tell (for example the auras are hidden in combat), the state is left as it was and nothing fires.
- Rules about an **event** (everything else) fire each time a matching event happens, then respect the cooldown.
- `all` means every condition holds now; `any` means at least one does. They cannot be nested. A combination that includes an event condition is treated as an event rule.
- Every fire is recorded (section `RULES`) and reported as the event `rule_fired`.
- Rules never move panels or change their transparency.

**Text templates.** The `text`, `title` and `child text` of actions can contain `{placeholders}` that are filled from the situation. Placeholders the situation does not provide stay as written. Curly braces in the filled-in values are removed. Level and XP are never available.

| Condition | Placeholders it provides |
| --- | --- |
| `bags_free_lte` | `{free}` `{count}` (both the number of free slots) |
| `durability_lte` | `{pct}` the lowest durability, `{slot}` its slot name, `{name}` the item in it |
| `deaths_in_subzone` | `{count}` deaths so far, `{subzone}` `{zone}` `{killer}` best guess |
| `buff_missing` | `{name}` `{spell}` |
| `zone_changed`, `entering_world` | `{zone}` `{subzone}` |
| `spell_learned` | `{spell}` `{name}` (the spell name) `{rank}` |
| `lower_rank_on_bar` | `{spell}` `{name}` (the spell name) `{rank}` (the rank you know) `{slot}` |
| `quest_complete` | `{name}` (quest title) |
| `item_looted` | `{name}` `{count}` |
| `item_equipped` | `{name}` `{slot}` |
| `target_elite` | `{name}` |
| `merchant_show`, `trainer_show`, `quest_reward`, `loot_opened` | `{name}` (NPC name, when known) `{count}` |
| `death` | `{killer}` `{subzone}` `{zone}` `{count}` |
| `level_up`, `logout` | none |

The overlay's own live variables (`{player}`, `{zone}`, `{gold}` and so on, see `wowui guide`) are for panel text, not for rule templates.

## Conditions (`when`)

`{"type": "<name>", ...fields}`. Fields marked * are required.

| `type` | Fields | Kind | True when |
| --- | --- | --- | --- |
| `bags_free_lte` | `n`* (0 to 200) | state | Free bag slots are `n` or fewer |
| `durability_lte` | `pct`* (0 to 100) | state | The most worn equipped item is at `pct` percent or less |
| `buff_missing` | `name` or `spell`* (give at least one), `pet` (true: check the pet's auras), `ooc` (true: only check out of combat) | state | The buff is not on you (or the pet). Unknown while auras are hidden |
| `deaths_in_subzone` | `count` (1 to 20), `minutes` (1 to 120) | event | You just died and, counting that death, died `count` times in this subzone within `minutes` |
| `death` | none | event | You died |
| `level_up` | none | event | You gained a level |
| `logout` | none | event | You logged out (best effort) |
| `entering_world` | none | event | You entered the world (login, zoning into the world, `/reload`) |
| `merchant_show` | none | event | A vendor window opened |
| `trainer_show` | none | event | A trainer window opened |
| `quest_reward` | none | event | The quest reward choice window opened |
| `loot_opened` | none | event | A loot window opened |
| `lower_rank_on_bar` | none | event | A lower rank than you know was found on your action bars |
| `spell_learned` | `name` (match this spell, case-insensitive) | event | You learned a spell or rank |
| `quest_complete` | `quest` (a quest id) | event | A quest in your log became ready to turn in |
| `zone_changed` | `zone`, `subzone` (case-insensitive matches) | event | You moved into a (matching) zone or subzone |
| `target_elite` | `level_diff` (-20 to 20), `rare` (true: also rares) | event | You targeted an attackable elite (or rare) that is at least `level_diff` levels above you |
| `item_looted` | `min_quality` (0 to 5) | event | You looted an item of at least that quality |
| `item_equipped` | none | event | You equipped an item |
| `all` / `any` | `of`* (2 to 4 conditions) | | See above |

## Actions (`then`)

`{"do": "<name>", ...fields}`. Fields marked * are required.

| `do` | Fields | What it does |
| --- | --- | --- |
| `toast` | `text`*, `level` (`info` `tip` `good` `warn` `bad`), `sound` (`none` `ping` `warning` `quest` `levelup` `ready`), `title` | A banner at the top of the screen |
| `chat` | `text`*, `sound` | A line in your chat window, shown only to you |
| `check` | `panel`*, `item`*, `done` (default true) | Tick (or untick) a checklist item |
| `waypoint` | `id`*, `map`*, `x`*, `y`* (0 to 100), `title`, `track`, `clear_yards` (1 to 500) | Set a map pin |
| `panel_text` | `panel`*, `child`*, `text`* | Change the text of a text component in a panel |
| `sound` | `sound`* | Play a sound |
| `flash` | none | Bounce the dock icon |

Actions cannot cast, target, move or press anything: the game does not allow it.

## Ready-made presets

For the five most common cases, no JSON is needed:

| Command | What it creates |
| --- | --- |
| `wowui rule preset bags-full` | Warn when 2 or fewer bag slots are free |
| `wowui rule preset durability` | Warn when any item is at 20 percent or less |
| `wowui rule preset deaths` | Warn after 2 deaths in the same subzone within 10 minutes |
| `wowui rule preset buff --set name="Demon Skin"` | Warn when a buff is missing out of combat |
| `wowui rule preset lower-rank` | Warn when an old rank of a spell is on your bar |

Tune with `--set KEY=VALUE` (repeat it): `id`, `cooldown_s`, `once`, `enabled`, `text`, `level`, `sound`, `toast` and `chat` (true or false: which actions to include), plus `n` (bags-full), `pct` (durability), `count` and `minutes` (deaths), `name`, `spell`, `pet`, `ooc` (buff). Example: `wowui rule preset bags-full --set n=4 --set cooldown_s=600`.

Other rule commands: `wowui rule list`, `wowui rule remove <id>`, `wowui rule clear`, `wowui rule add --json '<rule>'` (or pipe JSON into it), and `wowui ops -f file.json` for any ops.

## The five worked examples

Each is shown as JSON you can paste (save it to a file and run `wowui ops -f file.json`, or `wowui rule add --json '...'`) and as the matching preset command. The JSON is exactly what the preset creates.

### 1. Bags full

Preset: `wowui rule preset bags-full`

```json
{
  "op": "rule",
  "id": "preset-bags-full",
  "when": {"type": "bags_free_lte", "n": 2},
  "then": [
    {"do": "toast", "text": "Bags nearly full ({free} slots free). Sell the grey items at a vendor.", "level": "warn"},
    {"do": "chat", "text": "Bags nearly full ({free} slots free). Sell the grey items at a vendor."}
  ],
  "cooldown_s": 300
}
```

The addon re-arms this rule only after you have 4 or more free slots again, so it will not flap between 2 and 3.

### 2. Durability under 20 percent

Preset: `wowui rule preset durability`

```json
{
  "op": "rule",
  "id": "preset-durability",
  "when": {"type": "durability_lte", "pct": 20},
  "then": [
    {"do": "toast", "text": "Your gear is at {pct}% durability. Visit a repair vendor soon.", "level": "warn"},
    {"do": "chat", "text": "Your gear is at {pct}% durability. Visit a repair vendor soon."}
  ],
  "cooldown_s": 600
}
```

### 3. Two deaths in the same subzone within 10 minutes

Preset: `wowui rule preset deaths`

```json
{
  "op": "rule",
  "id": "preset-deaths",
  "when": {"type": "deaths_in_subzone", "count": 2, "minutes": 10},
  "then": [
    {"do": "toast", "text": "You have died {count} times in {subzone} in the last 10 minutes. This area may be too hard right now: pick easier quests or find a group.", "level": "bad"},
    {"do": "chat", "text": "You have died {count} times in {subzone} in the last 10 minutes. This area may be too hard right now: pick easier quests or find a group."}
  ],
  "cooldown_s": 300
}
```

If you change `count` or `minutes`, change the wording in `text` too (the preset's text says 10 minutes).

### 4. Demon Skin missing out of combat

Preset: `wowui rule preset buff --set name="Demon Skin"`

```json
{
  "op": "rule",
  "id": "preset-buff-demon-skin",
  "when": {"type": "buff_missing", "name": "Demon Skin", "ooc": true},
  "then": [
    {"do": "toast", "text": "Buff missing: {name}. Cast it before the next fight.", "level": "tip"},
    {"do": "chat", "text": "Buff missing: {name}. Cast it before the next fight."}
  ],
  "cooldown_s": 120
}
```

The name must match the buff's name exactly as the game shows it (case does not matter). You can use `"spell": <spell id>` instead. In combat the game hides auras from addons, so this rule only decides out of combat.

### 5. A lower spell rank on the action bar

Preset: `wowui rule preset lower-rank`

```json
{
  "op": "rule",
  "id": "preset-lower-rank",
  "when": {"type": "lower_rank_on_bar"},
  "then": [
    {"do": "toast", "text": "{name} on your bar is a lower rank than the one you know (slot {slot}). Drag the newest rank onto the bar.", "level": "tip"},
    {"do": "chat", "text": "{name} on your bar is a lower rank than the one you know (slot {slot}). Drag the newest rank onto the bar."}
  ],
  "cooldown_s": 300
}
```

You do the dragging. WoW Mentor only tells you. See `ACTIONBARS` in [FEED.md](FEED.md) for the data behind it.

## Checklist auto-complete

A checklist item can carry an `auto` condition. The addon checks them on relevant events and every couple of seconds and ticks the item exactly like a click from you (so your AI sees the tick), once per item per document version. You can still untick it.

| `auto.type` | Fields | Ticks when |
| --- | --- | --- |
| `quest_done` | `quest`* (id) | The quest is completed |
| `spell_known` | `spell` (id) or `name` (give one) | You know the spell |
| `skill_gte` | `skill`* (name), `value`* (1 to 600) | The skill or profession is at least that rank |
| `item_in_bags` | `item`* (id), `count` (default 1) | You carry at least that many |
| `zone` | `zone`* | You are in a zone, subzone or minimap zone of that name |

```json
{
  "op": "panel",
  "id": "next",
  "title": "Next steps",
  "children": [
    {"type": "checklist", "items": [
      {"id": "wolves", "text": "Turn in Wolves Across the Border", "auto": {"type": "quest_done", "quest": 33}},
      {"id": "fa", "text": "Learn First Aid to 20", "auto": {"type": "skill_gte", "skill": "First Aid", "value": 20}},
      {"id": "arrive", "text": "Go to Goldshire", "auto": {"type": "zone", "zone": "Goldshire"}}
    ]}
  ]
}
```

## Waypoints that clear themselves

A waypoint may have `clear_yards` (default 12) and `task` (`panel/item`). The addon shows a small distance arrow (using TomTom's distance if installed, otherwise its own math), and removes the waypoint when you are within `clear_yards` of it, or when the linked checklist item is done.

```json
{"op": "waypoint", "id": "trainer", "map": 1429, "x": 48.9, "y": 41.0, "title": "Trainer", "track": true, "clear_yards": 10, "task": "next/wolves"}
```

## The coach inbox, `/coach` and acknowledgements

- `wowui coach "Try the other path"` (op `coach`) puts a message in the coach inbox (a small built-in panel showing the newest 3), prints a chat line unless you add `--no-chat`, and a banner with `--toast`.
- The player can type `/coach <question>` in game. The addon prints "Sent to your coach" and reports a `coach_question` event. Read open questions with `wowui questions` (or `GET .../coach/questions`).
- Answer with `wowui coach "..." --answer <question id>`, which also closes the question (op `coach_answer`).
- When a toast, coach line or chat line was actually shown, the addon reports an `ack` event with its `id`.

## Reading events

Each event is `{"eid": 412, "t": 1794300100, "received_at": 1794300102.4, "boot": 1794290000, "type": "death", "data": {...}}`.
`eid` increases by one for every event and never repeats until the data is reset. `t` is the game's clock (seconds since 1970). `boot` identifies the game session (it changes on `/reload` or relog). Treat it as an opaque label: right after a `/reload` it can be a string such as `"1794262931+1"`.

```bash
wowui events                         # the newest events
wowui events --since 412             # events after eid 412 (the "next" of your last call)
wowui events --types death,fight     # only some types
wowui events --follow                # keep running, one event per line
```

Over HTTP: `GET <overlay link>/events?since=<eid>&types=a,b&limit=100` returns:

```json
{"ok": true, "events": [{"eid": 413, "t": 1794300160, "received_at": 1794300161.2, "boot": 1794290000, "type": "bags_full", "data": {"free": 2}}], "next": 413, "last_eid": 413, "oldest_eid": 41}
```

Ask again with `since=<next>`. Extra flags: `gap: true` means events between your cursor and `oldest_eid` were dropped (the server keeps the newest 500); `reset: true` means your cursor is ahead of the server (data was reset), so start again from `next`. Maximum `limit` is 500. For agents that only hold the feed link, `GET <feed link without ?t>/events?since=<eid>` returns the same shape, read-only, with every secret (overlay secret, write secret, read token, webhook URL and webhook secret) replaced by `[redacted]` if it ever appears.

Also: `wowui npcs [--name x]` (NPCs you met: name, id, map, x, y, kind, count, last seen; the position is where *you* stood, within about 5 yards of the NPC), `wowui session` (see [HOW-IT-WORKS.md](HOW-IT-WORKS.md#presence)).

### Event types

| `type` | When | `data` |
| --- | --- | --- |
| `login` | You entered the world after logging in | `char`, `realm`, `map`, `zone`, `subzone`, `bind` (where your hearthstone is bound) |
| `logout` | You logged out (best effort; may not arrive) | none |
| `level_up` | Level gained | `level` |
| `death` | You died | `killer` {`name`, `level`, `classification`, `elite`}, `attackers`, `attackers_list` [{`name`, `level`, `classification`}], `hp_at_pull`, `hpmax`, `mana_at_pull`, `manamax`, `map`, `x`, `y`, `zone`, `subzone`, `deaths_in_subzone_10m` |
| `death_released` | You released your spirit | `durability_pct` |
| `quest_complete` | A quest became ready to turn in | `quest`, `title` |
| `quest_turned_in` | A quest was turned in | `quest`, `title` |
| `spell_learned` | A spell or rank was learned | `spell`, `name`, `rank` |
| `item_equipped` | Equipment changed | `slot`, `link`, `name`, `quality`, `bind`, `soulbound` |
| `item_looted` | You looted an item (quality 2 and above is sent) | `link`, `name`, `quality`, `count`, `bind` (`none` `pickup` `equip` `use` `quest`), `equip_slot`, `armor_type`, `price` (copper), `boe_keep` (true for green-or-better bind-on-equip) |
| `zone_changed` | New zone or subzone | `map`, `zone`, `subzone` |
| `window_open` | A vendor, trainer, quest reward or loot window opened | `window` (`merchant` `trainer` `quest_reward` `loot`), `npc` {`name`, `id`, `map`, `x`, `y`}, `choices` [...] |
| `bags_full` | Free bag slots dropped to 2 or fewer (once, until 4 or more are free again) | `free` |
| `durability_critical` | An item dropped to 20 percent or less (once, until it recovers) | `pct`, `slot` |
| `rule_fired` | One of your rules fired | `rule`, `actions` [names], `ctx` {placeholders and values} |
| `coach_question` | The player typed `/coach <text>` | `id`, `text` |
| `ack` | A toast, coach line or chat line was actually shown | `kind` (`toast` `coach` `chat`), `id`, `shown_at` |
| `fight` | A fight ended | `start`, `end`, `duration`, `result` (`won` `died` `fled`), `casts` [{`t`, `spell`, `name`, `rank`, `target`, `wasted`}], `duplicate_dots` [{`spell`, `name`, `target`, `at`}], `damage_taken`, `damage_dealt`, `mana_spent_est`, `kills` |
| `npc_seen` | You targeted or talked to an NPC for the first time this session | `name`, `id`, `map`, `x`, `y`, `kind` |
| `buff_missing` | A buff your rules watch for went missing | `name` |
| `lower_rank_on_bar` | A lower rank than you know is on your bars | `spell`, `name`, `bar_rank`, `known_rank`, `slot` |
| `game_started` | The bridge saw the game window appear | none |
| `game_closed` | The game window went away | none |
| `state_changed` | Your presence state changed | `from`, `to` (see [presence](HOW-IT-WORKS.md#presence)) |
| `foreground_changed` | The game moved to or from the front | `foreground`, `previous` (true, false or `"unknown"`) |
| `webhook_test` | Sent only by `wowmentor webhook test` | `message` |

`window_open` `choices` for a quest reward are `{index, link, name, quality, equip_slot, armor_type, bind, stats, price}`. Anything the game hides is `null` or the string `unknown`. Event payloads are capped in size; very long lists are shortened.

Which events are sent depends on what your game build allows (see [FOREVER-API.md](FOREVER-API.md)): for example `death.killer` is a best guess, not a fact.

## Webhook

The webhook pushes events to your own URL as they happen, so you do not need to poll.

```bash
wowmentor webhook set https://example.com/hooks/wow --secret -    # then type the secret and press Return
wowmentor webhook test       # sends a sample event, tells you the HTTP status
wowmentor webhook status     # where events go, how many were delivered, whether it is failing
wowmentor webhook off        # stop sending

# If the receiver wants a plain key in an Authorization header instead of (or as well as) the signature:
wowmentor webhook set https://example.com/hooks/wow --bearer -    # sends  Authorization: Bearer <key>  with every POST
wowmentor webhook set https://example.com/hooks/wow --auth "Basic abc123"   # any other Authorization value
```

Notes: `--secret -` reads the secret from standard input so it does not end up in your shell history; you can also set `WOWMENTOR_WEBHOOK_SECRET`. Leave `--secret` out to keep the one already stored; `--no-secret` removes it. The running bridge notices a change within a few seconds. The URL must be `http://` or `https://`; use `https://` unless the receiver is on your own Mac. The URL path may itself be a secret, so it is never printed or logged (only `scheme://host`).

### What is sent

`POST` with `Content-Type: application/json`:

```json
{
  "v": 1,
  "sent_at": 1794300123,
  "session": {"state": "in_world", "foreground": true, "last_login_at": 1794290000, "last_logout_at": null, "uploader_alive_at": 1794300122},
  "events": [
    {"eid": 412, "t": 1794300100, "type": "death", "data": {"zone": "Elwynn Forest", "subzone": "Fargodeep Mine", "attackers": 2, "killer": {"name": "Kobold Miner", "level": 6, "elite": false}, "deaths_in_subzone_10m": 2}}
  ],
  "next": 412
}
```

Headers: `X-WoWMentor-Event-Count` (how many events), `User-Agent: wowmentor-webhook/1`, and, when you set a secret, `X-WoWMentor-Signature: sha256=<hex>`.

### When it is sent

- Events are batched. At most **one POST every 20 seconds**; a batch holds up to 100 events (more go in the next POST).
- The first event after a quiet period goes out after at most 20 seconds. These **urgent** types shorten the wait to **3 seconds**: `death`, `logout`, `game_closed`, `bags_full`, `durability_critical`, `coach_question`.
- Your endpoint must answer with a `2xx` status. Anything else (or no answer within 10 seconds) is a failure: the bridge keeps the events and retries after 5 seconds, then 20, then every 60. The queue holds the newest 500 events; older ones are dropped.
- Redirects are not followed.
- Delivery is "at least once": the same event can arrive twice (for example if your answer was slow). Use `eid` to ignore repeats.

### Verifying the signature

The signature is HMAC-SHA256 of the **exact bytes of the request body**, keyed with your secret, written as `sha256=` plus lowercase hex. Verify before you trust the message, and compare in constant time. Also check that `sent_at` is recent (for example within 5 minutes) to make replays less useful.

Python:

```python
import hashlib, hmac, time, json

def verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")

# in your request handler, with `raw_body` as the bytes you received and `headers` as the HTTP headers:
# if not verify(SECRET, raw_body, headers.get("X-WoWMentor-Signature")):
#     reject with 401
# message = json.loads(raw_body)
# if abs(time.time() - message["sent_at"]) > 300: reject
# for event in message["events"]: handle(event)   # skip eids you have already handled
```

Shell (save the raw body to a file first, for example `body.json`, and put the header value in `$SIG`):

```bash
EXPECTED="sha256=$(openssl dgst -sha256 -hmac "$WOWMENTOR_WEBHOOK_SECRET" -r body.json | cut -d' ' -f1)"
[ "$EXPECTED" = "$SIG" ] && echo "signature ok" || echo "signature BAD"
```

(The shell comparison is not constant time; use it for scripts and testing, and use the Python form in a real receiver.)

A tiny receiver for testing on your own Mac:

```python
import hashlib, hmac, http.server, json, os

SECRET = os.environ["WOWMENTOR_WEBHOOK_SECRET"]

class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        want = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, self.headers.get("X-WoWMentor-Signature", "")):
            self.send_response(401); self.end_headers(); return
        for ev in json.loads(body)["events"]:
            print(ev["eid"], ev["type"], ev["data"])
        self.send_response(204); self.end_headers()

http.server.HTTPServer(("127.0.0.1", 9000), Hook).serve_forever()
```

Run it, then `wowmentor webhook set http://127.0.0.1:9000/ --secret -` and `wowmentor webhook test`.

## What the AI sees in the feed

The feed has matching sections: `RULES` (rules loaded, last fire time, recent fires), `COACH_INBOX`, `COACH_QUESTIONS`, `EVENTS_TAIL` (the last 20 events) and `SESSION_STATE`. See [FEED.md](FEED.md).

## Limits and honest notes

- One request carries at most 100 ops; more is rejected with a 400 (nothing is silently dropped). A repeated `opId` with different ops is a 409.
- A rule only works while the game is running with your character in the world; rules are delivered through the overlay document, so a change takes a few seconds to arrive. A `/reload` re-applies the newest document.
- Rule state (cooldowns, "once") lives in game memory for the session and starts over after a `/reload` or relog.
- Events that happen while the bridge or the Mac is off are lost.
- The in-game queue of unsent events holds 200; the server keeps the newest 500; the webhook queue holds 500.
- The game hides many numbers in combat and in instances. Rules that need them stay quiet instead of guessing.
