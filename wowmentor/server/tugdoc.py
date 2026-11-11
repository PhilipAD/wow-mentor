"""Guide-file model for the Guidelime TUGs pack: parse, lint and edit with small ops.

A guide file is `Guidelime.registerGuide(\\n[[\\n<body lines>\\n]], "<pack name>")`. The body is one step per line.
Ops address a line by number (1-based, as `wowui tug show` prints) or by a unique `match` substring, and may carry
an `expect` substring that must be on that line, so an edit can never land on the wrong step after the file moved.
Pure stdlib; used by the feed server (dry run and cache) and by the Mac bridge (the real write).
"""
import hashlib, re

MAX_LINE = 800
MAX_FILE = 60_000
HEADER = re.compile(r"^\[(GA|N|NX|D)[ A-Za-z0-9#]")
KNOWN_TAGS = {
    "A", "QA", "QC", "QT", "G", "TAR", "SK", "S", "P", "F", "H", "T", "O", "N", "NX", "D", "GA", "CI", "SP", "OC", "OAC",
    "V", "R", "U", "L", "XP", "I", "QS", "QW", "LOC", "GT", "DN", "LA",
}
WRAP = re.compile(r'^(?P<pre>[^\[]*?registerGuide\(\s*\r?\n\[\[\r?\n)(?P<body>.*?)(?P<suf>\r?\n\]\],\s*"[^"]*"\)\s*)$', re.S)


class TugError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def split_file(text):
    m = WRAP.match(text)
    if not m:
        raise TugError(["file does not have the Guidelime.registerGuide( [[ ... ]], \"name\") wrapper"])
    eol = "\r\n" if "\r\n" in text else "\n"
    return m.group("pre"), m.group("body").split(eol), m.group("suf"), eol


def join_file(pre, lines, suf, eol):
    return pre + eol.join(lines) + suf


# ---------------------------------------------------------------- lint
def header_counts(lines):
    c = {"N": 0, "NX": 0, "GA": 0}
    for line in lines:
        m = re.match(r"^\[(GA|NX|N)(?=[ A-Za-z0-9#])", line)
        if m:
            c[m.group(1)] += 1
    return c


def lint(lines, base=None):
    """base = header counts of the file before the edit: a file may keep an odd header count it already had, but not gain one."""
    errs, warns = [], []
    headers = header_counts(lines)
    for i, line in enumerate(lines, 1):
        if "[[" in line or "]]" in line:
            errs.append("line %d: contains [[ or ]] which would end the guide text: %s" % (i, line[:70]))
        if line.count("[") != line.count("]"):
            errs.append("line %d: unbalanced [ ]: %s" % (i, line[:70]))
        if len(line) > MAX_LINE:
            errs.append("line %d: %d characters (limit %d)" % (i, len(line), MAX_LINE))
        if "\r" in line or "\n" in line:
            errs.append("line %d: stray newline" % i)
        for tag in re.findall(r"\[([A-Z]{1,3})(?=[^A-Za-z]|\])", line):
            if tag not in KNOWN_TAGS:
                warns.append("line %d: tag [%s...] is not used anywhere in the pack (typo?)" % (i, tag))
    b = base or {"N": 1, "NX": 1, "GA": 1}
    if headers["N"] == 0 and b["N"] >= 1:
        errs.append("the [N<name>] header line is missing")
    if headers["N"] > 1 and b["N"] <= 1:
        errs.append("only one [N<name>] header line is allowed (found %d)" % headers["N"])
    if headers["NX"] > 1 and b["NX"] <= 1:
        errs.append("at most one [NX<next guide>] header line (found %d)" % headers["NX"])
    if headers["GA"] > 1 and b["GA"] <= 1:
        errs.append("at most one [GA <faction>] header line (found %d)" % headers["GA"])
    return errs, sorted(set(warns))[:12]


