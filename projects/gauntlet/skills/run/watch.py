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
    events, last, last_kind, usage, title, summary, model = [], "", "", {}, "", "", ""
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
        model = m.get("model") or model
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use":
                last_kind = "tool_use"; events.append({"t": iso, "k": b["name"], "v": brief(b["name"], b.get("input", {}))})
            elif b.get("type") == "text" and b.get("text", "").strip():
                last_kind = "text"; events.append({"t": iso, "k": "text", "v": b["text"].strip()[:400]})
    ctx = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    age = time.time() - os.path.getmtime(path)
    return {"events": events[-20:], "running": last_kind == "tool_use" or age < 30, "ended": last, "context": ctx,
            "title": title, "notes": task_notes(path), "summary": summary, "model": model}


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


CHAT_CACHE = {}
MAX_MSG = 12000
SID_RE = re.compile(r"^[0-9a-f-]{36}$")
# Members get a name in start order inside a run, so each keeps its name across refreshes.
NAMES = ["Ada", "Linus", "Grace", "Alan", "Margaret", "Dennis", "Barbara", "Ken", "Frances", "Edsger", "Radia", "Guido", "Hedy", "Niklaus"]
DOING = {"Edit": "editing", "Write": "writing", "MultiEdit": "editing", "Read": "reading", "Bash": "running", "Grep": "searching",
         "Glob": "searching", "Agent": "briefing", "WebFetch": "reading", "WebSearch": "searching", "compact": "compacting"}
STATUS_WORD = {"DONE": "finished", "PASS": "passed", "FAILED": "failed", "FAIL": "failed", "BLOCKED": "is blocked", "STOPPED": "was stopped",
               "HANDOFF": "handed off", "NOREPORT": "ended without a report", "ENDED": "ended"}


def said(c):
    """What a person typed: '' for tool results, reminders and command output."""
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c): return ""
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str): return ""
    cmd = CMD_RE.search(c)
    if cmd: return (cmd.group(1).strip() + " " + " ".join((cmd.group(2) or "").split())).strip()
    return TAG_RE.sub("", c).strip()


def chat_lines(path, side):
    """Messages of one transcript, [{t, from: 'in'|'out'|'note', text}], and its model. 'in' is a prompt it got, 'out' a reply."""
    key = (path, os.path.getsize(path))
    if key in CHAT_CACHE: return CHAT_CACHE[key]
    out, model = [], ""
    with open(path) as f:
        for line in f:
            if '"type":"user"' not in line and '"type":"text"' not in line and '"compact_boundary"' not in line: continue
            try: d = json.loads(line)
            except ValueError: continue
            if (d.get("isSidechain") and not side) or d.get("isMeta") or d.get("isCompactSummary"): continue
            m, iso = d.get("message", {}), d.get("timestamp", "")
            if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                out.append({"t": iso, "from": "note", "text": "context compacted · " + compact_event(d)["v"]})
            elif d.get("type") == "user":
                text = said(m.get("content"))
                if text == "[Request interrupted by user]": out.append({"t": iso, "from": "note", "text": "interrupted"})
                elif text: out.append({"t": iso, "from": "in", "text": text[:MAX_MSG]})
            elif d.get("type") == "assistant":
                model = m.get("model") or model
                c = m.get("content") if isinstance(m.get("content"), list) else []
                text = "\n\n".join(b["text"].strip() for b in c if b.get("type") == "text" and b.get("text", "").strip())
                if text: out.append({"t": iso, "from": "out", "text": text[:MAX_MSG]})
    if len(CHAT_CACHE) > 64: CHAT_CACHE.clear()
    CHAT_CACHE[key] = (out, model)
    return out, model


def member_name(i): return NAMES[i % len(NAMES)] + (f" {i // len(NAMES) + 1}" if i >= len(NAMES) else "")


def lead_name(model):
    return next((k.capitalize() for k in ("fable", "opus", "sonnet", "haiku") if k in model), "Lead")


def doing(running, ev):
    cur = ev[-1] if ev else None
    return f"{DOING.get(cur['k'], 'using ' + cur['k'])} {cur['v'][:80]}".strip() if running and cur and cur["k"] != "text" else ""


def chat(sid):
    """One run as a group chat: you, the lead session, and one named member per subagent."""
    paths = glob.glob(f"{ROOT}/*/{sid}.jsonl") if SID_RE.match(sid) else []
    if not paths: return {"error": "no transcript for this session"}
    main = session_tail(paths[0])
    lines, model = chat_lines(paths[0], False)
    tasks = {}
    for tp in glob.glob(f"{ROOT}/*/{sid}/subagents/agent-*.jsonl"):
        t = parse_task(tp); t["path"] = tp
        if t["id"] not in tasks or t["mtime"] > tasks[t["id"]]["mtime"]: tasks[t["id"]] = t
    tasks = sorted(tasks.values(), key=lambda t: t["started"])
    members = [{"id": "main", "name": lead_name(model), "role": "lead" + (f" · {model}" if model else ""),
                "running": main["running"], "status": "", "reason": "", "context": main["context"],
                "doing": doing(main["running"], main["events"]), "summary": main["summary"]}]
    msgs = [{"t": l["t"], "from": {"in": "you", "out": "main"}.get(l["from"], "note"), "text": l["text"]} for l in lines]
    for i, t in enumerate(tasks):
        name = member_name(i)
        members.append({"id": t["id"], "name": name, "role": t["name"] + (f" · {t['type']}" if t["type"] else ""),
                        "running": t["running"], "status": t["status"], "reason": t["reason"], "context": t["context"],
                        "doing": doing(t["running"], t["events"]),
                        "summary": "" if t["running"] else (t["last_text"].strip().splitlines() or [""])[0][:300]})
        tl, _ = chat_lines(t["path"], True)
        for j, l in enumerate(tl):
            if l["from"] == "out": msgs.append({"t": l["t"], "from": t["id"], "text": l["text"]})
            elif l["from"] == "note": msgs.append({"t": l["t"], "from": "note", "about": t["id"], "text": f"{name}: {l['text']}"})
            elif j == 0: msgs.append({"t": l["t"], "from": "main", "to": t["id"], "text": l["text"]})
        if not t["running"]:
            msgs.append({"t": t["ended"], "from": "note", "about": t["id"],
                         "text": f"{name} {STATUS_WORD.get(t['status'], t['status'].lower())}" + (f" · {t['reason']}" if t["reason"] else "")})
    msgs.sort(key=lambda m: m["t"] or "")
    return {"id": sid, "cap": CAP, "members": members, "messages": msgs[-600:]}


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
        for i, t in enumerate(tasks): t["who"] = member_name(i)
        mtime = max([os.path.getmtime(rp)] + ([os.path.getmtime(path)] if path else []) + [t["mtime"] for t in tasks])
        if now - mtime > WINDOW: continue
        _, _, title = session_head(path) if path else ("", "", "")
        main = session_tail(path) if path else {"events": [], "running": False, "ended": "", "context": 0, "title": "", "notes": {"stopped": [], "turns": {}}, "summary": "", "model": ""}
        title = run.get("name") or (title if title.startswith("/") else main["title"] or title)
        for t in tasks:
            if t["id"] in main["notes"]["stopped"]: t["reason"] = t["reason"].replace("interrupted", "stopped by orchestrator")
            if t["id"] in main["notes"]["turns"] and t["status"] == "ENDED":
                t["status"] = "NOREPORT"; t["reason"] = f"hit the {main['notes']['turns'][t['id']]}-turn limit, no handoff written"
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip() if os.path.isdir(cwd) else ""
        sess = {"id": sid, "title": title or sid[:8], "branch": branch, "lead": lead_name(main["model"]), "job": jobs.get(sid), "main": main,
                "tasks": tasks, "running": sum(t["running"] for t in tasks) + (1 if main["running"] else 0),
                "started": tasks[0]["started"] if tasks else main["ended"], "mtime": mtime}
        repo, wt = worktree_label(cwd)
        w = trees.setdefault(cwd, {"path": short(cwd), "cwd": cwd, "repo": repo, "worktree": wt, "sessions": [], "mtime": 0})
        w["sessions"].append(sess); w["mtime"] = max(w["mtime"], sess["mtime"])
    for w in trees.values(): w["sessions"].sort(key=lambda s: -s["mtime"])
    return {"cap": CAP, "now": now, "worktrees": sorted(trees.values(), key=lambda w: -w["mtime"])}


PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>gauntlet</title>
<link rel=icon href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23111b21'/%3E%3Crect x='.5' y='.5' width='31' height='31' rx='6.5' fill='none' stroke='%232f3b43'/%3E%3Ccircle cx='16' cy='16' r='9.5' fill='none' stroke='%2300a884' stroke-opacity='.35' stroke-width='1.5'/%3E%3Ccircle cx='16' cy='16' r='5' fill='%2300a884'/%3E%3C/svg%3E">
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
  --bg:#0b141a;--side:#111b21;--head:#202c33;--hover:#1d282f;--sel:#2a3942;--line:#222d34;--line-2:#34434c;
  --ink:#e9edef;--ink-2:#aebac1;--ink-3:#8696a0;
  --accent:#00a884;--accent-ink:#21c063;--in:#202c33;--out:#005c4b;--tool:#53bdeb;--warn:#e9a23b;--bad:#f15c6d;
  --panel:var(--side);--raise:var(--head);--run:var(--accent-ink);--run-dim:#0d3b2e;
  --ok-bg:#0f2a22;--ok-line:#1f7a5c;--warn-bg:#2d2414;--warn-line:#8a6420;--bad-bg:#2d1619;--bad-line:#a8394a;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
  --list:360px;--drawer:380px;
}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:var(--line-2) transparent}
html{color-scheme:dark;background:var(--side)}
body{margin:0;background:var(--side);color:var(--ink);font:14px/1.45 var(--sans);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;height:100vh;height:100dvh;display:grid;grid-template-columns:var(--list) minmax(0,1fr) auto;overflow:hidden}
::selection{background:var(--out);color:var(--ink)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;text-align:left}
b{font-weight:600}
.i{display:inline-flex;flex:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.ib{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;color:var(--ink-3);flex:none;transition:background .15s ease-out,color .15s ease-out}
.ib:hover{background:var(--sel);color:var(--ink)}
.ib[aria-pressed=true]{background:var(--sel);color:var(--ink)}

/* avatars: initials on a tint of the person's colour */
.av{--c:var(--ink-3);width:46px;height:46px;border-radius:50%;flex:none;display:grid;place-items:center;font-weight:600;font-size:17px;letter-spacing:-.01em;color:var(--c);background:color-mix(in srgb,var(--c) 18%,var(--side));position:relative;user-select:none}
.av.s{width:32px;height:32px;font-size:13px}
.av.xs{width:28px;height:28px;font-size:12px}
.av.xl{width:96px;height:96px;font-size:36px}
.av.lead{box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--c) 70%,transparent)}
.av.on::after{content:"";position:absolute;right:0;bottom:0;width:12px;height:12px;border-radius:50%;background:var(--accent-ink);box-shadow:0 0 0 2.5px var(--side)}
.av.s.on::after,.av.xs.on::after{width:9px;height:9px;box-shadow:0 0 0 2px var(--side)}
.av svg{width:22px;height:22px}

