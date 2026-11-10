"""Writes tests/fixtures/sample_doc*.json (wire format) from the uidoc example, for the Lua harness."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import time
time.time = lambda: 1794300000.0      # fixtures must not change between runs
from wowmentor.server import uidoc
here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(here, exist_ok=True)
d = uidoc.Doc()
uidoc.apply_ops(d, uidoc.catalog()["example"] + [
    {"op": "child", "panel": "plan", "component": {"type": "kv", "rows": [["Zone", "{zone}"], ["Gold", "{gold}"]]}},
    {"op": "child", "panel": "plan", "component": {"type": "timer", "text": "Next check", "seconds": 90}},
    {"op": "child", "panel": "plan", "component": {"type": "row", "children": [{"type": "icon", "spell": 686, "text": "Shadow Bolt"}, {"type": "text", "text": "[c=#ff8800]hot[/c] {bags} bags"}]}},
    {"op": "child", "panel": "plan", "component": {"type": "divider"}},
    {"op": "panel", "id": "gear", "title": "Gear", "anchor": "LEFT", "x": 20, "y": 0, "collapsed": True, "children": [{"type": "text", "text": "Pipe | test {item:6948} {quest:7:Kobold}"}]},
])
open(os.path.join(here, "sample_doc.json"), "w").write(d.wire())
uidoc.apply_ops(d, [{"op": "check", "panel": "plan", "item": "wolves", "done": True}, {"op": "toast", "text": "Second doc", "level": "warn", "sound": "warning"},
                    {"op": "action", "name": "chat", "text": "hello {player}"}, {"op": "remove", "panel": "gear"}])
open(os.path.join(here, "sample_doc2.json"), "w").write(d.wire())
print("wrote fixtures, rev", d.rev)