# ---------------------------------------------------------------- addressing
def _find(lines, op, key="line", mkey="match", required=True):
    if key in op and op[key] is not None:
        n = op[key]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(lines):
            raise TugError(["%s: %r is not a line between 1 and %d" % (key, n, len(lines))])
        idx = n - 1
    elif mkey in op and op[mkey] is not None:
        hits = [i for i, l in enumerate(lines) if op[mkey] in l]
        nth = op.get("nth")
        if not hits:
            raise TugError(["%s: no line contains %r" % (mkey, op[mkey])])
        if len(hits) > 1 and nth is None:
            raise TugError(["%s: %r matches %d lines (%s); add nth (1-based), a longer match, or use line" %
                            (mkey, op[mkey], len(hits), ", ".join("%d: %s" % (h + 1, lines[h][:40]) for h in hits[:6]))])
        if nth is not None:
            if not isinstance(nth, int) or not 1 <= nth <= len(hits):
                raise TugError(["nth: must be 1..%d" % len(hits)])
            idx = hits[nth - 1]
        else:
            idx = hits[0]
    else:
        if required:
            raise TugError(["address the line with `%s` (number) or `%s` (unique text)" % (key, mkey)])
        return None
    exp = op.get("expect")
    if exp is not None and exp not in lines[idx]:
        raise TugError(["expect: line %d is %r, which does not contain %r (re-read the guide)" % (idx + 1, lines[idx][:80], exp)])
    return idx


def _text_lines(op):
    t = op.get("text")
    if not isinstance(t, str) or t == "":
        raise TugError(["text: required (use \\n between lines for several steps)"])
    return [x.rstrip("\r") for x in t.split("\n")]


def _first_body_line(lines):
    for i, l in enumerate(lines):
        if not HEADER.match(l):
            return i
    return len(lines)


# ---------------------------------------------------------------- ops
def apply_ops(lines, ops):
    """Apply ops to a list of lines (returns a new list and a list of change summaries). Atomic."""
    work = list(lines)
    summary = []
    if not isinstance(ops, list) or not ops:
        raise TugError(["ops must be a non-empty list"])
    for n, op in enumerate(ops):
        if not isinstance(op, dict) or "op" not in op:
            raise TugError(["op %d: each op is an object with an \"op\" field (replace, insert, delete, sub, set)" % n])
        try:
            name = op["op"]
            if name == "replace":
                i = _find(work, op)
                new = _text_lines(op)
                work[i:i + 1] = new
                summary.append("replaced line %d with %d line(s)" % (i + 1, len(new)))
            elif name == "insert":
                new = _text_lines(op)
                if op.get("at") == "end":
                    i = len(work)
                elif op.get("at") == "start":
                    i = _first_body_line(work)
                elif any(k in op for k in ("after_line", "after_match")):
                    i = _find(work, op, "after_line", "after_match") + 1
                elif any(k in op for k in ("before_line", "before_match")):
                    i = _find(work, op, "before_line", "before_match")
                else:
                    raise TugError(["insert needs one of after_line, after_match, before_line, before_match, at (start|end)"])
                work[i:i] = new
                summary.append("inserted %d line(s) at line %d" % (len(new), i + 1))
            elif name == "delete":
                i = _find(work, op)
                count = op.get("count", 1)
                if not isinstance(count, int) or not 1 <= count <= 50 or i + count > len(work):
                    raise TugError(["count: 1..50 and within the file"])
                del work[i:i + count]
                summary.append("deleted %d line(s) from line %d" % (count, i + 1))
            elif name == "sub":
                find, rep = op.get("find"), op.get("replace", "")
                if not isinstance(find, str) or not find or not isinstance(rep, str):
                    raise TugError(["sub needs find (text) and replace (text)"])
                if "\n" in rep or "\n" in find:
                    raise TugError(["sub works inside one line; use replace/insert for several lines"])
                i = _find(work, op, required=False)
                if i is not None:
                    if find not in work[i]:
                        raise TugError(["sub: line %d does not contain %r" % (i + 1, find)])
                    work[i] = work[i].replace(find, rep) if op.get("all") else work[i].replace(find, rep, 1)
                    summary.append("changed text on line %d" % (i + 1))
                else:
                    hits = [k for k, l in enumerate(work) if find in l]
                    if not hits:
                        raise TugError(["sub: %r not found anywhere" % find])
                    if len(hits) > 1 and not op.get("all"):
                        raise TugError(["sub: %r is on %d lines (%s); add line/match or all=true" % (find, len(hits), ", ".join(str(h + 1) for h in hits[:8]))])
                    for k in hits:
                        work[k] = work[k].replace(find, rep)
                    summary.append("changed text on %d line(s)" % len(hits))
            elif name == "set":
                work = _text_lines(op)
                summary.append("replaced the whole guide (%d lines)" % len(work))
            else:
                raise TugError(["unknown op %r (use replace, insert, delete, sub, set)" % name])
        except TugError as e:
            raise TugError(["op %d (%s): %s" % (n, op.get("op"), m) for m in e.errors])
    errs, warns = lint(work, header_counts(lines))
    if errs:
        raise TugError(errs[:8])
    return work, summary, warns


