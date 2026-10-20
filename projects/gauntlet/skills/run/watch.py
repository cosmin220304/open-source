#!/usr/bin/env python3
"""Local live view of Claude Code work: worktree -> session -> tasks. Reads ~/.claude transcripts, serves http://localhost:7777."""
import glob, json, os, re, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
ROOT = f"{HOME}/.claude/projects"
JOBS = f"{HOME}/.claude/jobs"
RUNS = f"{HOME}/.claude/gauntlet/runs"
CAP = int(os.environ.get("GAUNTLET_CONTEXT_CAP", "300000"))
PORT = 7777
WINDOW = 24 * 3600
STATUS_RE = re.compile(r"^(?:Status|Verdict):\s*([A-Z]+)", re.M)


def short(p): return p.replace(HOME, "~") if p else ""


TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
TICKET_SLUG_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}(?:/[\w.-]+)?")
URL_RE = re.compile(r"https?://\S+")
PATH_RE = re.compile(r"(?<!\w)(?:~|/)[\w.@+-]+(?:/[\w.@+-]+){2,}")
NOISE_RE = re.compile(r"[`*_#>|]+")
CODE_RE = re.compile(r"^\s*\(?(?:WITH|SELECT|FROM|WHERE|JOIN|AND|OR|ON|GROUP|ORDER|LIMIT|CASE|WHEN|THEN|ELSE|END|INSERT|UPDATE|DELETE|CREATE|import|export|const|def|class|function|\{|\[|<|```|--|//|#)\b|\bAS\b|[{}();]{2,}|'\{|::|->|=>|@>|jsonb_", re.I)
TAG_RE = re.compile(r"<([a-zA-Z][\w-]*)[^>]*>[\s\S]*?</\1>")
CMD_RE = re.compile(r"<command-name>(.*?)</command-name>(?:[\s\S]*?<command-args>(.*?)</command-args>)?", re.S)


def title_of(prompt):
    """Readable session name from the first prompt."""
    text = prompt
    tickets = list(dict.fromkeys(TICKET_RE.findall(text)))[:2]
    slug = next((m.group(0).split("/", 1)[1] for m in TICKET_SLUG_RE.finditer(text) if "/" in m.group(0)), "")
    text = URL_RE.sub("", text)
    text = PATH_RE.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], text)
    text = TICKET_SLUG_RE.sub("", text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    prose = [l for l in lines if not CODE_RE.search(l)]
    mostly_code = lines and len(prose) < len(lines) * 0.3
    pool = [l for l in (reversed(prose) if mostly_code else prose) if len(l.split()) >= 3]
    line = pool[0] if pool else (prose[0] if prose else "")
    if mostly_code: line = ("Pasted code · " + line) if line else "Pasted code"
    line = " ".join(NOISE_RE.sub(" ", line).split()).strip(" -:/,.")
    if len(line.split()) < 3 and slug: line = (line + " " + slug.replace("-", " ")).strip()
    if len(line) > 64:
        cut = line[:64]
        line = cut[:max(cut.rfind(" "), 40)].rstrip(" ,.;:") + "…"
    line = line[:1].upper() + line[1:] if line else "Untitled"
    return (" · ".join(tickets) + " · " + line) if tickets else line


def worktree_label(cwd):
    """('shop', 'SHOP-142-refund-flow') for a worktree; ('shop', '') for a plain checkout."""
    m = re.search(r"/([^/]+)/\.claude/worktrees/([^/]+)$", cwd)
    if m: return m.group(1), m.group(2)
    return os.path.basename(cwd.rstrip("/")) or cwd, ""


def brief(tool, inp):
    if tool == "Bash": return inp.get("description") or inp.get("command", "")
    if tool in ("Read", "Edit", "Write", "MultiEdit"): return short(inp.get("file_path", ""))
    if tool == "Grep": return f"{inp.get('pattern', '')}  {short(inp.get('path', ''))}".strip()
    if tool == "Glob": return inp.get("pattern", "")
    if tool == "Agent": return inp.get("description", "")
    if tool == "ToolSearch": return inp.get("query", "")
    vals = [v for v in inp.values() if isinstance(v, str) and v]
    return (vals[0] if vals else json.dumps(inp))[:140]


def fmt_k(n): return f"{n / 1000:.0f}k"


def compact_event(d):
    cm = d.get("compactMetadata", {})
    return {"t": d.get("timestamp", ""), "k": "compact", "v": f"{cm.get('trigger', '')} · {fmt_k(cm.get('preTokens', 0))} → {fmt_k(cm.get('postTokens', 0))}".lstrip(" ·")}


def parse_task(path):
    meta_path = path.replace(".jsonl", ".meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    events, usage, turns, tools, first, last, last_text, last_kind = [], {}, 0, 0, "", "", "", ""
    stopped = cap = False; compactions = 0
    with open(path) as f:
        for line in f:
            try: d = json.loads(line)
            except ValueError: continue
            m = d.get("message", {})
            iso = d.get("timestamp", "")
            first = first or iso
            last = iso or last
            if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                compactions += 1; events.append(compact_event(d)); continue
            if d.get("type") == "user":
                c = m.get("content")
                if isinstance(c, list) and c and c[0].get("type") == "tool_result":
                    last_kind = "tool_result"
                    if "CONTEXT CAP REACHED" in str(c[0].get("content", "")): cap = True
                elif "[Request interrupted by user]" in (c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))):
                    stopped = True; last_kind = "stopped"
                continue
            if d.get("type") != "assistant": continue
            if "usage" in m: usage = m["usage"]; turns += 1
            for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
                if b.get("type") == "tool_use":
                    tools += 1; last_kind = "tool_use"
                    events.append({"t": iso, "k": b["name"], "v": brief(b["name"], b.get("input", {}))})
                elif b.get("type") == "text" and b.get("text", "").strip():
                    last_text = b["text"].strip(); last_kind = "text"
                    events.append({"t": iso, "k": "text", "v": last_text[:400]})
    ctx = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    age = time.time() - os.path.getmtime(path)
    running = last_kind == "tool_use" or (age < 30 and not stopped)
    m = STATUS_RE.search(last_text)
    status = m.group(1) if m else "RUNNING" if running else "STOPPED" if stopped else "NOREPORT" if cap else "ENDED"
    if status == "SPLIT": status = "HANDOFF"
    reason = "" if running else "interrupted" if stopped else "hit the context cap, no handoff written" if cap and not m else ""
    if compactions: reason = (f"{reason} · " if reason else "") + f"compacted {compactions}x"
    return {
        "reason": reason,
        "id": os.path.basename(path)[6:-6],
        "type": meta.get("agentType", "").split(":")[-1],
        "name": meta.get("description", "") or os.path.basename(path)[6:-6],
        "started": first, "ended": last, "mtime": os.path.getmtime(path),
        "running": running,
        "status": status,
        "context": ctx, "turns": turns, "tools": tools,
        "last_text": last_text, "events": events[-40:],
    }


def session_head(path):
    """cwd, branch, first prompt, last activity from a main session transcript."""
    cwd = branch = title = ""
    with open(path) as f:
        for line in f:
            try: d = json.loads(line)
            except ValueError: continue
            cwd = cwd or d.get("cwd", ""); branch = branch or d.get("gitBranch", "")
            c = d.get("message", {}).get("content")
            if not title and d.get("type") == "user" and isinstance(c, str):
                cmd = CMD_RE.search(c)
                if cmd:
                    args = " ".join((cmd.group(2) or "").split())
                    if args: title = cmd.group(1).strip() + " " + args[:60]
                else:
                    plain = TAG_RE.sub("", c).strip()
                    if plain: title = title_of(plain)
            if cwd and title: break
    return cwd, branch, title


STOP_CACHE = {}


TURN_RE = re.compile(r"<task-id>([0-9a-f]+)</task-id>.{0,400}?stopped at its (\d+)-turn limit", re.S)


