## What does this change, and why?

<!-- A few plain sentences. Link the issue if there is one. -->

## How did you check it?

<!-- Tick what applies. -->

- [ ] `python3 -W error::ResourceWarning -m unittest discover -s tests -t .` passes
- [ ] `python3 tests/make_sample.py` and then `luajit tests/test_ui.lua` / `luajit tests/test_json.lua` (or `lua5.1`) pass, if I touched the addon
- [ ] I tried it on a Mac with the Forever client, or I say below why I could not

## Checklist

- [ ] Python is standard library only and works on Python 3.9 (no `match`, no `X | Y` types, no `dict | dict`)
- [ ] Lua works on Lua 5.1 (no `goto`, no `//`, no bit operators), and every optional WoW API is called through `pcall` or a feature check
- [ ] No personal data in code, tests, docs or examples (no real names, characters, hosts, tokens or links)
- [ ] Level and XP are still never shown on the overlay
- [ ] Nothing sends input to the game or automates play
- [ ] I updated the docs and the CHANGELOG if behaviour changed
- [ ] I did not add a dependency
