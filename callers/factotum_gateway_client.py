#!/usr/bin/env python3
"""Minimal MCP streamable-HTTP client for factotum-gateway (stdlib only).

Piped to the target host over SSH stdin by the factotum-gateway-call /
factotum-gateway-call-host2 wrappers, following the caller
pattern. The difference from factotum_mcp_client.py: the gateway's
bearer token is supplied to the wrapper through the
FACTOTUM_GATEWAY_TOKEN environment variable (one shared token per
host, D2); the wrapper passes it to this client via a stdin payload (the first stdin line is a
JSON payload, injected into this process's environment by the wrapper's
bootstrap; the token never appears in argv and is never read from any
file on the host).

The payload carries: token, bind (the host's tailnet IP), port, tool,
args, consumer. This client speaks to the gateway at http://bind:port.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


def main():
    payload = json.loads(os.environ["GW_PAYLOAD"])
    token = payload["token"]
    base = "http://%s:%s/mcp" % (payload["bind"], payload["port"])
    tool = payload["tool"]
    consumer = payload.get("consumer") or "unknown"

    def post(body, session=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + token,
                   "X-Consumer": consumer}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(base, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.headers.get("Mcp-Session-Id"), r.read().decode()
        except urllib.error.HTTPError as e:
            sys.stderr.write("HTTP %s: %s\n"
                             % (e.code, e.read().decode()[:500]))
            raise SystemExit(1)

    def parse_sse(raw):
        m = re.search(r"^data: (\{.*\})", raw, re.M)
        return json.loads(m.group(1)) if m else json.loads(raw)

    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": consumer,
                                             "version": "1.0"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"},
         session=sid)
    if tool == "__list":
        _, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                       "params": {}}, session=sid)
        out = parse_sse(raw)["result"]["tools"]
    else:
        _, raw = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": tool,
                                  "arguments": payload.get("args") or {}}},
                      session=sid)
        out = parse_sse(raw)["result"]
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")


main()