def apply_to_text(text, ops):
    pre, lines, suf, eol = split_file(text)
    new, summary, warns = apply_ops(lines, ops)
    out = join_file(pre, new, suf, eol)
    if len(out) > MAX_FILE:
        raise TugError(["guide would be %d bytes; limit %d" % (len(out), MAX_FILE)])
    return out, summary, warns, new


def numbered(lines, first=1, last=None, width=0):
    last = last or len(lines)
    return "\n".join("%4d  %s" % (i, lines[i - 1]) for i in range(max(1, first), min(len(lines), last) + 1))


def context(lines, ops_summary, new_lines, old_lines, pad=1):
    """A small before/after window around what changed, for the agent's reply."""
    a = next((i for i, (x, y) in enumerate(zip(old_lines, new_lines)) if x != y), min(len(old_lines), len(new_lines)))
    b = len(new_lines)
    while b > a and len(old_lines) - (len(new_lines) - b) > a and new_lines[b - 1] == old_lines[len(old_lines) - (len(new_lines) - b) - 1]:
        b -= 1
    return numbered(new_lines, max(1, a + 1 - pad), min(len(new_lines), max(b, a + 1) + pad))


def impact(old, new):
    """What an edit does to a player's Guidelime ticks (stored by step number and step text).

    Guidelime re-matches ticks by text only when the number of steps changed. So:
      shifted      old line ranges that moved to a different number (ticks on them follow only if the step count changed)
      count_same_shifted   True when lines moved but the count is unchanged: Guidelime will NOT re-match, ticks land on the wrong steps
      text_changed old line numbers whose text changed: a tick on such a step is dropped on re-match
    Line numbers are the 1-based numbers `tug show` prints (the player's resolved steps are in the feed GUIDES section)."""
    import difflib
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    shifted, changed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and i1 != j1:
            shifted.append({"old": [i1 + 1, i2], "new_start": j1 + 1})
        elif tag in ("replace", "delete"):
            changed.append([i1 + 1, i2])
    same = len(old) == len(new)
    out = {"old_lines": len(old), "new_lines": len(new), "shifted": shifted[:12], "text_changed": changed[:12]}
    if same and shifted:
        out["count_same_shifted"] = True
        out["warning"] = ("lines moved but the step count is unchanged, so Guidelime will not re-match ticks: the player's ticks on shifted steps land on the wrong steps. "
                          "Change the line count in the same batch (or edit in place) and tell the player to re-check the steps around the edit.")
    elif changed:
        out["warning"] = "text of existing steps changed (old lines %s): a tick or skip on those steps is lost at the next /reload, so do not edit steps the player has already resolved" % ", ".join("%d-%d" % tuple(c) if c[0] != c[1] else str(c[0]) for c in changed[:6])
    return out
