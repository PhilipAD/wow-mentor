# What the Forever client allows addons to do

**What this is for:** a researched report on what is and is not possible for an addon like WoW Mentor on the WoW Forever client, why the design is the way it is, and what the helper (macOS or Windows) does where the game says no. **Who it is for:** contributors, and curious players. You do not need it to use WoW Mentor.

Research date: 2026-11-09. Forever is new and still changing, so check anything important against a current client before depending on it.

## How to read this

Each topic gives: what the game allows, how sure we are, the sources, and what WoW Mentor does about it.

| Confidence | Meaning |
| --- | --- |
| **High** | The Warcraft Wiki marks the API or event as available on the `forever` game type, or Blizzard's own notes say it, and the behaviour is documented. |
| **Medium** | Reported in game by an addon author (named in the source), or inferred from documented behaviour of the shared Mainline code. Not tested by us. |
| **Low** | An inference or an idea nobody has confirmed on Forever. |

We did not test these in the game ourselves for this report. Where WoW Mentor's own code shows behaviour on a real client (for example a comment saying a call returned nothing) we say so.

## The client in one paragraph

Forever uses the modern addon API, not the Classic Era one. Blizzard said in September 2026 that Forever shares Mainline WoW's UI architecture "including the vast majority of APIs available in 12.1.5", that the addon restrictions introduced in Midnight (12.0), including **secret values**, are active in Forever, and that the new aura button and container system is available as well. The addon interface number is `16001` (game version 1.60.1), the internal game type name is `camelot` and `forever` is accepted as an alias. WoW Mentor's addon declares `## Interface: 16001`. (High.)
Sources: [Patch 1.60.1 API changes](https://warcraft.wiki.gg/wiki/Patch_1.60.1/API_changes), [Secret Values](https://warcraft.wiki.gg/wiki/Secret_Values), [Patch 12.0.0 API changes](https://warcraft.wiki.gg/wiki/Patch_12.0.0/API_changes). Community notes with in-game checks by an addon author: [Forever++ API notes](https://github.com/xIGBClutchIx/ForeverPlusPlus/blob/main/docs/forever-api.md) (referred to below as "Forever++ notes").

**Secret values** in short: in some situations (combat, boss encounters, instanced content, and so on) an API returns a "secret" value instead of a real one. Addon code can store it and pass it to a few display functions (for example `StatusBar:SetValue`), but cannot compare it, do arithmetic with it, or use it as a table key; trying raises a Lua error. `issecretvalue(v)` tells you. Source: [Secret Values](https://warcraft.wiki.gg/wiki/Secret_Values).

The addon in this repository follows one rule everywhere: wrap every optional API in `pcall` or a feature check, treat a secret or missing answer as `unknown`, and never raise.

## Summary table

| Topic | Can an addon do it on Forever? | Confidence | What WoW Mentor does |
| --- | --- | --- | --- |
| Is the game window in the foreground? | No API found | Medium | The bridge asks the OS (macOS or Windows) which window is in front |
| Scan nameplates | Yes, in the open world; identity is secret in instances | High / Medium | Reads them for a "nearby" list and tolerates `unknown` |
| Combat log | No (`COMBAT_LOG_EVENT_UNFILTERED` is closed to addons) | High | Rebuilds fights from other events; killer is best effort |
| Action bar contents | Yes (`GetActionInfo`) | High (existence) | Scans slots and compares with the spellbook |
| Auras in combat | Not readable while secret | High / Medium | Skips aura rules in combat; shows `unknown` |
| Item bind type, soulbound | Yes (`C_Item.GetItemInfo`, `C_Item.IsBound`, container info) | High | Reads only, never "previews" an equip |
| Trainer and quest reward windows | Yes (open window can be read) | High / Medium | Shows costs and choices while the window is open |
| NPC positions | No (only you and your group) | High | Remembers your own position when you interact with an NPC |
| Network and file access | No | High | Draws a pixel strip; the bridge reads it |
| SavedVariables | Yes, but written only at logout or `/reload` | High | Not used for live data; read-only peek at other addons |
| Hardware events | Spells, targeting and movement are impossible; some chat needs a real key press | High | Display only; prints locally |

## 1. Foreground (window focus) detection from inside the game

**Allowed?** We found no Lua API that tells an addon whether the game window has operating system focus. The Warcraft Wiki API index has no such function. The only function we found with that name, `IsWindowFocused`, comes from `awesome_wotlk`, a third-party patch for the old 3.3.5 client, so it does not exist in Forever and would not be acceptable (it modifies the game client). Old addons guessed from side effects: when the game window loses focus, frames stop updating (`OnUpdate` stops firing) or the frame rate drops to about 10, and they measured that. That is a heuristic from retail-era addons, not a Forever fact, and it gives false positives under load. (Confidence: Medium that there is no API; Low for the heuristic on Forever.)
Sources: [World of Warcraft API index](https://warcraft.wiki.gg/wiki/World_of_Warcraft_API), [Alt-Tab Toggle addon FAQ](https://www.wowace.com/projects/alt-tab-toggle), [awesome_wotlk API reference](https://github.com/FrostAtom/awesome_wotlk/blob/main/docs/api_reference.md).

**What the bridge does instead:** the bridge asks the operating system which window is in front (CoreGraphics window list on a Mac, the foreground window on Windows) and reports `foreground` as true, false or `unknown` (when the system gives no answer), together with the game's AFK flag and how fresh the strip is. See [HOW-IT-WORKS.md](HOW-IT-WORKS.md#presence). The addon itself never claims to know.

## 2. Nameplate scanning

**Allowed?** Yes. `C_NamePlate.GetNamePlates()` and `C_NamePlate.GetNamePlateForUnit(unitToken)` are marked `forever + 1.60.1`, and the events `NAME_PLATE_UNIT_ADDED` and `NAME_PLATE_UNIT_REMOVED` exist. Each nameplate has a unit token (`nameplate1` ...). Real limits:

- Nameplates exist only for units whose plates the game currently draws (distance limit, the player's nameplate settings, and at most 40). Unit tokens `nameplateN` can be queried but cannot be targeted or cast on.
- Midnight made creature names, GUIDs and IDs secret **inside instances**, no longer dependent on combat state. In the open world they are plain. Health of non-player units is secret in combat.
- A Forever addon author reported that nameplate aura frames are restricted regions and cannot be read or re-anchored safely, and that friendly units' health read as secret on name-only plates.
- Never anchor your own frame to the nameplate base frame (it breaks Blizzard's layout code).

(Existence: High. Open-world readability and the rest: Medium.)
Sources: [C_NamePlate.GetNamePlates](https://warcraft.wiki.gg/wiki/API:C_NamePlate.GetNamePlates), [UnitToken](https://warcraft.wiki.gg/wiki/UnitToken), [Patch 12.0.0 planned changes](https://warcraft.wiki.gg/wiki/Patch_12.0.0/Planned_API_changes), Forever++ notes (Nameplates, Secret values).

**What WoW Mentor does:** the `NEARBY` section lists visible nameplate units with their name, level, classification and flags, and writes anything the game hides as `unknown`. The coach's fight reconstruction uses nameplate units to find hostile units that were targeting you, as a best effort. It does not assume the list is complete.

## 3. Combat log

**Allowed?** No. In patch 12.0.0 Blizzard closed the combat log to addons: `COMBAT_LOG_EVENT` and `COMBAT_LOG_EVENT_UNFILTERED` cannot be registered (it errors), and `CombatLogGetCurrentEventInfo` was deprecated and is listed only for versions up to 12.0.0. The Forever++ notes report that `COMBAT_LOG_EVENT_UNFILTERED` never fires on Forever and that `CombatLogGetCurrentEventInfo` is absent from the client's API dump. (Mainline: High. Forever: High from Blizzard's statement that the restrictions are active, plus Medium from the addon author's checks.)

What is left:

- `UNIT_COMBAT` (`forever + 1.0.0`): amounts and event type for units that took damage. An addon author reported its amounts stayed readable in a solo open-world fight on Forever. Medium.
- `PLAYER_REGEN_DISABLED` and `PLAYER_REGEN_ENABLED`: you entered and left combat. High.
- `UNIT_SPELLCAST_*` for the player: Blizzard relaxed the rules so the player's own casts are not secret, even in combat. For other units they are secret. High (Blizzard's 12.0.0 notes).
- New in 12.0.0 and tagged `forever`: the events `UNIT_DIED` (its GUID is secret under the unit-identity rules) and `PLAYER_TARGET_DIED`, and the function `C_DeathRecap.GetRecapEvents`, which feeds Blizzard's own death recap window. Whether `GetRecapEvents` returns anything useful to an addon after you die, and with readable fields, is **not verified**. This is the most promising source of a real "what killed me" answer and is worth testing in game. Low.
- `C_DamageMeter` (Blizzard's built-in damage meter data) exists in the Forever API dump. Not evaluated. Low.
- Chat lines such as "Creature dies, you gain X experience" come through `CHAT_MSG_COMBAT_XP_GAIN`, which WoW Mentor already uses for kill counts. Chat in instances is secret.

Sources: [Patch 12.0.0 API changes](https://warcraft.wiki.gg/wiki/Patch_12.0.0/API_changes), [COMBAT_LOG_EVENT](https://warcraft.wiki.gg/wiki/Event:COMBAT_LOG_EVENT), [CombatLogGetCurrentEventInfo](https://warcraft.wiki.gg/wiki/API:CombatLogGetCurrentEventInfo), [UNIT_COMBAT](https://warcraft.wiki.gg/wiki/Event:UNIT_COMBAT), [UNIT_DIED](https://warcraft.wiki.gg/wiki/UNIT_DIED), [PLAYER_TARGET_DIED](https://warcraft.wiki.gg/wiki/PLAYER_TARGET_DIED), [C_DeathRecap.GetRecapEvents](https://warcraft.wiki.gg/wiki/API_C_DeathRecap.GetRecapEvents), Forever++ notes (Secret values and combat data).

**What WoW Mentor does:** reconstructs each fight from `UNIT_SPELLCAST_*`, `UNIT_COMBAT`, `PLAYER_REGEN_*`, nameplate units and the experience-gain chat lines. The "killer" in a `death` event is the best guess from hostile units that were targeting you, and is marked as such. Exact damage by source is not available. Health and mana "at pull" are the last out-of-combat values.

**Another route that is not used:** the client can write a combat log text file that outside programs have always been able to read. We have not checked whether that works on Forever, or whether it is allowed in the spirit of Blizzard's changes, and WoW Mentor does not use it.

## 4. Action bar APIs

**Allowed?** Yes for reading. `GetActionInfo(slot)` is tagged `forever + 1.60.1` and returns the type (`spell`, `item`, `macro`, `companion`, `equipmentset`, `flyout`), an id and a subtype. With the spell id, the spellbook (`C_SpellBook`) tells you which rank you know, so "a lower rank of this spell is on your bar" can be computed. 12.0.0 added a `C_ActionBar` namespace with many read functions (texture, text, cooldown, usable, in-range). We have not confirmed which of those have a Forever tag. Macros need their body read separately. Putting something onto a bar or using a slot is protected (see hardware events below), and the overlay never does it. (Existence of `GetActionInfo`: High. Rank logic: Medium, because it depends on Forever's spell ids and sub-names for ranks.)
Sources: [GetActionInfo](https://warcraft.wiki.gg/wiki/API:GetActionInfo), [Patch 12.0.0 API changes](https://warcraft.wiki.gg/wiki/Patch_12.0.0/API_changes) (the added `C_ActionBar` list), [C_ActionBar.GetSpell](https://warcraft.wiki.gg/wiki/API_C_ActionBar.GetSpell) (no `forever` tag on that page).

**What WoW Mentor does:** the `ACTIONBARS` section lists slots with kind, id, name, rank and macro text, then `known_not_on_bar:` and `lower_rank_on_bar:` lists, and emits the `lower_rank_on_bar` event. Anything unreadable is `unknown`.

## 5. Aura secrecy

**Allowed?** Only when auras are not secret. Midnight made aura queries secret during combat, boss encounters, Mythic+ and PvP matches; the 12.1.0 changes go further (aura APIs return secrets or nothing, and Blizzard replaced addon-readable aura lists with "aura buttons" and "aura containers" that addons can style but not read). `C_Secrets.ShouldAurasBeSecret()` is tagged `forever + 1.60.1` and tells you when queries will be secret. A Forever addon author recorded the error "Auras cannot be accessed when secret while tainted" when reading the player's own buffs from addon code after casts in combat (149 times in one session). Individual spells can be flagged never-secret or always-secret (`C_Secrets.GetSpellAuraSecrecy`). High for the rule; Medium for exact behaviour in each situation.
Sources: [Secret Values](https://warcraft.wiki.gg/wiki/Secret_Values), [Patch 12.1.0 API notes](https://warcraft.wiki.gg/wiki/Patch_12.1.0/API_changes/notes), [C_Secrets.ShouldAurasBeSecret](https://warcraft.wiki.gg/wiki/API_C_Secrets.ShouldAurasBeSecret), Forever++ notes.

**What WoW Mentor does:** the aura collector checks every value with `issecretvalue`, writes `unknown s left` when a time is hidden, and the "buff missing" rule only evaluates out of combat (when auras are readable). An AI should not conclude "no buff" from an empty or `unknown` aura list in combat.

## 6. Item binding and soulbound state

**Allowed?** Yes. `C_Item.GetItemInfo` is tagged `forever + 1.60.1` and its 14th return value, `bindType` (`Enum.ItemBind`), says when the item binds: none, on pickup, on equip, on use, quest. `C_Item.IsBound(itemLocation)` is tagged `forever + 1.60.1` and says whether a specific item is soulbound already. The container call `C_Container.GetContainerItemInfo(bag, slot)` also returns `isBound`. The old global `GetItemInfo` is absent from the Forever client, so use the `C_Item` forms. Item data may not be cached yet and then returns `nil`; ask for it and read it later (`GET_ITEM_INFO_RECEIVED`). A tooltip scan (`C_TooltipInfo`) is a possible fallback. (High for existence.)
Sources: [C_Item.GetItemInfo](https://warcraft.wiki.gg/wiki/API:C_Item.GetItemInfo), [C_Item.IsBound](https://warcraft.wiki.gg/wiki/API_C_Item.IsBound), [C_Container.GetContainerItemInfo](https://warcraft.wiki.gg/wiki/API_C_Container.GetContainerItemInfo), Forever++ notes (Removed Classic globals).

**What WoW Mentor does:** `ITEMS` and the `item_looted` event report `bind` (when it binds) and `soulbound` (whether it already is), and flag green-or-better bind-on-equip items as "keep for auction house". It only reads. It never equips or "previews" an item, because on Forever an item that binds on equip can become soulbound the moment it is equipped.

## 7. Trainer and quest reward windows

**Allowed?** Yes, reading. The events `TRAINER_SHOW`, `TRAINER_UPDATE`, `TRAINER_CLOSED`, `QUEST_COMPLETE` (tagged `forever`, fired when the reward choice is offered), `MERCHANT_SHOW`, `QUEST_TURNED_IN` and friends exist, and `GetTrainerServiceInfo` and `GetNumQuestChoices` are tagged `forever + 1.60.1`. While a window is open an addon can list the trainer services (name, rank, category, cost) and the reward choices (links, stats, price). Watch out for:

- `GetMerchantItemInfo` had its compatibility wrapper removed in 12.0.0 and the page lists no Forever support; use `C_MerchantFrame.GetItemInfo`. A Forever addon author also lists it as absent. (Medium.)
- Opening a Blizzard window from addon code (the character sheet, the map, bags) runs Blizzard's setup in tainted mode and errors in Forever (an addon author's in-game test). WoW Mentor never opens windows; it reads the ones you open.
- Choosing a reward, training a spell and buying are actions only you can take.

Sources: [Events index](https://warcraft.wiki.gg/wiki/Events), [QUEST_COMPLETE](https://warcraft.wiki.gg/wiki/QUEST_COMPLETE), [GetNumQuestChoices](https://warcraft.wiki.gg/wiki/API_GetNumQuestChoices), [GetTrainerServiceInfo](https://warcraft.wiki.gg/wiki/API:GetTrainerServiceInfo), [GetMerchantItemInfo](https://warcraft.wiki.gg/wiki/API_GetMerchantItemInfo), Forever++ notes (Secure code and Blizzard systems).

**What WoW Mentor does:** the `REWARDS` and `TRAINER` sections and the `window_open` event exist only while the window is open, and show costs and choices so your AI can advise. It never clicks anything.

## 8. NPC positions

**Allowed?** No API gives the position of an NPC. `UnitPosition(unit)` and `C_Map.GetPlayerMapPosition(map, unit)` both work only for `player`, `partyN` and `raidN`; the wiki says explicitly they do not work for pets or any unit not in your group. Both return `nil` inside restricted areas (instances, battlegrounds, arenas). WoW Mentor's own addon notes that on the client it was developed on, `C_Map.GetPlayerMapPosition` returned nothing for the player, so it computes the map position from `UnitPosition` plus the map's world corners, and as a last resort reads the coordinates other addons draw on screen as text. (High for the limits; the client-specific note comes from this repository's code.)
Sources: [UnitPosition](https://warcraft.wiki.gg/wiki/API:UnitPosition), [C_Map.GetPlayerMapPosition](https://warcraft.wiki.gg/wiki/API_C_Map.GetPlayerMapPosition).

**What WoW Mentor does:** when you talk to an NPC (target, gossip, merchant, trainer, quest giver) it records `npc_seen` with the NPC's name and id and **your own** position at that moment, which is within about 5 yards of the NPC. The server aggregates these into a cache (`GET .../npcs`). That is the only way positions get collected, so it only knows NPCs you have met.

## 9. Network and file access

**Allowed?** No. WoW's Lua has no `io`, `os`, `package`, `require` or `loadfile`; there are no sockets. The only ways data leaves an addon are chat messages and addon messages (`C_ChatInfo.SendAddonMessage`, throttled, 255 characters, not usable inside instances or during encounters since 12.0.0, and not allowed for trial accounts on Forever), and SavedVariables. Forever's Beta 4 notes mention a Lua `import` function (renamed from `require`); it is for loading code, and we did not find any file reading or writing API. (High that there is no I/O; the purpose of `import` is not verified.)
Sources: [Lua functions](https://warcraft.wiki.gg/wiki/Lua_functions), [C_ChatInfo.SendAddonMessage](https://warcraft.wiki.gg/wiki/API:C_ChatInfo.SendAddonMessage), [Patch 1.60.1 API changes](https://warcraft.wiki.gg/wiki/Patch_1.60.1/API_changes).

**What WoW Mentor does:** the addon draws a pixel strip and the bridge reads it (see [HOW-IT-WORKS.md](HOW-IT-WORKS.md)); the bridge, not the addon, talks to the server and to your webhook. For the other direction, the bridge writes tiny Lua files into pre-created load-on-demand addon folders.

## 10. SavedVariables behaviour

**Allowed?** Yes, with limits. The client writes SavedVariables when you log out, disconnect, quit or `/reload`, not while you play, and a crash or force quit does not write them. Files live under `WTF/Account/<account>/SavedVariables/` and per character. Variables are loaded after the addon's files run, so an addon must initialise them on `ADDON_LOADED`. During the Forever beta SavedVariables were written but not read back until a fix around 2026-09-24, so an addon author's advice is not to depend on them for anything critical. (High for the rules; Medium for Forever's reliability.)
Sources: [Saving variables between game sessions](https://warcraft.wiki.gg/wiki/Saving_variables_between_game_sessions), [AddOn loading process](https://warcraft.wiki.gg/wiki/AddOn_loading_process), Forever++ notes (SavedVariables).

**What WoW Mentor does:** the addon keeps your panel positions and tick marks in its own saved variable (`WoWFeedDB`), but live data never goes through saved variables, and "once only" rules are remembered in memory for the session. The bridge also reads a few other addons' saved variable files read-only, and says in the feed that they are only as fresh as the last reload or logout.

## 11. Hardware events and protected actions

**Allowed?** Since 2006 (patch 2.0) addons cannot cast spells, target, or move at all except through secure templates that need a real click or key press from the player. A hardware event is a real keyboard or mouse input; timers, events and macros cannot fake one. Other functions need one too: for example `TakeTaxiNode`, and `SendChatMessage` for `SAY`, `YELL` and (since Forever beta build 70291) `EMOTE` outside instances. Forever has the same secure execution and taint model as Mainline. (High.)
Sources: [Secure Execution and Tainting](https://warcraft.wiki.gg/wiki/Secure_Execution_and_Tainting), [SecureActionButtonTemplate](https://warcraft.wiki.gg/wiki/SecureActionButtonTemplate), [Patch 1.60.1 API changes](https://warcraft.wiki.gg/wiki/Patch_1.60.1/API_changes) (Beta 4 notes), [UI FAQ: macros and scripts](https://warcraft.wiki.gg/wiki/UI_FAQ/Macros_and_scripts).

**What WoW Mentor does:** nothing that needs one. It draws frames, sets map pins (using TomTom if installed, otherwise the game's own waypoint), plays sounds, prints chat lines **locally** (not sent to anyone), and flashes the dock icon. It does not use `SendChatMessage`.

## 12. Other things that shaped the design

- **Health and power in combat are secret.** `UnitHealth`, `UnitPower` and their percentages returned secrets in a solo open-world fight on Forever (addon author's report); the player's own maximums are plain. `StatusBar:SetValue` accepts secrets, so the addon feeds hidden values into three status bars and the bridge measures how full they are in the picture. Medium. This is why health and mana are shown as `~` estimates.
- **Chat.** In instances, chat messages reach Lua as secrets, and the Combat Log chat tab text was changed so addons cannot parse it. The addon's chat capture is for open-world use only. High.
- **Map position in instances** is `nil`. High.
- **Events that may not exist on Forever.** A client raises an error when an addon registers an event it does not have. `LEARNED_SPELL_IN_TAB` was removed from Mainline in 12.0.0 (its wiki page lists it only up to 12.0.0) and is not tagged for Forever; a Forever addon author reports that registering it throws. The Coach registers every event through `pcall` and also listens to `SPELLS_CHANGED`. Do the same for any event you add. High that the wiki lists it as removed; Medium for the Forever behaviour.
- **Removed globals.** `GetItemInfo`, `GetSpellInfo`, `UnitAura`, `GetContainerItemInfo`, `IsAddOnLoaded` and `GetAddOnMetadata` are absent as plain globals on Forever; use `C_Item`, `C_Spell`, `C_UnitAuras`, `C_Container` and `C_AddOns`. `CombatLogGetCurrentEventInfo` is gone entirely. Medium (addon author's API dump).
- **Detecting Forever:** `WOW_PROJECT_ID` equals the Mainline value on Forever, so it cannot tell the clients apart. Use the interface number (`select(4, GetBuildInfo())` between 16000 and 16999) or probe for the API you need. Medium.
- **Talents** on Forever use the trait system with a Forever-specific config type, not the Classic talent functions. WoW Mentor's talent section degrades to `unknown` when the Classic calls are missing.

## What is not covered

Cooldown tracking details, the auction house, professions and recipes, group content, the Forever-specific Legacy system, and anything about gameplay rules. Also, we have not run these checks against a live Forever client for this report. If you have, please open an issue with the build number and what you saw, and we will update the table.

## Sources, all accessed 2026-11-09

- Warcraft Wiki, pages named in each section above, plus [Patch 12.0.0 API changes](https://warcraft.wiki.gg/wiki/Patch_12.0.0/API_changes/notes) and [Patch 12.0.5](https://warcraft.wiki.gg/wiki/Patch_12.0.5/API_changes).
- [Forever++ notes](https://github.com/xIGBClutchIx/ForeverPlusPlus/blob/main/docs/forever-api.md): a third-party addon author's notes on the Forever client, each fact tagged with how it was checked. We rely on it for Medium-confidence items.
- [WoW Forever addon API summary](https://theforeverguides.com/addons/api/): a fan site that restates Blizzard's statements. Used only as a cross check.
