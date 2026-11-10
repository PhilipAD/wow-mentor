#!/usr/bin/env python3
"""Documentation sanity checks (standard library only, Python 3.9+).

1. Every relative link in a Markdown file points at a file that exists, and `#anchor` parts match a heading.
2. Every `wowmentor <command>` and `wowui <command>` that appears in a code span or code block names a real command.
3. Every ```json block parses; blocks that contain overlay ops are also validated with the real server schema.

Run from anywhere:  python3 .github/scripts/check_docs.py
"""
import json, os, re, subprocess, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)

MD_FILES = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "NOTICE"] + \
    sorted(os.path.join("docs", f) for f in os.listdir(os.path.join(ROOT, "docs")) if f.endswith(".md")) + \
    [".github/PULL_REQUEST_TEMPLATE.md"]
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```(\w*)\s*$")
errors = []


def err(path, msg):
    errors.append("%s: %s" % (path, msg))


def slug(heading):
    """GitHub's heading anchor: lower case, drop punctuation, spaces to dashes."""
    h = re.sub(r"`", "", heading.strip().lower())
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def anchors(path):
    out = set()
    try:
        in_fence = False
        for line in open(path, encoding="utf-8"):
            if line.startswith("```"):
                in_fence = not in_fence
            if not in_fence:
                m = re.match(r"^#{1,6}\s+(.*)$", line)
                if m:
                    out.add(slug(m.group(1)))
    except OSError:
        pass
    return out


def help_commands(argv):
    r = subprocess.run([sys.executable] + argv + ["--help"], capture_output=True, text=True, cwd=ROOT)
    cmds = set()
    for line in r.stdout.splitlines():
        m = re.match(r"^    ([a-z][a-z0-9-]*)(?:\s|$)", line)
        if m:
            cmds.add(m.group(1))
    return cmds


def code_snippets(text):
    """(language, text) of every fenced block, then ('span', text) for every inline code span outside fences."""
    out, cur, lang, in_fence, rest = [], [], "", False, []
    for line in text.splitlines():
        m = FENCE.match(line)
        if m and not in_fence:
            in_fence, lang, cur = True, m.group(1), []
        elif line.startswith("```") and in_fence:
            in_fence = False
            out.append((lang, "\n".join(cur)))
        elif in_fence:
            cur.append(line)
        else:
            rest.append(line)
    for m in re.finditer(r"`([^`\n]+)`", "\n".join(rest)):
        out.append(("span", m.group(1)))
    return out


def check_versions():
    """One version everywhere: wowmentor/__init__.py, the addon .toc and a CHANGELOG entry."""
    from wowmentor import __version__
    toc = open(os.path.join(ROOT, "wowmentor", "addon", "WoWFeed", "WoWFeed.toc"), encoding="utf-8").read()
    m = re.search(r"^## Version:\s*(\S+)", toc, re.M)
    if not m or m.group(1) != __version__:
        err("WoWFeed.toc", "## Version is %s but wowmentor.__version__ is %s" % (m.group(1) if m else "missing", __version__))
    log = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    if not re.search(r"^## \[?%s\b" % re.escape(__version__), log, re.M):
        err("CHANGELOG.md", "no entry for version %s" % __version__)


def main():
    wm = help_commands(["wowmentor.sh"])
    wu = help_commands(["wowui"])
    if not wm or not wu:
        err("setup", "could not read the command lists from --help (wowmentor: %d, wowui: %d)" % (len(wm), len(wu)))
    wm |= {"--help", "--version"}
    from wowmentor.server import uidoc

    for rel in MD_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        base = os.path.dirname(path)
        # 1. links (outside code)
        plain = re.sub(r"```.*?```", "", text, flags=re.S)
        plain = re.sub(r"`[^`\n]*`", "", plain)
        for target in LINK.findall(plain):
            if re.match(r"^(https?:|mailto:)", target):
                continue
            file_part, _, frag = target.partition("#")
            dest = os.path.normpath(os.path.join(base, file_part)) if file_part else path
            if not os.path.exists(dest):
                err(rel, "broken link: %s" % target)
            elif frag and dest.endswith(".md") and frag not in anchors(dest):
                err(rel, "no heading for anchor: %s" % target)
        # 2 and 3. code
        for lang, snippet in code_snippets(text):
            if os.path.basename(rel).startswith("DESIGN"):
                continue                      # design notes may describe commands that are still being built
            for m in re.finditer(r"(?<![\w./-])(?<!-m )(?<!from )wowmentor[ \t]+((?:--)?[a-z][a-z0-9-]*)", snippet):
                word = m.group(1)
                if lang in ("span", "bash", "sh"):
                    if word not in wm and word not in ("is", "has", "was", "will"):
                        err(rel, "unknown command `wowmentor %s`" % word)
            for m in re.finditer(r"(?<![\w./-])wowui[ \t]+([a-z][a-z0-9-]*)", snippet):
                word = m.group(1)
                if lang in ("span", "bash", "sh") and word not in wu:
                    err(rel, "unknown command `wowui %s`" % word)
            if lang == "json":
                try:
                    data = json.loads(snippet)
                except ValueError as e:
                    err(rel, "json block does not parse (%s): %s" % (e, snippet[:60].replace("\n", " ")))
                    continue
                ops = data if isinstance(data, list) else data.get("ops") if isinstance(data, dict) and "ops" in data else [data]
                if isinstance(ops, list) and ops and all(isinstance(o, dict) and "op" in o for o in ops):
                    try:
                        uidoc.apply_ops(uidoc.Doc(), ops)
                    except uidoc.OpError as e:
                        err(rel, "overlay ops are rejected by the server schema: %s" % "; ".join(e.errors))
    check_versions()
    if errors:
        print("\n".join(errors))
        print("\n%d problem(s) found" % len(errors))
        return 1
    print("docs ok: %d files checked" % len(MD_FILES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