def task_notes(path):
    """What the parent session did to or heard about each subagent: {'stopped': ids, 'turns': {id: limit}}."""
    key = (path, os.path.getsize(path))
    if key in STOP_CACHE: return STOP_CACHE[key]
    notes = {"stopped": [], "turns": {}}
    with open(path, "rb") as f:
        for line in f:
            if b'"name":"TaskStop"' in line:
                try: d = json.loads(line)
                except ValueError: continue
                for b in d.get("message", {}).get("content", []) if isinstance(d.get("message", {}).get("content"), list) else []:
                    if b.get("type") == "tool_use" and b.get("name") == "TaskStop": notes["stopped"].append(b.get("input", {}).get("task_id", ""))
            elif b"-turn limit" in line:
                m = TURN_RE.search(line.decode("utf-8", "replace").replace("\\n", "\n"))
                if m: notes["turns"][m.group(1)] = int(m.group(2))
    STOP_CACHE.clear(); STOP_CACHE[key] = notes
    return notes


def session_tail(path, limit=256 * 1024):
    """Recent activity of a main session: last events, running state, last activity time."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > limit: f.seek(size - limit); f.readline()
        chunk = f.read().decode("utf-8", "replace")
    events, last, last_kind, usage, title, summary = [], "", "", {}, "", ""
    for line in chunk.splitlines():
        try: d = json.loads(line)
        except ValueError: continue
        if d.get("isSidechain"): continue
        if d.get("type") == "ai-title": title = d.get("aiTitle") or title; continue
        m = d.get("message", {}); iso = d.get("timestamp", ""); last = iso or last
        if d.get("type") == "system":
            if d.get("subtype") == "compact_boundary": events.append(compact_event(d))
            elif d.get("subtype") == "away_summary": summary = d.get("content", "").split(" (disable recaps")[0]
            continue
        if d.get("type") == "user":
            c = m.get("content")
            if isinstance(c, list) and c and c[0].get("type") == "tool_result": last_kind = "tool_result"
            elif isinstance(c, str): last_kind = "prompt"
            continue
        if d.get("type") != "assistant": continue
        if "usage" in m: usage = m["usage"]
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use":
                last_kind = "tool_use"; events.append({"t": iso, "k": b["name"], "v": brief(b["name"], b.get("input", {}))})
            elif b.get("type") == "text" and b.get("text", "").strip():
                last_kind = "text"; events.append({"t": iso, "k": "text", "v": b["text"].strip()[:400]})
    ctx = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    age = time.time() - os.path.getmtime(path)
    return {"events": events[-20:], "running": last_kind == "tool_use" or age < 30, "ended": last, "context": ctx,
            "title": title, "notes": task_notes(path), "summary": summary}


def jobs_by_session():
    out = {}
    for sp in glob.glob(f"{JOBS}/*/state.json"):
        try: s = json.load(open(sp))
        except ValueError: continue
        link = s.get("linkScanPath", "")
        if not link: continue
        sid = os.path.basename(link)[:-6]
        out[sid] = {"id": os.path.basename(os.path.dirname(sp)), "state": s.get("state", ""), "detail": s.get("detail", ""),
                    "tokens": s.get("tokens", 0), "mtime": os.path.getmtime(sp)}
    return out


def git(cwd, *args, timeout=15):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else ""


LANGS = {"kt": "kotlin", "kts": "kotlin", "java": "java", "ts": "typescript", "tsx": "tsx", "js": "javascript", "jsx": "jsx", "py": "python",
         "sql": "sql", "json": "json", "yml": "yaml", "yaml": "yaml", "md": "markdown", "html": "html", "css": "css", "sh": "bash", "zsh": "bash",
         "go": "go", "rs": "rust", "rb": "ruby", "xml": "xml", "toml": "toml", "gradle": "groovy", "properties": "ini"}
MAX_CONTENT = 400_000


def read_text(path):
    try:
        with open(path, "rb") as f: b = f.read(MAX_CONTENT + 1)
    except OSError: return None
    if len(b) > MAX_CONTENT or b"\x00" in b: return None
    return b.decode("utf-8", "replace")


def changes(cwd):
    """Uncommitted changes in the working tree (vs HEAD), untracked files included. One entry per file."""
    if not cwd or not os.path.isdir(cwd) or not git(cwd, "rev-parse", "--is-inside-work-tree").strip():
        return {"error": "not a git repo"}
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    patch = git(cwd, "diff", "-U5", "--no-color", "HEAD")
    untracked = git(cwd, "ls-files", "--others", "--exclude-standard").splitlines()
    for f in untracked:
        r = subprocess.run(["git", "-C", cwd, "diff", "-U5", "--no-color", "--no-index", "/dev/null", f], capture_output=True, text=True)
        patch += r.stdout
    numstat = {}
    for line in git(cwd, "diff", "--numstat", "HEAD").splitlines():
        a, d, f = line.split("\t", 2); numstat[f] = (a, d)
    files, total = [], 0
    for chunk in re.split(r"^(?=diff --git )", patch, flags=re.M):
        if not chunk.startswith("diff --git"): continue
        m = re.match(r"diff --git a/(.*?) b/(.*)\n", chunk)
        if not m: continue
        old_name, new_name = m.group(1), m.group(2)
        status = "A" if "\n--- /dev/null" in chunk or new_name in untracked else "D" if "\n+++ /dev/null" in chunk else "R" if old_name != new_name else "M"
        a, d = numstat.get(new_name, ("", ""))
        if status == "A" and not a: a = "-" if "\nBinary files " in chunk else str(chunk.count("\n+") - 1)
        old = read_text_git(cwd, old_name) if status != "A" else ""
        new = read_text(os.path.join(cwd, new_name)) if status != "D" else ""
        total += len(chunk)
        files.append({"path": new_name, "oldPath": old_name, "status": status, "add": a, "del": d,
                      "lang": LANGS.get(new_name.rsplit(".", 1)[-1].lower(), ""), "old": old, "new": new,
                      "patch": chunk if total < 3_000_000 else ""})
    return {"branch": branch, "files": files, "truncated": total >= 3_000_000}


def read_text_git(cwd, path):
    r = subprocess.run(["git", "-C", cwd, "show", f"HEAD:{path}"], capture_output=True)
    b = r.stdout
    if r.returncode or len(b) > MAX_CONTENT or b"\x00" in b: return None
    return b.decode("utf-8", "replace")


def register(sid, cwd, name=""):
    os.makedirs(RUNS, exist_ok=True)
    path = f"{RUNS}/{sid}.json"
    old = json.load(open(path)) if os.path.exists(path) else {}
    json.dump({"session": sid, "cwd": os.path.abspath(cwd), "name": name or old.get("name", ""), "at": old.get("at") or time.time()}, open(path, "w"))


def forget(cwd):
    """Drop every registration for a checkout. Transcripts stay; the next run there registers it again."""
    for rp in glob.glob(f"{RUNS}/*.json"):
        try: run = json.load(open(rp))
        except ValueError: continue
        if run.get("cwd") == cwd: os.remove(rp)


def snapshot():
    """Registered gauntlet runs, the latest one per checkout, grouped by checkout."""
    now = time.time()
    jobs = jobs_by_session()
    trees = {}
    latest = {}
    for rp in glob.glob(f"{RUNS}/*.json"):
        try: run = json.load(open(rp))
        except ValueError: continue
        cwd = run.get("cwd", "")
        if run.get("session") and cwd and run.get("at", 0) > latest.get(cwd, ({}, ""))[0].get("at", 0): latest[cwd] = (run, rp)
    for run, rp in latest.values():
        sid, cwd = run["session"], run["cwd"]
        paths = glob.glob(f"{ROOT}/*/{sid}.jsonl")
        path = paths[0] if paths else ""
        tasks = {}
        for tp in glob.glob(f"{ROOT}/*/{sid}/subagents/agent-*.jsonl"):
            t = parse_task(tp)
            if t["id"] not in tasks or t["mtime"] > tasks[t["id"]]["mtime"]: tasks[t["id"]] = t
        tasks = sorted(tasks.values(), key=lambda t: t["started"])
        mtime = max([os.path.getmtime(rp)] + ([os.path.getmtime(path)] if path else []) + [t["mtime"] for t in tasks])
        if now - mtime > WINDOW: continue
        _, _, title = session_head(path) if path else ("", "", "")
        main = session_tail(path) if path else {"events": [], "running": False, "ended": "", "context": 0, "title": "", "notes": {"stopped": [], "turns": {}}, "summary": ""}
        title = run.get("name") or (title if title.startswith("/") else main["title"] or title)
        for t in tasks:
            if t["id"] in main["notes"]["stopped"]: t["reason"] = t["reason"].replace("interrupted", "stopped by orchestrator")
            if t["id"] in main["notes"]["turns"] and t["status"] == "ENDED":
                t["status"] = "NOREPORT"; t["reason"] = f"hit the {main['notes']['turns'][t['id']]}-turn limit, no handoff written"
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip() if os.path.isdir(cwd) else ""
        sess = {"id": sid, "title": title or sid[:8], "branch": branch, "job": jobs.get(sid), "main": main,
                "tasks": tasks, "running": sum(t["running"] for t in tasks) + (1 if main["running"] else 0),
                "started": tasks[0]["started"] if tasks else main["ended"], "mtime": mtime}
        repo, wt = worktree_label(cwd)
        w = trees.setdefault(cwd, {"path": short(cwd), "cwd": cwd, "repo": repo, "worktree": wt, "sessions": [], "mtime": 0})
        w["sessions"].append(sess); w["mtime"] = max(w["mtime"], sess["mtime"])
    for w in trees.values(): w["sessions"].sort(key=lambda s: -s["mtime"])
    return {"cap": CAP, "now": now, "worktrees": sorted(trees.values(), key=lambda w: -w["mtime"])}


PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>gauntlet</title>
<link rel=icon href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23161b22'/%3E%3Crect x='.5' y='.5' width='31' height='31' rx='6.5' fill='none' stroke='%233d444d'/%3E%3Ccircle cx='16' cy='16' r='9.5' fill='none' stroke='%233fb950' stroke-opacity='.35' stroke-width='1.5'/%3E%3Ccircle cx='16' cy='16' r='5' fill='%233fb950'/%3E%3C/svg%3E">
<link rel=stylesheet href="https://cdn.jsdelivr.net/npm/@git-diff-view/react@0.1.7/dist/css/diff-view.css">
<script type=module>
import React from "https://esm.sh/react@18.3.1";
import { createRoot } from "https://esm.sh/react-dom@18.3.1/client?deps=react@18.3.1";
import { DiffView, DiffModeEnum } from "https://esm.sh/@git-diff-view/react@0.1.7?deps=react@18.3.1,react-dom@18.3.1";
window.GDV = { React, createRoot, DiffView, DiffModeEnum };
window.dispatchEvent(new Event("gdv-ready"));
</script>
<style>
:root{
  --bg:#0d1117;--panel:#161b22;--raise:#1c2230;--line:#21262d;--line-2:#3d444d;
  --ink:#e6edf3;--ink-2:#9198a1;--ink-3:#848d97;
  --run:#3fb950;--run-dim:#1b3626;--warn:#d29922;--bad:#f85149;--tool:#58a6ff;
  --ok-bg:#12261e;--ok-line:#238636;--warn-bg:#2b2111;--warn-line:#9e6a03;--bad-bg:#2c1518;--bad-line:#da3633;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:var(--line-2) transparent}
html{color-scheme:dark;background:var(--bg)}
:root{--nav:300px}
body{margin:0;background:var(--bg);color:var(--ink);font:13.5px/1.45 var(--sans);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;height:100vh;display:grid;grid-template-columns:var(--nav) minmax(0,1fr);overflow:hidden}
#grip{position:fixed;top:0;bottom:0;left:max(0px,calc(var(--nav) - 3px));width:6px;cursor:col-resize;z-index:3}
#grip:hover,#grip.on{background:linear-gradient(to right,transparent 2px,var(--line-2) 2px,var(--line-2) 4px,transparent 4px)}
body.dragging{cursor:col-resize;user-select:none}
#navopen{position:fixed;top:10px;left:10px;z-index:4;width:28px;height:28px;border-radius:6px;border:1px solid var(--line-2);background:var(--panel);color:var(--ink-2);font-size:14px;line-height:26px;text-align:center;display:none;box-shadow:0 2px 8px rgba(0,0,0,.4)}
#navopen:hover{color:var(--ink);background:var(--raise)}
body.navclosed #navopen{display:block}
body.navclosed main{padding-left:48px}
body.dragging main,body.dragging nav{pointer-events:none}
::selection{background:var(--run-dim);color:var(--ink)}
:focus-visible{outline:1.5px solid var(--run);outline-offset:2px;border-radius:4px}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;text-align:left}
b{font-weight:600}

/* tree */
nav{border-right:1px solid var(--line);background:var(--panel);overflow-y:auto;overflow-x:hidden;padding:14px 0 24px;min-width:0}
nav h1{font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);margin:0 0 10px;padding:0 16px}
.wt{border-top:1px solid var(--line)}
.wt summary{list-style:none;cursor:pointer;padding:10px 16px 10px 12px;display:flex;align-items:baseline;gap:8px;color:var(--ink)}
.wt summary::-webkit-details-marker{display:none}
.wt summary:hover{background:var(--raise)}
.wt summary .ch{width:10px;flex:none;color:var(--ink-3);font-size:10px;transition:transform .15s ease-out;display:inline-block}
.wt[open] summary .ch{transform:rotate(90deg)}
.wt summary .repo{font-weight:600}
.wt summary .br{font-family:var(--mono);font-size:11.5px;color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.wt summary .cnt{margin-left:auto;color:var(--ink-3);font-size:12px;flex:none;display:flex;align-items:center;gap:6px}
.wt summary .rm{align-self:center;flex:none;display:flex;padding:3px;margin:-3px -6px -3px 0;border-radius:4px;color:var(--ink-3);opacity:0}
.wt summary:hover .rm,.wt summary .rm:focus-visible{opacity:1}
.wt summary .rm:hover{color:var(--bad);background:var(--line)}
.wt .body{padding-bottom:6px}
.sess{display:block;width:100%;padding:7px 16px 7px 30px;border-left:2px solid transparent}
.sess:hover{background:var(--raise)}
.sess[aria-current=true]{background:var(--raise);border-left-color:var(--run)}
.sess .t{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
.sess .m{display:flex;gap:8px;align-items:center;margin-top:3px;font-size:12px;color:var(--ink-3)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ink-3);flex:none}
.dot.on{background:var(--run);box-shadow:0 0 0 3px var(--run-dim)}
.dot.bad{background:var(--bad)}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:var(--raise);border:1px solid var(--line-2);color:var(--ink-2);font-family:var(--mono)}
.tag.bg{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.tag.bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-bg);font-family:var(--sans)}

/* main */
main{overflow-y:auto;padding:22px 32px 64px}
.top{display:flex;align-items:baseline;gap:12px;margin-bottom:0;min-width:0}
.top h2{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:0;max-width:70ch;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top .meta{color:var(--ink-3);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
.top .meta .br{font-family:var(--mono);font-size:11.5px}
.job{margin:10px 0 26px;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel);display:flex;gap:14px;align-items:baseline;font-size:13px}
.job .st{color:var(--warn);font-weight:600;font-size:12px;letter-spacing:.04em;text-transform:uppercase}
.job .d{color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job .tk{margin-left:auto;color:var(--ink-3);white-space:nowrap}
h3{font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);margin:26px 0 10px}
h3 span{font-weight:500;letter-spacing:0;text-transform:none;margin-left:8px}
.empty{color:var(--ink-2);max-width:56ch;padding:30px 0}
.empty code{font-family:var(--mono);font-size:12.5px;background:var(--raise);border:1px solid var(--line-2);padding:1px 6px;border-radius:4px}

/* views */
.views{display:flex;gap:18px;margin:8px 0 4px;border-bottom:1px solid var(--line)}
.views button{padding:8px 0 9px;color:var(--ink-3);border-bottom:1.5px solid transparent;margin-bottom:-1px;font-weight:500}
.views button:hover{color:var(--ink-2)}
.views button[aria-selected=true]{color:var(--ink);border-color:var(--ink)}
.views button span{color:var(--ink-3);margin-left:6px;font-weight:400}
.chg-head{display:flex;gap:14px;align-items:baseline;margin:16px 0 12px;font-size:12.5px;color:var(--ink-2)}
.chg-head code{font-family:var(--mono);color:var(--ink)}
.chg-head .r{margin-left:auto;display:flex;gap:10px;align-items:baseline}
.chg-head .a{color:var(--run)}.chg-head .d{color:var(--bad)}.chg-head .w{color:var(--warn)}

.chg-head button{color:var(--ink-3);padding:4px 6px;border:1px solid var(--line-2);border-radius:5px;display:inline-flex}
.chg-head button:hover{color:var(--ink)}
.chg-head button[aria-pressed=true]{color:var(--ink);border-color:var(--ink-3);background:var(--raise)}
.i{display:inline-flex}
#diff{margin-top:6px}
.files{margin:0 0 10px;padding:0;list-style:none;border:1px solid var(--line);border-radius:8px;background:var(--panel);max-height:20vh;overflow:auto}
.fr{display:flex;width:100%;gap:8px;align-items:center;padding:3px 10px;border-left:2px solid transparent;font-size:12px;line-height:1.4}
.fr+.fr{border-top:1px solid var(--line)}
.fr:hover{background:var(--raise)}
.fr[aria-current=true]{background:var(--raise);border-left-color:var(--tool)}
.fp{font-family:var(--mono);color:var(--ink);white-space:nowrap;min-width:0;display:flex}
.fp .dir{color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;min-width:0}
.fs{font-size:10.5px;font-weight:600;letter-spacing:.04em;padding:1px 5px;border-radius:3px;border:1px solid var(--line-2);color:var(--ink-2);flex:none;font-family:var(--sans)}
.fn{margin-left:auto;flex:none;color:var(--ink-3);font-size:12px}
.fn b{font-weight:500}.fn .a{color:var(--run)}.fn .d{color:var(--bad)}
.f{border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:hidden}
.f .fh{display:flex;gap:10px;align-items:baseline;padding:8px 12px;background:var(--raise);border-bottom:1px solid var(--line);font-size:12.5px}
.f .pg{display:flex;gap:2px;align-items:center;margin-left:14px;flex:none;color:var(--ink-3);font-size:12px}
.f .pg button{padding:0 7px;color:var(--ink-2);border-radius:4px;line-height:20px}
.f .pg button:hover{background:var(--line)}.f .pg button:disabled{color:var(--line-2);cursor:default;background:none}
.f .pg span{padding:0 4px}
.fs.A{color:var(--run);border-color:var(--ok-line)}.fs.D{color:var(--bad);border-color:var(--bad-line)}.fs.R{color:var(--warn);border-color:var(--warn-line)}
.f .body{overflow:auto;flex:0 1 auto;min-height:0}
main.changes{display:flex;flex-direction:column;overflow:hidden;padding-bottom:20px}
main.changes .top,main.changes .views,main.changes .chg-head,main.changes .files{flex:none}
main.changes #diff{display:flex;flex-direction:column;flex:1;min-height:0}
main.changes #dv,main.changes .f{display:flex;flex-direction:column;flex:0 1 auto;min-height:0}
.f.full{position:fixed;inset:0;z-index:10;border:0;border-radius:0;display:flex;flex-direction:column;background:var(--bg)}
.f.full .body{flex:1}
.f .pg button{display:inline-flex;padding:2px 4px;line-height:0}
.f .pg .fs-btn{margin-left:6px}
.vw{display:flex;gap:6px;align-items:center;flex:none;margin-left:12px;font-size:12px;color:var(--ink-2);cursor:pointer;user-select:none}
.vw input{margin:0;accent-color:var(--run);cursor:pointer}
.fr .ck{flex:none;width:12px;align-self:center;display:flex;color:var(--run);visibility:hidden}
.fr.read .ck{visibility:visible}
.fr.read .fp,.fr.read .fp .dir{color:var(--ink-3)}
.f .bin{padding:12px;color:var(--ink-3);font-size:12.5px}
.raw{margin:0;padding:8px 0;font-family:var(--mono);font-size:12px;line-height:1.5}
.raw span{display:block;padding:0 12px;white-space:pre}
.raw .a{background:var(--ok-bg);color:#aff5b4}.raw .d{background:var(--bad-bg);color:#ffdcd7}.raw .h{color:var(--tool)}
.chg-empty{color:var(--ink-2);padding:24px 0}

/* running task */
.live{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 18px 13px;margin-bottom:10px}
.live .h{display:flex;align-items:baseline;gap:10px}
.live .n{font-weight:600;font-size:14.5px}
.live .ty{color:var(--ink-3);font-size:12.5px}
.live .el{margin-left:auto;color:var(--ink-3);font-size:12.5px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--run);position:relative;top:-1px;flex:none}
.pulse::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:1.5px solid var(--run);opacity:0;animation:ring 1.6s cubic-bezier(.16,1,.3,1) infinite}
@keyframes ring{0%{transform:scale(.5);opacity:.8}100%{transform:scale(1.5);opacity:0}}
.now{margin:12px 0 12px;font-family:var(--mono);font-size:13px;display:grid;grid-template-columns:minmax(0,1fr);gap:5px}
.now div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.now b{color:var(--tool);margin-right:10px;font-weight:600}
.now .said b{color:var(--ink-2)}
.now .prev{color:var(--ink-3)}.now .prev b{color:var(--ink-3)}
.now .compact b,.tl .compact b{color:var(--warn)}
.meter{display:flex;align-items:center;gap:12px;font-size:12.5px;color:var(--ink-2)}
.bar{flex:1;max-width:320px;height:4px;background:var(--line-2);border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;width:100%;background:var(--run);transform-origin:left;transition:transform .3s cubic-bezier(.16,1,.3,1)}
.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
.meter .c{color:var(--ink-3)}
.wait{background:var(--panel);border:1px solid var(--line-2);border-radius:10px;padding:12px 18px;font-size:14px;line-height:1.55;max-width:90ch;overflow-wrap:anywhere}
.wait.idle{border-color:var(--tool)}
.wait b{display:block;font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--tool);margin-bottom:4px}
#status{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:20;padding:6px 14px;border-radius:6px;border:1px solid var(--warn-line);background:var(--warn-bg);color:var(--warn);font-size:12.5px;display:none}
body.stale #status{display:block}
body.stale main,body.stale nav{opacity:.55}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* finished tasks */
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
td{padding:9px 14px;border-top:1px solid var(--line);vertical-align:top}
tbody tr:first-child td{border-top:0}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--raise)}
tr.row[aria-expanded=true] td{background:var(--raise)}
td.num{text-align:right;white-space:nowrap;color:var(--ink-2)}
td.name{font-weight:550;width:100%}
td.name span{color:var(--ink-3);font-weight:400;margin-left:8px;font-size:12.5px}
td.name .why{display:block;margin:2px 0 0;color:var(--warn);font-size:12px}
.pill{display:inline-block;min-width:58px;text-align:center;font-size:11px;font-weight:600;letter-spacing:.04em;padding:2px 7px;border-radius:4px;background:var(--raise);border:1px solid var(--line-2);color:var(--ink-2)}
.pill.DONE,.pill.PASS{color:var(--run);border-color:var(--ok-line);background:var(--ok-bg)}
.pill.HANDOFF{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.pill.NOREPORT{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.pill.FAILED,.pill.FAIL,.pill.BLOCKED,.pill.STOPPED{color:var(--bad);border-color:var(--bad-line);background:var(--bad-bg)}
.pill[title]{cursor:help}
tr.detail td{background:var(--raise);padding:12px 16px 14px}
.tabs{display:flex;gap:14px;margin:0 0 10px;font-size:12.5px}
.tabs button{color:var(--ink-3);padding:2px 0;border-bottom:1.5px solid transparent}
.tabs button[aria-selected=true]{color:var(--ink);border-color:var(--ink)}
.report{font-family:var(--mono);font-size:12.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word;margin:0;max-height:380px;overflow:auto}
.tl{margin:0;padding:0;list-style:none;font-family:var(--mono);font-size:12.5px;line-height:1.6}
.tl li{display:grid;grid-template-columns:64px 76px 1fr;gap:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tl .t{color:var(--ink-3)}.tl b{color:var(--tool)}.tl .said b{color:var(--ink-3)}.tl .said span{white-space:normal;color:var(--ink-2)}
@media (max-width:820px){body{grid-template-columns:minmax(0,1fr);grid-template-rows:auto 1fr}nav{border-right:0;border-bottom:1px solid var(--line);max-height:40vh}main{padding:16px}#grip{display:none}}
@media (prefers-reduced-motion:reduce){.pulse::after{animation:none}.bar i{transition:none}}
</style>
<nav id=nav></nav>
<div id=grip role=separator tabindex=0 aria-orientation=vertical aria-label="resize worktrees panel" title="drag or use arrow keys; double-click to hide"></div>
<button id=navopen title="show worktrees" aria-label="show worktrees">»</button>
<main id=main></main>
<div id=status role=status></div>
<div id=announce class=sr aria-live=polite></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const open=new Set(), tab={}; let sel=location.hash.slice(1)||null, data=null;
let view=new URLSearchParams(location.search).get('view')||'tasks', diffMode='split', diffKey='', diffCache=null, diffCwd='', diffSel=new URLSearchParams(location.search).get('file')||'', diffFull=false;
// Test files: test/spec dirs, foo.test.ts / foo_spec.rb / test_foo.py, FooTest.kt / FooTests.swift / foo_test.go.
const TEST=/(^|\/)(__)?(tests?|specs?)(__)?\/|[._](test|spec)s?\.\w+$|(^|\/)test_[^/]*$|Tests?\.\w+$/;
let hideTests=(()=>{try{return localStorage.getItem('hideTests')==='1'}catch(e){return false}})();
// Files left after the tests filter; shown() also drops viewed files unless "show viewed" is on.
let showViewed=false;
const base=()=>diffCache.files.filter(f=>!hideTests||!TEST.test(f.path));
const shown=()=>base().filter(f=>showViewed||!isRead(f));
// Viewed files, keyed by checkout + path and tied to a hash of the patch, so a file that changes again is unread.
const read=(()=>{try{return JSON.parse(localStorage.getItem('read')||'{}')}catch(e){return {}}})();
const fhash=f=>f._h??=[...(f.patch||f.add+f.del)].reduce((h,c)=>(h*31+c.charCodeAt(0))|0,0);
const rkey=f=>diffCwd+'\0'+f.path;
const isRead=f=>read[rkey(f)]===fhash(f);
function toggleRead(f){
  const fs=shown(), i=fs.indexOf(f);
  if(isRead(f)) delete read[rkey(f)]; else read[rkey(f)]=fhash(f);
  try{localStorage.setItem('read',JSON.stringify(read))}catch(e){}
  const next=isRead(f)&&[...fs.slice(i+1),...fs.slice(0,i)].find(x=>!isRead(x));
  if(next) diffSel=next.path;
  paintDiff();
}
const viewed=f=>`<label class=vw title="Viewed (v)"><input type=checkbox data-read ${isRead(f)?'checked':''}>Viewed</label>`;
const fmtK=n=>n>=1e6?(n/1e6).toFixed(1).replace(/\.0$/,'')+'M':n>=1000?(n/1000).toFixed(n>=10000?0:1).replace(/\.0$/,'')+'k':String(n);
const hhmm=iso=>iso?new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
const dur=(a,b)=>{const s=Math.max(0,Math.round((new Date(b)-new Date(a))/1000));return s<60?s+'s':s<3600?Math.floor(s/60)+'m':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m'};
const ago=(now,iso)=>{const s=Math.max(0,Math.round(now-new Date(iso)/1000));return s<60?'now':s<3600?Math.floor(s/60)+'m ago':s<172800?Math.floor(s/3600)+'h ago':Math.floor(s/86400)+'d ago'};
// Clock time today, weekday + time this week, date before that.
const when=iso=>{if(!iso)return '';const d=new Date(iso),age=(Date.now()-d)/864e5;return d.toDateString()===new Date().toDateString()?hhmm(iso):age<6?d.toLocaleDateString([],{weekday:'short'})+' '+hhmm(iso):d.toLocaleDateString([],{month:'short',day:'numeric'})};
const STATUS={DONE:'finished and reported done',PASS:'check passed',FAILED:'reported a failure',FAIL:'check failed',BLOCKED:'could not continue, needs a decision',STOPPED:'interrupted before it finished',HANDOFF:'handed the rest of the work to a later task',NOREPORT:'ran out of turns before writing a report',ENDED:'ended without a status line in its report'};
const BAD=/FAIL|BLOCKED|NOREPORT|HANDOFF/;
const bad=s=>s.tasks.filter(t=>!t.running&&BAD.test(t.status)).length;
const num=(f,k)=>f[k]==='-'?'':(f[k]||0);

const openWt=(()=>{try{return new Set(JSON.parse(localStorage.getItem('wt')||'[]'))}catch(e){return new Set()}})();
const saveWt=()=>{try{localStorage.setItem('wt',JSON.stringify([...openWt]))}catch(e){}};
function nav(d){
  return `<h1>Worktrees</h1>`+d.worktrees.map(w=>{
    const has=w.sessions.some(s=>s.id===sel), run=w.sessions.reduce((n,s)=>n+s.running,0), nbad=w.sessions.reduce((n,s)=>n+bad(s),0);
    const isOpen=has||openWt.has(w.path);
    return `<details class=wt data-path="${esc(w.path)}" ${isOpen?'open':''}><summary title="${esc(w.path)}"><span class=ch>▶</span><span class=repo>${esc(w.repo)}</span>${w.worktree?`<span class=br>${esc(w.worktree)}</span>`:''}<span class=cnt>${run?`<span class="dot on" title="running"></span>`:nbad?`<span class="dot bad" title="${nbad} to review"></span>`:''}${w.sessions.length}</span><button class=rm data-rm="${esc(w.cwd)}" title="Remove from monitor" aria-label="Remove ${esc(w.repo)} ${esc(w.worktree)} from monitor"><svg width=12 height=12 viewBox="0 0 12 12" fill=none stroke=currentColor stroke-width=1.5 stroke-linecap=round><path d="M3 3l6 6M9 3l-6 6"/></svg></button></summary><div class=body>${w.sessions.map(s=>`
    <button class=sess data-id="${s.id}" aria-current="${s.id===sel}">
      <span class=t>${esc(s.title)}</span>
      <span class=m><span class="dot ${s.running?'on':''}"></span>${s.running?'active':(s.tasks.length?s.tasks.length+' task'+(s.tasks.length===1?'':'s'):ago(d.now,s.main.ended))}<span>·</span>${when(s.started)}${bad(s)?` <span class="tag bad">${bad(s)} to review</span>`:''}${s.job?` <span class="tag bg">bg</span>`:''}</span>
    </button>`).join('')}</div></details>`}).join('');
}

function live(t,cap,now){
  const ev=t.events.slice().reverse(), cur=ev[0], prev=ev[1];
  const pct=Math.min(100,t.context/cap*100), cls=pct>95?'bad':pct>75?'warn':'';
  const line=(e,c='')=>`<div class="${c} ${e.k==='text'?'said':e.k==='compact'?'compact':''}"><b>${esc(e.k==='text'?'said':e.k)}</b>${esc(e.v)}</div>`;
  return `<section class=live>
    <div class=h><span class=pulse></span><span class=n>${esc(t.name)}</span><span class=ty>${esc(t.type)}</span><span class=el>${t.tools} calls · ${dur(t.started,new Date(now*1000).toISOString())}</span></div>
    <div class=now>${cur?line(cur):'<div>starting…</div>'}${prev?line(prev,'prev'):''}</div>
    <div class=meter><span>${t.context.toLocaleString()} <span class=c>/ ${fmtK(cap)} tokens</span></span><div class=bar><i class="${cls}" style="transform:scaleX(${pct/100})"></i></div></div>
  </section>`;
}

function table(tasks){
  return `<table><thead class=sr><tr><th>status</th><th>task</th><th>context</th><th>duration</th><th>ended</th></tr></thead><tbody>${tasks.map(t=>{
    const isOpen=open.has(t.id), tb=tab[t.id]||'report';
    const tl=t.events.slice().reverse().map(e=>`<li class="${e.k==='text'?'said':e.k==='compact'?'compact':''}"><span class=t>${hhmm(e.t)}</span><b>${esc(e.k==='text'?'said':e.k)}</b><span>${esc(e.v)}</span></li>`).join('');
    return `<tr class=row data-id="${t.id}" tabindex=0 aria-expanded="${isOpen}">
      <td><span class="pill ${esc(t.status)}" title="${esc(STATUS[t.status]||'')}">${esc(t.status==='NOREPORT'?'NO REPORT':t.status)}</span></td>
      <td class=name>${esc(t.name)}<span>${esc(t.type)}</span>${t.reason?`<span class=why>${esc(t.reason)}</span>`:''}</td>
      <td class=num>${fmtK(t.context)} tok</td>
      <td class=num>${dur(t.started,t.ended)}</td>
      <td class=num>${when(t.ended)}</td>
    </tr>${isOpen?`<tr class=detail><td colspan=5>
      <div class=tabs role=tablist><button role=tab aria-selected="${tb==='report'}" tabindex="${tb==='report'?0:-1}" data-tab=report data-id="${t.id}">report</button><button role=tab aria-selected="${tb==='timeline'}" tabindex="${tb==='timeline'?0:-1}" data-tab=timeline data-id="${t.id}">timeline</button></div>
      ${tb==='report'?`<pre class=report>${esc(t.last_text||'(no final message)')}</pre>`:`<ul class=tl>${tl}</ul>`}
    </td></tr>`:''}`}).join('')}</tbody></table>`;
}

function render(d){
  const all=d.worktrees.flatMap(w=>w.sessions.map(s=>({...s,wt:w.path,cwd:w.cwd,repo:w.repo,worktree:w.worktree})));
  if(!all.length){$('#nav').innerHTML='<h1>Worktrees</h1>';$('#main').innerHTML=`<div class=empty><p>No gauntlet runs in the last 24h.</p><p>Start one with <code>/gauntlet:run &lt;spec&gt;</code>. The run registers itself here on its first step.</p></div>`;return;}
  if(!all.some(s=>s.id===sel)) sel=(all.find(s=>s.running)||all[0]).id;
  $('#nav').innerHTML=nav(d);
  const s=all.find(x=>x.id===sel);
  const running=s.tasks.filter(t=>t.running), ended=s.tasks.filter(t=>!t.running).reverse();
  const review=ended.filter(t=>BAD.test(t.status)), done=ended.filter(t=>!BAD.test(t.status));
  let h=`<div class=top><h2>${esc(s.title)}</h2><span class=meta title="${esc(s.wt)}"><b>${esc(s.repo)}</b>${s.worktree?` / <span class=br>${esc(s.worktree)}</span>`:''} · ${s.branch&&s.branch!=='HEAD'?`<span class=br>${esc(s.branch)}</span> · `:''}${when(s.started)}</span></div>`;
  if(s.job) h+=`<div class=job><span class=st>${esc(s.job.state)}</span><span class=d>${esc(s.job.detail||'')}</span><span class=tk>bg ${esc(s.job.id)} · ${fmtK(s.job.tokens)} tok</span></div>`;
  const nfiles=diffCache&&diffCwd===s.cwd&&!diffCache.error?shown().length:null;
  h+=`<div class=views role=tablist><button role=tab data-view=tasks aria-selected="${view==='tasks'}" tabindex="${view==='tasks'?0:-1}">Tasks<span>${s.tasks.length}</span></button><button role=tab data-view=changes aria-selected="${view==='changes'}" tabindex="${view==='changes'?0:-1}">Changes${nfiles!==null?`<span>${nfiles}</span>`:''}</button></div>`;
  $('#main').classList.toggle('changes',view==='changes');
  if(view==='changes'){
    if(diffCwd!==s.cwd){diffCwd=s.cwd;diffCache=null;diffKey='';unmountDiff();loadDiff();}
    if(diffBox&&document.contains(diffBox)){paintDiff();return;}
    unmountDiff();
    h+=`<div class=chg-head id=chg-head></div><div id=diff><ol class=files id=flist></ol><div id=dv></div></div>`;
    $('#main').innerHTML=h; paintDiff(); return;
  }
  const me={id:'main',name:'session',type:'main conversation',events:s.main.events,context:s.main.context,tools:s.main.events.filter(e=>e.k!=='text').length,started:s.started,running:s.main.running};
  if(s.main.running) h+=`<h3>Running</h3>`+live(me,1000000,d.now)+running.map(t=>live(t,d.cap,d.now)).join('');
  else if(running.length) h+=`<h3>Running</h3>`+running.map(t=>live(t,d.cap,d.now)).join('');
  // Idle main session with nothing running: the orchestrator is waiting for the user.
  if(!s.main.running&&(s.main.events.length||s.main.summary)){const e=s.main.summary?{k:'recap',v:s.main.summary}:s.main.events[s.main.events.length-1],idle=!running.length;h+=`<h3>${idle?'Your turn':'Session'}<span>idle · ${ago(d.now,s.main.ended)}</span></h3><div class="wait ${idle?'idle':''}"><b>${esc(e.k==='text'?'last message':e.k)}</b>${esc(e.v)}</div>`;}
  if(review.length) h+=`<h3>Needs review<span>${review.length}</span></h3>`+table(review);
  if(done.length) h+=`<h3>Finished<span>${done.length}</span></h3>`+table(done);
  $('#main').innerHTML=h;
}

async function loadDiff(){
  if(!diffCwd)return;
  try{const r=await (await fetch('/diff?cwd='+encodeURIComponent(diffCwd))).json();
    const key=r.error||r.files.map(x=>x.path+x.status+x.add+x.del+x.patch.length).join('|');
    if(key!==diffKey){diffKey=key;diffCache=r;if(view==='changes')paintDiff();}
  }catch(e){diffKey='error';diffCache={error:'could not load the diff'};if(view==='changes')paintDiff();}
}
let diffRoot=null, diffBox=null, gdvLate=false;
setTimeout(()=>{gdvLate=true;if(!window.GDV&&view==='changes')paintDiff();},6000);
const plainDiff=f=>`<div class=f><div class=fh><span class="fs ${esc(f.status)}">${esc(f.status)}</span><span class=fp>${esc(f.path)}</span>${viewed(f)}</div><div class=body>${f.patch?`<pre class=raw>${f.patch.split('\n').map(l=>`<span class="${l.startsWith('@@')?'h':l.startsWith('+')&&!l.startsWith('+++')?'a':l.startsWith('-')&&!l.startsWith('---')?'d':''}">${esc(l)||' '}</span>`).join('')}</pre>`:'<div class=bin>diff omitted, patch too large</div>'}</div></div>`;
function unmountDiff(){if(diffRoot){diffRoot.unmount();diffRoot=null;diffBox=null;}}
// Line icons, 16px grid, drawn with the current text colour.
const ICON={
  eye:'<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>',
  flask:'<path d="M6 1.5h4M6.75 1.5v4.25L2.9 12.6a1.2 1.2 0 0 0 1.05 1.9h8.1a1.2 1.2 0 0 0 1.05-1.9L9.25 5.75V1.5M4.6 9.5h6.8"/>',
  split:'<rect x="1.75" y="2.5" width="12.5" height="11" rx="1.5"/><path d="M8 2.5v11"/>',
  unified:'<rect x="1.75" y="2.5" width="12.5" height="11" rx="1.5"/><path d="M4.5 6h7M4.5 8.5h7M4.5 11h4"/>',
  expand:'<path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"/>',
  collapse:'<path d="M6 2.5V6H2.5M13.5 6H10V2.5M10 13.5V10h3.5M2.5 10H6v3.5"/>',
  prev:'<path d="M10 3.5L5.5 8l4.5 4.5"/>',
  next:'<path d="M6 3.5L10.5 8 6 12.5"/>',
};
const ico=k=>`<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[k]}</svg>`;
const Ico=k=>window.GDV.React.createElement('span',{className:'i',dangerouslySetInnerHTML:{__html:ico(k)}});
const splitPath=p=>{const i=p.lastIndexOf('/')+1;return [p.slice(0,i),p.slice(i)]};
function FileCard({f,i,n,mode,full}){
  const {React,DiffView,DiffModeEnum}=window.GDV, h=React.createElement;
  const data=React.useMemo(()=>({oldFile:{fileName:f.oldPath,fileLang:f.lang,content:f.old??undefined},newFile:{fileName:f.path,fileLang:f.lang,content:f.new??undefined},hunks:[f.patch]}),[f.patch]);
  const [dir,name]=splitPath(f.path);
  return h('div',{className:'f'+(full?' full':'')},
    h('div',{className:'fh'},
      h('span',{className:'fs '+f.status},f.status),
      h('span',{className:'fp',title:f.path},h('span',{className:'dir'},dir),name),
      h('span',{className:'fn'},f.add==='-'?'binary':[h('b',{className:'a',key:'a'},'+'+num(f,'add')),' ',h('b',{className:'d',key:'d'},'−'+num(f,'del'))]),
      h('label',{className:'vw',title:'Viewed (v)'},h('input',{type:'checkbox',checked:isRead(f),onChange:()=>toggleRead(f)}),'Viewed'),
      h('span',{className:'pg'},h('button',{disabled:i===0,onClick:()=>stepFile(-1),'aria-label':'previous file'},Ico('prev')),h('span',null,(i+1)+' / '+n),h('button',{disabled:i===n-1,onClick:()=>stepFile(1),'aria-label':'next file'},Ico('next')),h('button',{className:'fs-btn',onClick:()=>{diffFull=!diffFull;paintDiff();},'aria-label':full?'exit full screen':'full screen',title:full?'Exit full screen (Esc)':'Full screen (f)'},Ico(full?'collapse':'expand')))),
    f.patch?h('div',{className:'body'},h(DiffView,{data,diffViewMode:mode==='split'?DiffModeEnum.Split:DiffModeEnum.Unified,diffViewTheme:'dark',diffViewHighlight:true,diffViewFontSize:12,diffViewWrap:false}))
           :h('div',{className:'bin'},'diff omitted, patch too large'));
}
function stepFile(d){const fs=shown(), i=fs.findIndex(f=>f.path===diffSel)+d; if(fs[i]){diffSel=fs[i].path;paintDiff();}}
function paintDiff(){
  const head=$('#chg-head'), box=$('#diff'), list=$('#flist'), dv=$('#dv'); if(!head||!box)return;
  const r=diffCache;
  if(!r){head.textContent='loading changes…';return;}
  if(r.error){head.innerHTML='';unmountDiff();box.innerHTML=`<div class=chg-empty>${esc(r.error)} at <code>${esc(diffCwd)}</code></div>`;return;}
  const all=base(), files=shown(), nread=all.filter(isRead).length;
  const adds=files.reduce((n,x)=>n+(+x.add||0),0), dels=files.reduce((n,x)=>n+(+x.del||0),0);
  // Rewrite head and file list only when the diff, mode or selection changed, so focus and scroll stay.
  const hk=diffKey+diffMode+hideTests+showViewed+nread;
  if(head.dataset.k!==hk){head.dataset.k=hk;head.innerHTML=`<span><code>${esc(r.branch)}</code> uncommitted</span><span class=a>+${adds}</span><span class=d>−${dels}</span>${r.truncated?'<span class=w>some diffs omitted, over 3 MB</span>':''}
    <span class=r>${nread?`<button data-viewed aria-pressed="${showViewed}" title="${showViewed?'Hide':'Show'} viewed files" aria-label="Show viewed files">${ico('eye')}</button>`:''}<button data-tests aria-pressed="${!hideTests}" title="${hideTests?'Show':'Hide'} test files" aria-label="Show test files">${ico('flask')}</button><button data-mode title="${diffMode==='split'?'Split view, click for unified':'Unified view, click for split'}" aria-label="Switch to ${diffMode==='split'?'unified':'split'} view">${ico(diffMode)}</button></span>`;}
  if(!r.files.length){unmountDiff();box.innerHTML=`<div class=chg-empty>No uncommitted changes. Working tree is clean.</div>`;return;}
  if(!files.length){unmountDiff();box.innerHTML=`<div class=chg-empty>${all.length?'All files viewed. Use show viewed to see them again.':'Only test files changed. Turn off hide tests to see them.'}</div>`;return;}
  if(!list||!dv){unmountDiff();box.innerHTML='<ol class=files id=flist></ol><div id=dv></div>';return paintDiff();}
  if(!files.some(f=>f.path===diffSel)) diffSel=files[0].path;
  const lk=diffKey+hideTests+showViewed+nread;
  if(list.dataset.k!==lk){list.dataset.k=lk;list.innerHTML=files.map(f=>{const [dir,name]=splitPath(f.path);return `<li><button class=fr data-file="${esc(f.path)}"><span class=ck aria-hidden=true><svg width=12 height=12 viewBox="0 0 12 12" fill=none stroke=currentColor stroke-width=1.6 stroke-linecap=round stroke-linejoin=round><path d="M2.5 6.5l2.5 2.5 4.5-6"/></svg></span><span class="fs ${esc(f.status)}">${esc(f.status)}</span><span class=fp title="${esc(f.path)}"><span class=dir>${esc(dir)}</span>${esc(name)}</span><span class=fn>${f.add==='-'?'binary':`<b class=a>+${num(f,'add')}</b> <b class=d>−${num(f,'del')}</b>`}</span></button></li>`}).join('');}
  const byPath=Object.fromEntries(files.map(f=>[f.path,f]));
  list.querySelectorAll('.fr').forEach(b=>{b.setAttribute('aria-current',b.dataset.file===diffSel);b.classList.toggle('read',isRead(byPath[b.dataset.file]));});
  if(!window.GDV){
    // Diff viewer comes from a CDN; without it, show the raw patch.
    if(gdvLate){unmountDiff();dv.innerHTML=plainDiff(files.find(f=>f.path===diffSel));return;}
    dv.innerHTML='<div class=chg-empty>loading diff viewer…</div>';window.addEventListener('gdv-ready',paintDiff,{once:true});return;}
  if(diffBox!==dv){unmountDiff();dv.innerHTML='';diffRoot=window.GDV.createRoot(dv);diffBox=dv;}
  const i=files.findIndex(f=>f.path===diffSel);
  diffRoot.render(window.GDV.React.createElement(FileCard,{key:diffSel,f:files[i],i,n:files.length,mode:diffMode,full:diffFull}));
}
document.addEventListener('click',e=>{
  const rm=e.target.closest('[data-rm]'); if(rm){e.preventDefault();fetch('/forget?cwd='+encodeURIComponent(rm.dataset.rm),{method:'POST',headers:{'X-Gauntlet':'1'}}).then(tick);return;}
  const v=e.target.closest('.views [data-view]'); if(v){view=v.dataset.view;if(view!=='changes')unmountDiff();render(data);return;}
  const md=e.target.closest('.chg-head [data-mode]'); if(md){diffMode=diffMode==='split'?'unified':'split';paintDiff();return;}
  if(e.target.closest('.chg-head [data-viewed]')){showViewed=!showViewed;paintDiff();return;}
  if(e.target.closest('.chg-head [data-tests]')){hideTests=!hideTests;try{localStorage.setItem('hideTests',hideTests?'1':'0')}catch(e){}if(!$('#flist'))render(data);else paintDiff();return;}
  const fb=e.target.closest('#flist [data-file]'); if(fb){diffSel=fb.dataset.file;paintDiff();return;}
  const s=e.target.closest('.sess'); if(s){sel=s.dataset.id;history.replaceState(null,'','#'+sel);unmountDiff();render(data);return;}
  const tb=e.target.closest('.tabs [data-tab]'); if(tb){tab[tb.dataset.id]=tb.dataset.tab;keep(()=>render(data));return;}
  const r=e.target.closest('tr.row'); if(r){open.has(r.dataset.id)?open.delete(r.dataset.id):open.add(r.dataset.id);keep(()=>render(data));}
});
document.addEventListener('change',e=>{if(e.target.matches('#dv [data-read]'))toggleRead(shown().find(x=>x.path===diffSel));});
document.addEventListener('toggle',e=>{const w=e.target.closest('.wt');if(!w)return;w.open?openWt.add(w.dataset.path):openWt.delete(w.dataset.path);saveWt();},true);
(()=>{
  const root=document.documentElement, grip=$('#grip');
  const setNav=w=>{root.style.setProperty('--nav',w+'px');document.body.classList.toggle('navclosed',w===0);};
  try{const w=localStorage.getItem('nav');if(w!==null)setNav(+w);}catch(e){}
  $('#navopen').addEventListener('click',()=>{setNav(300);try{localStorage.setItem('nav',300)}catch(e){}});
  grip.addEventListener('pointerdown',e=>{
    grip.setPointerCapture(e.pointerId);grip.classList.add('on');document.body.classList.add('dragging');
    const move=ev=>{setNav(ev.clientX<120?0:Math.min(600,Math.max(200,ev.clientX)));};
    const up=()=>{grip.classList.remove('on');document.body.classList.remove('dragging');grip.removeEventListener('pointermove',move);grip.removeEventListener('pointerup',up);try{localStorage.setItem('nav',parseInt(getComputedStyle(root).getPropertyValue('--nav')))}catch(e){}};
    grip.addEventListener('pointermove',move);grip.addEventListener('pointerup',up);
  });
  grip.addEventListener('keydown',e=>{if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;const w=parseInt(getComputedStyle(root).getPropertyValue('--nav'))+(e.key==='ArrowRight'?20:-20);setNav(w<120?0:Math.min(600,Math.max(200,w)));try{localStorage.setItem('nav',parseInt(getComputedStyle(root).getPropertyValue('--nav')))}catch(e){}});
  grip.addEventListener('dblclick',()=>{const w=parseInt(getComputedStyle(root).getPropertyValue('--nav'))?0:300;setNav(w);try{localStorage.setItem('nav',w)}catch(e){}});
})();
document.addEventListener('keydown',e=>{if(view==='changes'&&diffCache&&!e.target.closest('input,textarea')){if(e.key==='[')stepFile(-1);if(e.key===']')stepFile(1);if(e.key==='Escape'&&diffFull){diffFull=false;paintDiff();}if(e.key==='f'&&!e.metaKey&&!e.ctrlKey){diffFull=!diffFull;paintDiff();}if(e.key==='v'&&!e.metaKey&&!e.ctrlKey){const f=shown().find(x=>x.path===diffSel);f&&toggleRead(f);}}const r=e.target.closest('tr.row');if(r&&(e.key==='Enter'||e.key===' ')){e.preventDefault();r.click();}
  const t=e.target.closest('[role=tab]');if(t&&(e.key==='ArrowLeft'||e.key==='ArrowRight')){const all=[...t.parentNode.querySelectorAll('[role=tab]')],n=all[(all.indexOf(t)+(e.key==='ArrowRight'?1:all.length-1))%all.length];n.click();const k=n.dataset.view?`[data-view=${n.dataset.view}]`:`[data-tab=${n.dataset.tab}][data-id="${n.dataset.id}"]`;$(k)?.focus();}});
// Re-render keeping focus, report scroll and page scroll. Skips while the user is selecting text in main.
function keep(fn){
  const a=document.activeElement, ds=a&&a!==document.body?Object.entries(a.dataset).map(([k,v])=>`[data-${k}="${CSS.escape(v)}"]`).join(''):'';
  const q=ds&&a.tagName.toLowerCase()+ds, m=$('#main').scrollTop, reps=[...document.querySelectorAll('.report')].map(r=>r.scrollTop);
  fn();
  $('#main').scrollTop=m; document.querySelectorAll('.report').forEach((r,i)=>r.scrollTop=reps[i]||0);
  if(q) document.querySelector(q)?.focus({preventScroll:true});
}
const statuses=d=>Object.fromEntries(d.worktrees.flatMap(w=>w.sessions.flatMap(s=>s.tasks.map(t=>[t.id,[t.name,t.running?'running':t.status]]))));
let raw='', seen=null;
async function tick(){
  let text;
  try{text=await (await fetch('/api')).text();}
  catch(err){document.body.classList.add('stale');$('#status').textContent=`Monitor offline · last update ${data?hhmm(new Date(data.now*1000).toISOString()):'never'} · retrying`;return;}
  document.body.classList.remove('stale');
  const sl=getSelection(); if(text===raw||(!sl.isCollapsed&&$('#main').contains(sl.anchorNode)))return;
  raw=text; data=JSON.parse(text);
  const now=statuses(data);
  if(seen) $('#announce').textContent=Object.entries(now).filter(([id,[,st]])=>seen[id]&&seen[id][1]!==st&&st!=='running').map(([,[n,st]])=>`${n}: ${st.toLowerCase()}`).join('. ');
  seen=now;
  keep(()=>render(data));
}
tick();setInterval(tick,2000);setInterval(()=>{if(view==='changes')loadDiff();},4000);
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api":
            body = json.dumps(snapshot()).encode(); ctype = "application/json"
        elif u.path == "/diff":
            body = json.dumps(changes(parse_qs(u.query).get("cwd", [""])[0])).encode(); ctype = "application/json"
        else:
            body = PAGE.encode(); ctype = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    # The custom header forces a CORS preflight, so other sites cannot remove entries.
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/forget" or not self.headers.get("X-Gauntlet"):
            self.send_response(404); self.end_headers(); return
        forget(parse_qs(u.query).get("cwd", [""])[0])
        self.send_response(204); self.end_headers()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--register"]:
        register(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:])); sys.exit(0)
    print(f"gauntlet watch on http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
