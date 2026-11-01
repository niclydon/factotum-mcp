#!/usr/bin/env python3
"""Generate an EXAMPLE Factotum registry seed for a fictional host.

This is the public-export replacement for the private per-host seed
generator. The private version carries one real host's hand-maintained
action catalog (its real apps, services, launchd labels, and sibling
tools) and stays private permanently. This example keeps the exact
same registry shape and generation mechanism, populated entirely
with synthetic entries for a host called "host2", so a reader can
see what a seed produces and adapt it to their own machine.

Run ON the example host. Produces
~/Library/Application Support/Factotum/registry.json with a live
discovery snapshot of installed apps (running flags via lsappinfo,
falling back to ps) merged with the small hand-maintained example
action catalog below.
"""
import json
import os
import plistlib
import subprocess
import sys
from datetime import datetime, timezone

HOST = "host2"
SUPPORT = os.path.expanduser("~/Library/Application Support/Factotum")
OUT = os.path.join(SUPPORT, "registry.json")
ROOTS = ["/Applications", "/System/Applications",
         os.path.expanduser("~/Applications")]
ALLOWLIST_PATH = os.path.expanduser("~/services/example-allowlist.json")


def running_bundle_ids():
    ids = set()
    try:
        out = subprocess.run(["lsappinfo", "list"], capture_output=True,
                             text=True, timeout=15).stdout
        import re
        ids.update(re.findall(r'bundleID="([^"]+)"', out))
        ids.update(re.findall(r'\b([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+){2,})\b',
                              out))
    except Exception:
        pass
    if not ids:
        try:
            out = subprocess.run(["ps", "-axo", "command="], capture_output=True,
                                  text=True, timeout=15).stdout
            for line in out.splitlines():
                if ".app/Contents/MacOS/" in line:
                    ids.add(line.strip().split(".app/")[0] + ".app")
        except Exception:
            pass
    return ids


def scan_apps(running):
    apps = []
    seen = set()
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".app"):
                continue
            path = os.path.join(root, name)
            bid = ver = None
            try:
                with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
                    pl = plistlib.load(f)
                bid = pl.get("CFBundleIdentifier")
                ver = pl.get("CFBundleShortVersionString")
            except Exception:
                pass
            key = bid or path
            if key in seen:
                continue
            seen.add(key)
            is_running = (bid in running) or (path in running)
            apps.append({"bundle_id": bid, "name": name[:-4],
                         "version": ver, "path": path,
                         "installed": True, "running": bool(is_running)})
    return apps


def ensure_app(apps, entry, running):
    for a in apps:
        if a.get("bundle_id") == entry["bundle_id"]:
            a.update({k: v for k, v in entry.items() if v is not None})
            return
    e = dict(entry)
    e.setdefault("installed", os.path.isdir(e.get("path") or ""))
    e.setdefault("running", e["bundle_id"] in running)
    e.setdefault("version", None)
    apps.append(e)


def actions():
    # Catalog pointers to fictional sibling tools on the same host.
    photos = {"mcp": "example-photos-mcp", "host": "host2", "port": 7891,
              "caller": "~/bin/example-photos-call"}
    runner = {"mcp": "example-runner-mcp", "host": "host2", "port": 7897,
              "caller": "~/bin/example-runner-call"}
    A = []

    def add(id, app, name, op, mech, risk, status, notes, pointer=None,
            app_name=None):
        a = {"id": id, "app_bundle_id": app, "name": name, "op": op,
             "mechanism": mech, "risk": risk, "status": status,
             "notes": notes}
        if app_name:
            a["app_name"] = app_name
        if pointer:
            a["pointer"] = pointer
        A.append(a)

    # Native actions on the Factotum app itself (example bundle id).
    F = "com.example.factotum"
    invoke_tool = "factotum-mcp: factotum_invoke (127.0.0.1:7901)"

    def nadd(id, name, op, mech, tool, risk, notes):
        A.append({"id": id, "app_bundle_id": F, "name": name, "op": op,
                  "mechanism": mech, "risk": risk, "status": "native",
                  "notes": notes,
                  "pointer": {"kind": "tool", "tool": tool,
                              "via": invoke_tool}})

    nadd("factotum.health", "Factotum health", "read", "native",
         "factotum_health", "read", "One-call health roll-up for the host.")
    nadd("factotum.registry_read", "Read registry", "read", "native",
         "factotum_registry", "read", "The registry document itself.")
    nadd("factotum.discovery_run", "Run discovery", "update", "native",
         "factotum_discovery_run", "write",
         "Ask the app to rediscover installed apps and rewrite the registry.")

    # Catalog actions: owned by sibling tools, never invoked through here.
    add("photos.fetch", "com.example.photos", "Fetch a photo", "read",
        "mcp", "read", "catalog",
        "Example catalog action owned by the photos tool.",
        pointer=dict(photos, command="fetch"))
    add("photos.status", "com.example.photos", "Photos tool status", "read",
        "mcp", "read", "catalog", "Example catalog status action.",
        pointer=dict(photos, command="status"))
    add("runner.run", "com.example.runner", "Run an allowlisted item",
        "update", "mcp", "write", "catalog",
        "Example catalog action owned by the runner tool; the allowlist "
        "lives with that tool, not in this registry.",
        pointer=dict(runner, command="run"))
    return A


ALL5 = ["status", "start", "stop", "restart", "logs_recent"]


def services():
    """Managed services on the example host, in the shared app's
    serviceEntry shape: {id, launchd_label, kind, repo, desired_state,
    health{type,target}, actions[5], log_paths[], notes}."""
    S = []

    def add(id, label, kind, repo, desired, htype, htarget, logs, notes):
        S.append({"id": id, "launchd_label": label, "kind": kind,
                  "repo": repo, "desired_state": desired,
                  "health": {"type": htype, "target": htarget},
                  "actions": list(ALL5), "log_paths": logs, "notes": notes})

    home = os.path.expanduser("~")
    sup = home + "/Library/Application Support/Factotum"
    add("factotum", "com.example.factotum", "native", "factotum", "running",
        "launchd", "com.example.factotum",
        [home + "/Library/Logs/factotum.out.log", sup + "/factotum.log"],
        "Factotum itself (example entry).")
    add("factotum-mcp", "com.example.factotum-mcp", "native", "factotum-mcp",
        "running", "http", "http://127.0.0.1:7901/health",
        ["~/services/factotum-mcp/service.log"],
        "This MCP server. Loopback 7901, origin-secret guarded.")
    add("example-photos-mcp", "com.example.photos-mcp", "supervised",
        "example-photos", "running", "port", 7891,
        ["~/services/example-photos-mcp/service.log"],
        "Fictional sibling MCP front-end (loopback 7891).")
    return S


def main():
    running = running_bundle_ids()
    apps = scan_apps(running)
    for entry in [
        {"bundle_id": "com.example.factotum", "name": "Factotum",
         "version": "1.0.0",
         "path": os.path.expanduser("~/Applications/Factotum.app")},
        {"bundle_id": "com.example.photos", "name": "Example Photos Tool",
         "version": "1.0.0",
         "path": os.path.expanduser("~/services/example-photos")},
    ]:
        ensure_app(apps, entry, running)
    reg = {"host": HOST,
           "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "apps": apps, "actions": actions(), "services": services()}
    os.makedirs(SUPPORT, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=1, sort_keys=False)
    os.rename(tmp, OUT)
    print("apps=%d actions=%d services=%d stamp=%s" % (
        len(apps), len(reg["actions"]), len(reg["services"]),
        reg["generated_at"]))


if __name__ == "__main__":
    sys.exit(main())