/* chat list */
#list{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line);background:var(--side)}
.lh{display:flex;align-items:center;gap:10px;padding:14px 16px 10px;flex:none}
.lh h1{font-size:21px;font-weight:700;letter-spacing:-.02em;margin:0;flex:1}
.lh .live{font-size:12px;color:var(--ink-3);display:flex;align-items:center;gap:6px}
.lh .live i{width:7px;height:7px;border-radius:50%;background:var(--accent-ink)}
.filt{display:flex;gap:8px;padding:2px 16px 10px;flex:none}
.filt button{padding:5px 12px;border-radius:999px;background:var(--head);color:var(--ink-2);font-size:13px}
.filt button:hover{background:var(--sel)}
.filt button[aria-pressed=true]{background:color-mix(in srgb,var(--accent) 22%,var(--side));color:var(--accent-ink)}
#chats{overflow-y:auto;flex:1;min-height:0}
.row{display:grid;grid-template-columns:46px minmax(0,1fr) auto;column-gap:14px;row-gap:1px;width:100%;padding:11px 16px;align-items:center;position:relative}
.row::after{content:"";position:absolute;left:76px;right:0;bottom:0;border-bottom:1px solid var(--line)}
.row:hover{background:var(--hover)}
.row[aria-current=true]{background:var(--sel)}
.row .av{grid-row:span 2}
.row .nm{font-size:15.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .tm{font-size:12px;color:var(--ink-3);justify-self:end}
.row.unread .tm,.row.turn .tm{color:var(--accent-ink)}
.row .pv{font-size:13.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.row .pv b{font-weight:500;color:var(--ink-2)}
.row .pv.ty{color:var(--accent-ink)}
.row .pv.turn{color:var(--ink)}
.row .bd{display:flex;gap:5px;justify-self:end;align-items:center}
.cnt{min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:var(--accent-ink);color:#0b141a;font-size:11.5px;font-weight:700;display:grid;place-items:center}
.cnt.bad{background:var(--bad)}
.flag{font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;color:var(--accent-ink);background:color-mix(in srgb,var(--accent) 18%,var(--side))}
.lempty{padding:28px 20px;color:var(--ink-3);font-size:13.5px;line-height:1.55}
.lempty code,.cempty code{font-family:var(--mono);font-size:12.5px;color:var(--ink-2);background:var(--head);padding:1px 6px;border-radius:4px}

/* conversation */
#conv{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
.ch{display:flex;align-items:center;gap:12px;padding:8px 10px 8px 16px;background:var(--head);flex:none;min-height:60px}
.ch .back{display:none}
.ch .who{display:flex;align-items:center;gap:12px;min-width:0;flex:1;padding:4px 0}
.ch .t{min-width:0}
.ch .nm{font-size:16px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ch .sub{font-size:12.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ch .sub.ty{color:var(--accent-ink)}
.ch .acts{display:flex;gap:4px;align-items:center}
.chg-btn{display:flex;align-items:center;gap:7px;padding:7px 12px;border-radius:999px;color:var(--ink-2);font-size:13px}
.chg-btn:hover,.chg-btn[aria-pressed=true]{background:var(--sel);color:var(--ink)}
.chg-btn b{font-weight:600;color:var(--ink)}
.pin{display:flex;align-items:center;gap:12px;width:100%;padding:8px 16px;background:var(--side);border-bottom:1px solid var(--line);flex:none;font-size:13px;color:var(--ink-2)}
.pin:hover{background:var(--hover)}
.pin svg{flex:none;color:var(--ink-3)}
.pin .tx{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.pin .tx b{color:var(--ink);font-weight:500}
.pin .tx .wk{color:var(--accent-ink)}.pin .tx .rv{color:var(--warn)}
#msgs{flex:1;overflow-y:auto;padding:14px 6% 18px;display:flex;flex-direction:column;background-color:var(--bg);background-image:radial-gradient(rgba(233,237,239,.035) 1px,transparent 1.2px);background-size:22px 22px}
.day{align-self:center;margin:12px 0 8px;padding:5px 12px;border-radius:8px;background:var(--head);color:var(--ink-3);font-size:12px;box-shadow:0 1px 1px rgba(0,0,0,.25)}
.note{align-self:center;margin:6px 0;padding:5px 12px;border-radius:8px;background:#182229;color:var(--ink-3);font-size:12.5px;max-width:80%;text-align:center;box-shadow:0 1px 1px rgba(0,0,0,.2)}
.note.bad{color:#f6a3ae}.note.ok{color:#7fd8b6}
.m{display:grid;grid-template-columns:32px minmax(0,1fr);column-gap:8px;margin-top:2px}
.m.first{margin-top:10px}
.m .av{align-self:start}
.m.me{grid-template-columns:minmax(0,1fr)}
.bub{position:relative;justify-self:start;max-width:min(100%,720px);padding:6px 9px 8px;border-radius:8px;background:var(--in);overflow-wrap:anywhere;font-size:14px;line-height:1.48;box-shadow:0 1px 1px rgba(0,0,0,.25);grid-column:2}
.m.first .bub{border-top-left-radius:2px}
.m.me .bub{justify-self:end;background:var(--out);grid-column:1;max-width:min(80%,720px)}
.m.me.first .bub{border-top-left-radius:8px;border-top-right-radius:2px}
.bub .from{display:flex;gap:6px;align-items:baseline;font-size:13px;font-weight:600;margin-bottom:2px}
.bub .from button:hover{text-decoration:underline;text-underline-offset:3px}
.bub .from span{font-weight:400;color:var(--ink-3);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bub .body{display:block}
.bub .tt{float:right;margin:6px 0 -4px 12px;font-size:11px;color:var(--ink-3);line-height:1;position:relative;top:4px}
.m.me .bub .tt{color:#9fc7bd}
.bub p{margin:0 0 6px}.bub p:last-child{margin:0}
.bub code{font-family:var(--mono);font-size:12.5px;background:rgba(0,0,0,.25);padding:1px 4px;border-radius:4px}
.bub pre{font-family:var(--mono);font-size:12.5px;line-height:1.5;background:rgba(0,0,0,.28);padding:8px 10px;border-radius:6px;overflow-x:auto;margin:4px 0 6px;white-space:pre}
.bub pre code{background:none;padding:0}
.bub ul{margin:0 0 6px;padding-left:20px}
.bub li{margin:1px 0}
.bub.long .body{max-height:300px;overflow:hidden;-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}
.bub.long.open .body{max-height:none;-webkit-mask-image:none;mask-image:none}
.bub .more{display:block;color:var(--tool);font-size:13px;margin-top:4px}
.bub .more:hover{text-decoration:underline;text-underline-offset:3px}
.typing{display:flex;gap:8px;align-items:center;margin-top:10px}
.typing .bub{display:flex;gap:9px;align-items:center;padding:9px 12px;color:var(--ink-3);font-size:13px;grid-column:auto;max-width:min(80%,600px)}
.dots{display:inline-flex;gap:3px;flex:none}.dots i{width:6px;height:6px;border-radius:50%;background:var(--ink-3);animation:blink 1.3s infinite}
.dots i:nth-child(2){animation-delay:.18s}.dots i:nth-child(3){animation-delay:.36s}
.typing .what{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.typing .what b{color:var(--ink-2);font-weight:500}
@keyframes blink{0%,60%,100%{opacity:.35;transform:none}30%{opacity:1;transform:translateY(-2px)}}
.cmp{flex:none;display:flex;gap:10px;align-items:center;padding:10px 16px;background:var(--head)}
.cmp .box{flex:1;display:flex;gap:10px;align-items:center;padding:10px 14px;border-radius:10px;background:var(--sel);color:var(--ink-3);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cmp .box .i{color:var(--ink-3)}
.cempty{margin:auto;max-width:44ch;text-align:center;color:var(--ink-3);font-size:14px;line-height:1.6;padding:24px}
.cempty h2{color:var(--ink);font-size:22px;font-weight:600;letter-spacing:-.01em;margin:16px 0 6px}
.cempty .mark{display:inline-grid;place-items:center;width:84px;height:84px;border-radius:50%;background:color-mix(in srgb,var(--accent) 14%,var(--bg));color:var(--accent-ink)}

/* drawer: group info, a person, or the changes */
#drawer{width:0;overflow:hidden;background:var(--side);border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
body.drawer #drawer{width:var(--drawer)}
body.drawer.wide{grid-template-columns:var(--list) 0 minmax(0,1fr)}
body.drawer.wide #conv{visibility:hidden}
body.drawer.wide #drawer{width:auto}
.dh{display:flex;align-items:center;gap:14px;padding:8px 12px;background:var(--head);min-height:60px;flex:none}
.dh h2{font-size:16px;font-weight:500;margin:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.db{overflow-y:auto;flex:1;min-height:0}
.dsec{padding:16px 22px;border-bottom:8px solid var(--bg)}
.dsec.hero{text-align:center;padding:26px 22px 20px}
.dsec.hero .av{margin:0 auto 14px}
.dsec.hero h3{font-size:21px;font-weight:500;margin:0 0 4px;letter-spacing:-.01em}
.dsec.hero p{margin:0;color:var(--ink-3);font-size:13.5px;overflow-wrap:anywhere}
.dsec.hero .path{font-family:var(--mono);font-size:12px}
.dsec h4{font-size:13px;font-weight:500;color:var(--ink-3);margin:0 0 10px;display:flex;gap:8px}
.dsec h4 span{margin-left:auto}
.recap{margin:0;font-size:14px;line-height:1.55;color:var(--ink)}
.mem{display:grid;grid-template-columns:40px minmax(0,1fr) auto;column-gap:12px;width:100%;padding:9px 22px;align-items:center;margin:0 -22px;width:calc(100% + 44px)}
.mem:hover{background:var(--hover)}
.mem .av{width:40px;height:40px;font-size:15px;grid-row:span 2}
.mem .nm{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mem .rl{font-size:12.5px;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.mem .rl.ty{color:var(--accent-ink)}
.st{font-size:11px;font-weight:600;letter-spacing:.02em;padding:2px 8px;border-radius:999px;justify-self:end;grid-row:span 2;color:var(--ink-2);background:var(--head);white-space:nowrap}
.st.work{color:var(--accent-ink);background:color-mix(in srgb,var(--accent) 16%,var(--side))}
.st.ok{color:#7fd8b6;background:var(--ok-bg)}
.st.warn{color:var(--warn);background:var(--warn-bg)}
.st.bad{color:#f6a3ae;background:var(--bad-bg)}
.kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 16px;font-size:13.5px;margin:0}
.kv dt{color:var(--ink-3)}.kv dd{margin:0;overflow-wrap:anywhere}
.kv dd.why{color:var(--warn)}
.meter{display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--ink-2)}
.bar{flex:1;height:4px;background:var(--line-2);border-radius:2px;overflow:hidden}
.bar i{display:block;height:100%;width:100%;background:var(--accent-ink);transform-origin:left;transition:transform .3s cubic-bezier(.16,1,.3,1)}
.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
.report{font-family:var(--mono);font-size:12.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word;margin:0;color:var(--ink-2);max-height:46vh;overflow:auto}
.tl{margin:0;padding:0;list-style:none;font-size:13px}
.tl li{display:grid;grid-template-columns:44px 68px minmax(0,1fr);gap:8px;padding:3px 0}
.tl .t{color:var(--ink-3);font-size:12px}
.tl b{color:var(--tool);font-weight:500;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tl span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono);font-size:12px;color:var(--ink-2)}
.tl .said b{color:var(--ink-3)}.tl .said span:last-child{font-family:var(--sans);white-space:normal;font-size:13px}
.tl .compact b{color:var(--warn)}
.danger{display:flex;gap:12px;align-items:center;width:100%;padding:12px 22px;color:var(--bad);font-size:14.5px}
.danger:hover{background:var(--hover)}
.danger small{display:block;color:var(--ink-3);font-size:12.5px}
.dchanges{display:flex;flex-direction:column;min-height:0;flex:1;padding:0 16px 14px}

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
.dchanges #diff{display:flex;flex-direction:column;flex:1;min-height:0}
.dchanges #dv,.dchanges .f{display:flex;flex-direction:column;flex:0 1 auto;min-height:0}
.chg-head{margin:12px 0 10px}

#status{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:20;padding:7px 14px;border-radius:8px;background:var(--warn-bg);color:var(--warn);font-size:13px;display:none;box-shadow:0 4px 14px rgba(0,0,0,.4)}
body.stale #status{display:block}
body.stale #list,body.stale #conv{opacity:.6}

@media (max-width:820px){
  body{grid-template-columns:minmax(0,1fr)}
  #list,#conv{grid-column:1;grid-row:1}
  body.open #list{display:none}
  body:not(.open) #conv{display:none}
  .ch .back{display:grid}
  #msgs{padding:12px 10px 16px}
  #drawer{position:fixed;inset:0;z-index:15;border:0;width:100%!important;transform:translateX(100%);transition:transform .25s cubic-bezier(.16,1,.3,1)}
  body.drawer #drawer{transform:none}
  .chg-btn .lbl{display:none}
}
@media (prefers-reduced-motion:reduce){.dots i{animation:none}.bar i,#drawer{transition:none}}
</style>
<aside id=list aria-label="chats">
  <div class=lh><h1>Gauntlet</h1><span class=live id=live></span></div>
  <div class=filt role=group aria-label="filter chats"><button data-filter=all aria-pressed=true>All</button><button data-filter=turn aria-pressed=false>Your turn</button><button data-filter=active aria-pressed=false>Working</button><button data-filter=review aria-pressed=false>To review</button></div>
  <div id=chats></div>
</aside>
<section id=conv aria-label="conversation"></section>
<aside id=drawer aria-label="details"></aside>
<div id=status role=status></div>
<div id=announce class=sr aria-live=polite></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const store={get:(k,d)=>{try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}},set:(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
let sel=location.hash.slice(1)||null, data=null, filter='all', drawer=null; // drawer: null | {k:'group'} | {k:'person',id} | {k:'changes'}
let diffMode='split', diffKey='', diffCache=null, diffCwd='', diffSel='', diffFull=false;
const inChanges=()=>drawer&&drawer.k==='changes';
// Test files: test/spec dirs, foo.test.ts / foo_spec.rb / test_foo.py, FooTest.kt / FooTests.swift / foo_test.go.
const TEST=/(^|\/)(__)?(tests?|specs?)(__)?\/|[._](test|spec)s?\.\w+$|(^|\/)test_[^/]*$|Tests?\.\w+$/;
let hideTests=store.get('hideTests',false);
// Files left after the tests filter; shown() also drops viewed files unless "show viewed" is on.
let showViewed=false;
const base=()=>diffCache.files.filter(f=>!hideTests||!TEST.test(f.path));
const shown=()=>base().filter(f=>showViewed||!isRead(f));
// Viewed files, keyed by checkout + path and tied to a hash of the patch, so a file that changes again is unread.
const read=store.get('read',{});
const fhash=f=>f._h??=[...(f.patch||f.add+f.del)].reduce((h,c)=>(h*31+c.charCodeAt(0))|0,0);
const rkey=f=>diffCwd+'\0'+f.path;
const isRead=f=>read[rkey(f)]===fhash(f);
function toggleRead(f){
  const fs=shown(), i=fs.indexOf(f);
  if(isRead(f)) delete read[rkey(f)]; else read[rkey(f)]=fhash(f);
  store.set('read',read);
  const next=isRead(f)&&[...fs.slice(i+1),...fs.slice(0,i)].find(x=>!isRead(x));
  if(next) diffSel=next.path;
  paintDiff();
}
const fmtK=n=>n>=1e6?(n/1e6).toFixed(1).replace(/\.0$/,'')+'M':n>=1000?(n/1000).toFixed(n>=10000?0:1).replace(/\.0$/,'')+'k':String(n);
const hhmm=iso=>iso?new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
const dur=(a,b)=>{const s=Math.max(0,Math.round((new Date(b)-new Date(a))/1000));return s<60?s+'s':s<3600?Math.floor(s/60)+'m':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m'};
// Clock time today, "Yesterday", weekday this week, date before that.
const when=iso=>{if(!iso)return '';const d=new Date(iso),n=new Date(),y=new Date(n-864e5);return d.toDateString()===n.toDateString()?hhmm(iso):d.toDateString()===y.toDateString()?'Yesterday':(n-d)/864e5<6?d.toLocaleDateString([],{weekday:'long'}):d.toLocaleDateString([],{month:'short',day:'numeric'})};
const num=(f,k)=>f[k]==='-'?'':(f[k]||0);
const STATUS={DONE:['Done','ok','finished and reported done'],PASS:['Passed','ok','check passed'],FAILED:['Failed','bad','reported a failure'],FAIL:['Failed','bad','check failed'],BLOCKED:['Blocked','bad','could not continue, needs a decision'],STOPPED:['Stopped','bad','interrupted before it finished'],HANDOFF:['Handed off','warn','handed the rest of the work to a later task'],NOREPORT:['No report','warn','ran out of room before writing a report'],ENDED:['Ended','','ended without a status line in its report']};
const BAD=/FAIL|BLOCKED|NOREPORT|HANDOFF|STOPPED/;
const NAMECOL=['#53bdeb','#f15c6d','#ffd279','#a5b337','#fc9775','#25d366','#c79ecf','#ff72a1','#7f66ff','#00a5f4'];
const hue=s=>NAMECOL[[...String(s)].reduce((h,c)=>(h*31+c.charCodeAt(0))>>>0,7)%NAMECOL.length];
// Initials from real words: ticket ids and numbers are skipped, so "CONNECT-683 refund flow" reads RF.
const initials=n=>{const ws=String(n).split(/[\s·/_.,:-]+/).filter(w=>/^[a-z]/i.test(w)&&!/\d/.test(w)&&!/^[A-Z]{2,}$/.test(w));return (ws.length?ws.slice(0,2).map(w=>w[0]).join(''):String(n).replace(/[^a-z]/ig,'').slice(0,2)).toUpperCase()||'?'};
const av=(name,cls='',col)=>`<span class="av ${cls}" style="--c:${col||hue(name)}" aria-hidden=true>${esc(initials(name))}</span>`;
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
  back:'<path d="M13 8H3M7.5 3.5L3 8l4.5 4.5"/>',
  close:'<path d="M4 4l8 8M12 4l-8 8"/>',
  info:'<circle cx="8" cy="8" r="6.5"/><path d="M8 7.25v4M8 4.9v.1"/>',
  diff:'<path d="M4.5 1.75h5l3 3v9.5h-8z"/><path d="M9.5 1.75v3h3M8.5 7v4M6.5 9h4M6.5 12.5h4"/>',
  pin:'<path d="M9.75 1.75l4.5 4.5-2 .75-2.5 2.5-.25 3-1.25 1.25-6-6L3.5 6.5l3-.25L9 3.75z"/><path d="M5.25 10.75L1.75 14.25"/>',
  term:'<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="1.5"/><path d="M4.5 6.5L6.5 8l-2 1.5M8.5 10h3"/>',
  branch:'<circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="5.5" r="1.5"/><path d="M4.5 5v6M11.5 7c0 3-7 2-7 4"/>',
  trash:'<path d="M2.5 4h11M6.5 4V2.5h3V4M4 4l.75 9.5h6.5L12 4"/>',
};
const ico=(k,s=16)=>`<svg width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[k]}</svg>`;
const Ico=k=>window.GDV.React.createElement('span',{className:'i',dangerouslySetInnerHTML:{__html:ico(k)}});
const splitPath=p=>{const i=p.lastIndexOf('/')+1;return [p.slice(0,i),p.slice(i)]};
const viewed=f=>`<label class=vw title="Viewed (v)"><input type=checkbox data-read ${isRead(f)?'checked':''}>Viewed</label>`;
const DOING={Edit:'editing',Write:'writing',MultiEdit:'editing',Read:'reading',Bash:'running',Grep:'searching',Glob:'searching',Agent:'briefing',WebFetch:'reading',WebSearch:'searching',compact:'compacting'};
const doing=e=>e?(e.k==='text'?'writing a message':(DOING[e.k]||'using '+e.k)+' '+e.v):'';

// Chats: one per checkout, from the latest registered run there.
function chats(d){
  return d.worktrees.map(w=>{const s=w.sessions[0];
    const people=[{id:'main',name:s.lead||'Lead',lead:true,running:s.main.running,events:s.main.events,context:s.main.context,status:'',reason:'',last_text:''},
      ...s.tasks.map(t=>({...t,name:t.who||t.name,role:t.name}))];
    const said=people.flatMap(p=>p.events.filter(e=>e.k==='text').map(e=>({t:e.t,who:p,v:e.v})));
    said.sort((a,b)=>a.t<b.t?-1:1);
    const last=said[said.length-1], busy=people.filter(p=>p.running);
    const review=s.tasks.filter(t=>!t.running&&BAD.test(t.status)).length;
    // Lead idle with nothing running, recently: it waits for you in the terminal.
    const turn=!busy.length&&!!s.main.events.length&&d.now-new Date(s.main.ended)/1000<3*3600;
    return {...s,cwd:w.cwd,path:w.path,repo:w.repo,worktree:w.worktree,people,said,last,busy,review,turn,mtime:s.mtime};
  });
}
const seen=store.get('seen',{});
const unread=c=>{if(!(c.id in seen)){seen[c.id]=c.last?c.last.t:'';store.set('seen',seen);}return c.said.filter(m=>m.t>seen[c.id]).length};
const markSeen=c=>{if(c&&c.last&&seen[c.id]!==c.last.t&&!document.hidden){seen[c.id]=c.last.t;store.set('seen',seen);}};

function renderList(all){
  const pass=c=>filter==='all'||(filter==='turn'&&c.turn)||(filter==='active'&&c.busy.length)||(filter==='review'&&c.review);
  const vis=all.filter(pass);
  let h=vis.map(c=>{
    const n=c.id===sel?0:unread(c);
    const pv=c.busy.length?`<span class="pv ty">${esc(c.busy.map(p=>p.name).join(', '))} ${c.busy.length>1?'are':'is'} typing…</span>`
      :c.turn&&c.last?`<span class="pv turn"><b>${esc(c.last.who.name)}:</b> ${esc(c.last.v.replace(/\s+/g,' '))}</span>`
      :`<span class=pv>${c.last?`<b>${esc(c.last.who.name)}:</b> ${esc(c.last.v.replace(/\s+/g,' '))}`:'No messages yet'}</span>`;
    return `<button class="row ${n?'unread':''} ${c.turn?'turn':''}" data-id="${esc(c.id)}" aria-current="${c.id===sel}" title="${esc(c.path)}">
      <span class="av ${c.busy.length?'on':''}" style="--c:${hue(c.repo+c.worktree)}" aria-hidden=true>${esc(initials(c.title))}</span>
      <span class=nm>${esc(c.title)}</span><span class=tm>${esc(when(c.last?c.last.t:c.started))}</span>
      ${pv}<span class=bd>${c.turn?'<span class=flag>your turn</span>':''}${c.review?`<span class="cnt bad" title="${c.review} to review">${c.review}</span>`:''}${n?`<span class=cnt title="${n} unread">${n}</span>`:''}</span>
    </button>`}).join('');
  if(!all.length) h=`<div class=lempty>No gauntlet runs in the last 24 hours. Start one with <code>/gauntlet:run &lt;spec&gt;</code>; it shows up here on its first step.</div>`;
  else if(!vis.length) h=`<div class=lempty>No chats match this filter.</div>`;
  const box=$('#chats'); if(box.dataset.k!==h){box.dataset.k=h;box.innerHTML=h;}
  const nb=all.reduce((n,c)=>n+c.busy.length,0);
  $('#live').innerHTML=nb?`<i></i>${nb} working`:'';
}

// Small markdown: fenced code, bullets, inline code, bold. Odd parts of the split are code.
const fmt=s=>esc(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
const BUL=/^\s*[-*] /;
const paras=t=>t.trim().split(/\n{2,}/).filter(Boolean).map(p=>p.split('\n').reduce((g,l)=>{const b=BUL.test(l),last=g[g.length-1];if(last&&last.b===b)last.ls.push(l);else g.push({b,ls:[l]});return g},[])
  .map(g=>g.b?'<ul>'+g.ls.map(l=>`<li>${fmt(l.replace(BUL,''))}</li>`).join('')+'</ul>':'<p>'+g.ls.map(fmt).join('<br>')+'</p>').join('')).join('');
const md=t=>t.split(/```[^\n]*\n?([\s\S]*?)```/g).map((p,i)=>i%2?`<pre><code>${esc(p.replace(/\n$/,''))}</code></pre>`:paras(p)).join('');

let chatSid='', chatData=null, chatRaw='';
const opened=new Set();
async function loadChat(){
  const sid=chatSid; if(!sid)return;
  try{const t=await (await fetch('/chat?id='+encodeURIComponent(sid))).text();
    if(sid!==chatSid||t===chatRaw)return; chatRaw=t; chatData=JSON.parse(t); paintMsgs();
  }catch(e){}
}
const members=()=>chatData&&!chatData.error?Object.fromEntries(chatData.members.map(m=>[m.id,m])):{};
const colOf=(id,name)=>hue(name+id);

function renderConv(c){
  const conv=$('#conv');
  if(!c){const h=`<div class=cempty><span class=mark>${ico('term',38)}</span><h2>Gauntlet</h2>Pick a chat on the left. Each chat is one checkout; the lead session and its builders talk there. You answer in the terminal.</div>`;if(conv.dataset.k!==h){conv.dataset.k=h;conv.innerHTML=h;}return;}
  if(conv.dataset.sid!==c.id){conv.dataset.sid=c.id;conv.dataset.k='';
    conv.innerHTML=`<header class=ch id=chead></header><button class=pin id=pin data-open=group></button><div id=msgs role=log aria-label="messages"></div><div class=cmp><span class=box>${ico('term')}Reply in the terminal that runs this session. This page only reads.</span></div>`;}
  const sub=c.busy.length?`<span class="sub ty">${esc(c.busy.map(p=>p.name).join(', '))} typing…</span>`:`<span class=sub>${esc(['You',...c.people.map(p=>p.name)].join(', '))}</span>`;
  const nf=diffCache&&diffCwd===c.cwd&&!diffCache.error?diffCache.files.length:null;
  const hh=`<button class="ib back" data-back aria-label="back to chats">${ico('back',20)}</button>
    <button class=who data-open=group aria-label="group info">${`<span class="av s ${c.busy.length?'on':''}" style="--c:${hue(c.repo+c.worktree)}" aria-hidden=true>${esc(initials(c.title))}</span>`}<span class=t><span class=nm style="display:block">${esc(c.title)}</span>${sub}</span></button>
    <span class=acts><button class=chg-btn data-open=changes aria-pressed="${inChanges()}" title="Uncommitted changes">${ico('diff')}<span class=lbl>Changes</span>${nf!==null?`<b>${nf}</b>`:''}</button><button class=ib data-open=group aria-pressed="${drawer&&drawer.k!=='changes'}" aria-label="group info" title="Group info">${ico('info',18)}</button></span>`;
  const head=$('#chead'); if(head.dataset.k!==hh){head.dataset.k=hh;head.innerHTML=hh;}
  // Pinned summary: who works, who needs review, whose turn it is.
  const bits=[];
  if(c.busy.length) bits.push(`<span class=wk>${esc(c.busy.map(p=>p.name).join(', '))} working</span>`);
  const rv=c.people.filter(p=>!p.running&&BAD.test(p.status));
  if(rv.length) bits.push(`<span class=rv>${esc(rv.map(p=>p.name).join(', '))} to review</span>`);
  const done=c.people.filter(p=>!p.running&&/DONE|PASS/.test(p.status)).length;
  if(done) bits.push(`${done} done`);
  if(c.turn) bits.push(`<b>${esc(c.people[0].name)} is waiting for you</b>`);
  const recap=c.main.summary?` · ${esc(c.main.summary)}`:'';
  const ph=`${ico('pin')}<span class=tx>${bits.join(' · ')||`${esc(c.people[0].name)} is idle`}${recap}</span>`;
  const pin=$('#pin'); if(pin.dataset.k!==ph){pin.dataset.k=ph;pin.innerHTML=ph;}
  if(chatSid!==c.id){chatSid=c.id;chatData=null;chatRaw='';loadChat();}
  paintMsgs();
}

function paintMsgs(){
  const box=$('#msgs'), c=current(); if(!box||!c)return;
  const ch=chatData;
  if(!ch){if(!box.dataset.k)box.innerHTML='<div class=cempty>Loading messages…</div>';return;}
  if(ch.error){box.innerHTML=`<div class=cempty>${esc(ch.error)}</div>`;return;}
  const by=members();
  let h='', lastKey='', lastDay='';
  for(const m of ch.messages){
    const day=m.t?new Date(m.t).toDateString():'';
    if(day&&day!==lastDay){lastDay=day;lastKey='';const w=when(m.t);h+=`<div class=day>${/\d:\d/.test(w)?'Today':esc(w)}</div>`;}
    if(m.from==='note'){lastKey='';const tone=/finished|passed/.test(m.text)?'ok':/failed|blocked|stopped|without a report|handed off|interrupted/.test(m.text)?'bad':'';h+=`<div class="note ${tone}">${esc(m.text)}</div>`;continue;}
    const key=m.from+'>'+(m.to||''), first=key!==lastKey; lastKey=key;
    const id=m.t+key, long=m.text.length>1100||m.text.split('\n').length>18;
    const bub=(from)=>`<div class="bub ${long?'long':''} ${opened.has(id)?'open':''}">${from}<span class=body>${md(m.text)}</span>${long?`<button class=more data-more="${esc(id)}">${opened.has(id)?'Show less':'Read more'}</button>`:''}<span class=tt>${hhmm(m.t)}</span></div>`;
    if(m.from==='you'){h+=`<div class="m me ${first?'first':''}">${bub('')}</div>`;continue;}
    const f=by[m.from]||{name:m.from,role:''}, to=m.to&&by[m.to], col=colOf(m.from,f.name);
    const from=first?`<span class=from><button data-person="${esc(m.from)}" style="color:${col}">${esc(f.name)}</button>${to?`<span>to <button data-person="${esc(m.to)}" style="color:${colOf(m.to,to.name)}">${esc(to.name)}</button> · ${esc(to.role.split(' · ')[0])}</span>`:m.from==='main'?'<span>lead</span>':''}</span>`:'';
    h+=`<div class="m ${first?'first':''}">${first?av(f.name,'s'+(m.from==='main'?' lead':''),col):'<span></span>'}${bub(from)}</div>`;
  }
  if(!ch.messages.length) h+=`<div class=cempty>No messages yet.</div>`;
  for(const m of ch.members) if(m.running) h+=`<div class=typing>${av(m.name,'s'+(m.id==='main'?' lead':''),colOf(m.id,m.name))}<div class=bub><span class=dots><i></i><i></i><i></i></span><span class=what><b>${esc(m.name)}</b>${m.doing?' · '+esc(m.doing):''}</span></div></div>`;
  if(box.dataset.k===h)return;
  const stick=!box.dataset.k||box.dataset.sid!==ch.id||box.scrollHeight-box.scrollTop-box.clientHeight<90, top=box.scrollTop;
  box.dataset.k=h; box.dataset.sid=ch.id; box.innerHTML=h;
  box.scrollTop=stick?box.scrollHeight:top;
}

function statusPill(p){
  if(p.running) return `<span class="st work">Working</span>`;
  if(p.lead) return `<span class=st>Lead</span>`;
  const [l,t,why]=STATUS[p.status]||[p.status,'',''];
  return `<span class="st ${t}" title="${esc(why)}">${esc(l)}</span>`;
}
function renderDrawer(c){
  document.body.classList.toggle('drawer',!!drawer&&!!c);
  document.body.classList.toggle('wide',inChanges());
  const dr=$('#drawer');
  if(!drawer||!c){if(dr.dataset.k){dr.dataset.k='';dr.innerHTML='';unmountDiff();}return;}
  if(inChanges()){
    if(diffCwd!==c.cwd){diffCwd=c.cwd;diffCache=null;diffKey='';unmountDiff();loadDiff();}
    if(dr.dataset.k!=='changes'+c.id){dr.dataset.k='changes'+c.id;unmountDiff();
      dr.innerHTML=`<header class=dh><button class=ib data-close aria-label="close changes">${ico('close',18)}</button><h2>Changes · <span style="font-family:var(--mono);font-size:13.5px;color:var(--ink-2)">${esc(c.worktree||c.repo)}</span></h2></header><div class=dchanges><div class=chg-head id=chg-head></div><div id=diff><ol class=files id=flist></ol><div id=dv></div></div></div>`;}
    paintDiff(); return;
  }
  let h;
  if(drawer.k==='person'){
    const p=c.people.find(x=>x.id===drawer.id); if(!p){drawer={k:'group'};return renderDrawer(c);}
    const cap=p.lead?1e6:data.cap, pct=Math.min(100,p.context/cap*100), col=colOf(p.id,p.name);
    const [sl,,why]=STATUS[p.status]||['','',''];
    const tl=p.events.slice().reverse().map(e=>`<li class="${e.k==='text'?'said':e.k==='compact'?'compact':''}"><span class=t>${hhmm(e.t)}</span><b>${esc(e.k==='text'?'said':e.k)}</b><span>${esc(e.v)}</span></li>`).join('');
    h=`<header class=dh><button class=ib data-open=group aria-label="back to group info">${ico('back',18)}</button><h2>${p.lead?'Lead':'Builder'} info</h2><button class=ib data-close aria-label="close details">${ico('close',18)}</button></header><div class=db>
      <section class="dsec hero">${av(p.name,'xl'+(p.lead?' lead':'')+(p.running?' on':''),col)}<h3>${esc(p.name)}</h3><p>${esc(p.lead?'Lead session · runs the brief and the gates':p.role+(p.type?' · '+p.type:''))}</p></section>
      <section class=dsec><dl class=kv><dt>State</dt><dd>${p.running?`<span style="color:var(--accent-ink)">${esc(doing(p.events[p.events.length-1])||'working')}</span>`:p.lead?'idle':esc(sl+(why?' · '+why:''))}</dd>${p.reason?`<dt>Note</dt><dd class=why>${esc(p.reason)}</dd>`:''}${p.started?`<dt>Ran</dt><dd>${esc(when(p.started))} · ${dur(p.started,p.running?new Date().toISOString():p.ended)}${p.tools?` · ${p.tools} tool calls`:''}</dd>`:''}</dl>
        <div class=meter style="margin-top:14px" title="context used"><span>${fmtK(p.context)} / ${fmtK(cap)}</span><div class=bar><i class="${pct>95?'bad':pct>75?'warn':''}" style="transform:scaleX(${pct/100})"></i></div></div></section>
      ${p.lead&&c.main.summary?`<section class=dsec><h4>Recap</h4><p class=recap>${esc(c.main.summary)}</p></section>`:''}
      ${!p.lead&&!p.running?`<section class=dsec><h4>Final report</h4><pre class=report>${esc(p.last_text||'(no final message)')}</pre></section>`:''}
      <section class=dsec><h4>Activity<span>${p.events.length} latest</span></h4><ul class=tl>${tl||'<li><span></span><span></span><span>nothing yet</span></li>'}</ul></section></div>`;
  }else{
    h=`<header class=dh><button class=ib data-close aria-label="close group info">${ico('close',18)}</button><h2>Group info</h2></header><div class=db>
      <section class="dsec hero"><span class="av xl" style="--c:${hue(c.repo+c.worktree)}" aria-hidden=true>${esc(initials(c.title))}</span><h3>${esc(c.title)}</h3><p>${esc(c.repo)}${c.worktree?' / '+esc(c.worktree):''}${c.branch&&c.branch!=='HEAD'?' · '+esc(c.branch):''}</p><p class=path>${esc(c.path)}</p></section>
      ${c.main.summary?`<section class=dsec><h4>Recap</h4><p class=recap>${esc(c.main.summary)}</p></section>`:''}
      ${c.job?`<section class=dsec><h4>Background job</h4><dl class=kv><dt>${esc(c.job.state)}</dt><dd>${esc(c.job.detail||'')} · ${fmtK(c.job.tokens)} tokens</dd></dl></section>`:''}
      <section class=dsec><h4>${c.people.length+1} members</h4>
        <div class=mem style="cursor:default">${av('You','',`var(--ink-2)`)}<span class=nm>You</span><span class=st>Terminal</span><span class=rl>you answer the gates in Claude Code</span></div>
        ${c.people.map(p=>`<button class=mem data-person="${esc(p.id)}">${av(p.name,(p.lead?'lead':'')+(p.running?' on':''),colOf(p.id,p.name))}<span class=nm>${esc(p.name)}</span>${statusPill(p)}<span class="rl ${p.running?'ty':''}">${esc(p.running?doing(p.events[p.events.length-1])||'working':p.lead?'lead session':p.reason||p.role)}</span></button>`).join('')}
      </section>
      <section class=dsec style="padding:6px 0"><button class=danger data-rm="${esc(c.cwd)}">${ico('trash',18)}<span>Remove from monitor<small>Hides this checkout until its next run. Transcripts stay.</small></span></button></section></div>`;
  }
  if(dr.dataset.k!==h){const sc=dr.querySelector('.db')?.scrollTop||0;unmountDiff();dr.dataset.k=h;dr.innerHTML=h;const db=dr.querySelector('.db');if(db)db.scrollTop=sc;}
}

let all=[];
const current=()=>all.find(c=>c.id===sel);
function render(){
  if(!data)return;
  all=chats(data);
  if(sel&&!all.some(c=>c.id===sel)) sel=null;
  if(!sel&&all.length&&window.innerWidth>820){sel=(all.find(c=>c.turn)||all.find(c=>c.busy.length)||all[0]).id;}
  const c=current();
  document.body.classList.toggle('open',!!c&&!!location.hash);
  markSeen(c);
  renderList(all); renderConv(c); renderDrawer(c);
}
function pick(id){sel=id;history.replaceState(null,'','#'+id);document.body.classList.add('open');opened.clear();if(drawer&&drawer.k!=='group')drawer=inChanges()?drawer:{k:'group'};unmountDiff();diffCache=null;diffCwd='';diffKey='';render();const c=current();if(c){diffCwd=c.cwd;loadDiff();}}

async function loadDiff(){
  if(!diffCwd)return;
  try{const r=await (await fetch('/diff?cwd='+encodeURIComponent(diffCwd))).json();
    const key=r.error||r.files.map(x=>x.path+x.status+x.add+x.del+x.patch.length).join('|');
    if(key!==diffKey){diffKey=key;diffCache=r;if(inChanges())paintDiff();else render();}
  }catch(e){diffKey='error';diffCache={error:'could not load the diff'};if(inChanges())paintDiff();else render();}
}
let diffRoot=null, diffBox=null, gdvLate=false;
setTimeout(()=>{gdvLate=true;if(!window.GDV&&inChanges())paintDiff();},6000);
const plainDiff=f=>`<div class=f><div class=fh><span class="fs ${esc(f.status)}">${esc(f.status)}</span><span class=fp>${esc(f.path)}</span>${viewed(f)}</div><div class=body>${f.patch?`<pre class=raw>${f.patch.split('\n').map(l=>`<span class="${l.startsWith('@@')?'h':l.startsWith('+')&&!l.startsWith('+++')?'a':l.startsWith('-')&&!l.startsWith('---')?'d':''}">${esc(l)||' '}</span>`).join('')}</pre>`:'<div class=bin>diff omitted, patch too large</div>'}</div></div>`;
function unmountDiff(){if(diffRoot){diffRoot.unmount();diffRoot=null;diffBox=null;}}
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
  const t=e.target;
  const rm=t.closest('[data-rm]'); if(rm){fetch('/forget?cwd='+encodeURIComponent(rm.dataset.rm),{method:'POST',headers:{'X-Gauntlet':'1'}}).then(()=>{drawer=null;sel=null;history.replaceState(null,'',location.pathname);tick();});return;}
  const row=t.closest('.row[data-id]'); if(row){pick(row.dataset.id);return;}
  const fl=t.closest('[data-filter]'); if(fl){filter=fl.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.setAttribute('aria-pressed',b===fl));render();return;}
  if(t.closest('[data-back]')){document.body.classList.remove('open');history.replaceState(null,'',location.pathname);drawer=null;render();return;}
  if(t.closest('[data-close]')){drawer=null;render();return;}
  const op=t.closest('[data-open]'); if(op){const k=op.dataset.open;drawer=drawer&&drawer.k===k&&op.closest('.ch')?null:{k};render();if(k==='changes')loadDiff();return;}
  const pe=t.closest('[data-person]'); if(pe){drawer={k:'person',id:pe.dataset.person};render();return;}
  const mo=t.closest('[data-more]'); if(mo){opened.has(mo.dataset.more)?opened.delete(mo.dataset.more):opened.add(mo.dataset.more);paintMsgs();return;}
  const md_=t.closest('.chg-head [data-mode]'); if(md_){diffMode=diffMode==='split'?'unified':'split';paintDiff();return;}
  if(t.closest('.chg-head [data-viewed]')){showViewed=!showViewed;paintDiff();return;}
  if(t.closest('.chg-head [data-tests]')){hideTests=!hideTests;store.set('hideTests',hideTests);paintDiff();return;}
  const fb=t.closest('#flist [data-file]'); if(fb){diffSel=fb.dataset.file;paintDiff();return;}
});
document.addEventListener('change',e=>{if(e.target.matches('#dv [data-read]'))toggleRead(shown().find(x=>x.path===diffSel));});
document.addEventListener('keydown',e=>{
  if(e.target.closest('input,textarea'))return;
  if(e.key==='Escape'){if(diffFull){diffFull=false;paintDiff();}else if(drawer){drawer=null;render();}return;}
  if(inChanges()&&diffCache&&!e.metaKey&&!e.ctrlKey){if(e.key==='[')stepFile(-1);if(e.key===']')stepFile(1);if(e.key==='f'){diffFull=!diffFull;paintDiff();}if(e.key==='v'){const f=shown().find(x=>x.path===diffSel);f&&toggleRead(f);}}
  // Up/down moves through the chat list when it has focus.
  const r=e.target.closest('.row');if(r&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();const rs=[...document.querySelectorAll('.row')],n=rs[rs.indexOf(r)+(e.key==='ArrowDown'?1:-1)];if(n){pick(n.dataset.id);document.querySelector(`.row[data-id="${CSS.escape(n.dataset.id)}"]`)?.focus();}}
});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)render();});
const statuses=d=>Object.fromEntries(d.worktrees.flatMap(w=>w.sessions.flatMap(s=>s.tasks.map(t=>[t.id,[t.who||t.name,t.running?'running':t.status]]))));
let raw='', seenSt=null;
async function tick(){
  let text;
  try{text=await (await fetch('/api')).text();}
  catch(err){document.body.classList.add('stale');$('#status').textContent=`Monitor offline · last update ${data?hhmm(new Date(data.now*1000).toISOString()):'never'} · retrying`;return;}
  document.body.classList.remove('stale');
  if(text===raw)return;
  raw=text; data=JSON.parse(text);
  const now=statuses(data);
  if(seenSt) $('#announce').textContent=Object.entries(now).filter(([id,[,st]])=>seenSt[id]&&seenSt[id][1]!==st&&st!=='running').map(([,[n,st]])=>`${n}: ${(STATUS[st]||[st])[0].toLowerCase()}`).join('. ');
  seenSt=now;
  render();
}
tick();setInterval(tick,2000);setInterval(loadChat,2500);setInterval(()=>{if(inChanges())loadDiff();},4000);
setTimeout(()=>{const c=current();if(c&&!diffCwd){diffCwd=c.cwd;loadDiff();}},300);
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api":
            body = json.dumps(snapshot()).encode(); ctype = "application/json"
        elif u.path == "/chat":
            body = json.dumps(chat(parse_qs(u.query).get("id", [""])[0])).encode(); ctype = "application/json"
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
