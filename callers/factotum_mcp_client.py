#!/usr/bin/env python3
"""Minimal MCP streamable-HTTP client (stdlib only).

Piped to the target host over SSH stdin by the factotum-mcp-call /
factotum-mcp-call-host2 wrappers. Reads the origin shared secret
transiently from factotum-mcp's .env on that host; the secret never
leaves the host and never touches this VM's disk.

Usage (on host): python3 - <port> <tool|__list> <args-json-b64> <env-file> <secret-var>
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request


def load_secret(env_file, var):
    with open(os.path.expanduser(env_file)) as f:
        for line in f:
            line = line.strip()
            if line.startswith(var + "="):
                v = line.split("=", 1)[1]
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
    raise SystemExit("secret var %s not found in %s" % (var, env_file))


def main():
    port, tool, args_b64, env_file, secret_var = sys.argv[1:6]
    secret = load_secret(env_file, secret_var)
    base = "http://127.0.0.1:%s/mcp" % port

    def post(payload, session=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "X-Origin-Secret": secret}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(base, data=json.dumps(payload).encode(),
                                       headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return r.headers.get("Mcp-Session-Id"), r.read().decode()
        except urllib.error.HTTPError as e:
            sys.stderr.write("HTTP %s: %s\n" % (e.code, e.read().decode()[:500]))
            raise SystemExit(1)

    def parse_sse(raw):
        m = re.search(r"^data: (\{.*\})", raw, re.M)
        return json.loads(m.group(1)) if m else json.loads(raw)

    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "caller", "version": "1.0"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session=sid)
    if tool == "__list":
        _, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                       "params": {}}, session=sid)
        out = [{"name": t["name"], "description": t.get("description", "")}
               for t in parse_sse(raw)["result"]["tools"]]
    else:
        args = json.loads(base64.b64decode(args_b64).decode() or "{}")
        _, raw = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}, session=sid)
        out = parse_sse(raw)["result"]
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")


main()
