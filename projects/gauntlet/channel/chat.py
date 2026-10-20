#!/usr/bin/env python3
"""Gauntlet chat channel: an MCP stdio server inside a Claude Code session.

It carries what the user types in the monitor chat into this session, and carries permission prompts out
so the chat can answer them. It is active only in sessions the monitor launched: those set GAUNTLET_SESSION.
Every other session gets a quiet MCP server with no capabilities.
"""
import json, os, sys, threading, time, urllib.request
from urllib.parse import quote

SESSION = os.environ.get("GAUNTLET_SESSION", "")
MONITOR = f"http://127.0.0.1:{os.environ.get('GAUNTLET_PORT', '7777')}"
TOKEN = os.path.expanduser("~/.claude/gauntlet/token")
INSTRUCTIONS = ("Messages in <channel> tags from this server are typed by the user in the Gauntlet monitor chat. "
                "Treat each one exactly like a message the user typed in this terminal. Answer in your normal reply; "
                "the monitor shows your reply in the chat.")
out = threading.Lock()


def send(msg):
    with out:
        sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()


def call(path, body=None, timeout=40):
    try: token = open(TOKEN).read().strip()
    except OSError: token = ""
    req = urllib.request.Request(MONITOR + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Gauntlet-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read() or b"null")


def pump():
    """Long-poll the monitor and forward chat messages and permission answers into the session."""
    while True:
        try: items = call(f"/channel/inbox?session={quote(SESSION)}")
        except Exception: time.sleep(3); continue
        for it in items or []:
            if it.get("kind") == "permission":
                send({"jsonrpc": "2.0", "method": "notifications/claude/channel/permission",
                      "params": {"request_id": it["request_id"], "behavior": it["behavior"]}})
            else:
                send({"jsonrpc": "2.0", "method": "notifications/claude/channel", "params": {"content": it["text"], "meta": {"sender": "user"}}})


def ask(params):
    """Hand a permission prompt to the monitor; retry briefly if it is restarting."""
    for _ in range(5):
        try: call("/channel/permission", {"session": SESSION, **params}, timeout=5); return
        except Exception: time.sleep(1)


def main():
    for line in sys.stdin:
        try: msg = json.loads(line)
        except ValueError: continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            # Stay on a pre-2026-07-28 protocol revision: channel messages do not travel on the newer one.
            version = min(msg.get("params", {}).get("protocolVersion", "2025-06-18"), "2025-06-18")
            caps = {"experimental": {"claude/channel": {}, "claude/channel/permission": {}}} if SESSION else {}
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": version, "capabilities": caps,
                  "serverInfo": {"name": "gauntlet-chat", "version": "1"}, **({"instructions": INSTRUCTIONS} if SESSION else {})}})
        elif method == "notifications/initialized":
            if SESSION: threading.Thread(target=pump, daemon=True).start()
        elif method == "notifications/claude/channel/permission_request":
            threading.Thread(target=ask, args=(msg.get("params", {}),), daemon=True).start()
        elif mid is not None:
            if method == "ping": send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else: send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
