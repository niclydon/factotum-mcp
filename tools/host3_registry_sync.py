#!/usr/bin/env python3
"""factotum-host3-sync: collect the Host3 (PostgresOS) host state into the
Postgres factotum_* registry tables, as host "host3".

Phase 1 of the Host3 display/Host3 Factotum incorporation (see
a companion design doc, not included in this export): Host3 becomes a
third Factotum host for READ visibility. The collector runs on Host1,
polls postgresd's tailnet API (GET /health at 192.0.2.10:7871, header
x-origin-secret), reads the Host1-signed app catalog index, probes
wayvnc's TCP port, composes a registry document in the same shape the
Mac Factotum shell produces, and writes it with the same semantics as
factotum-mcp's sync_push (one transaction; blob upsert keyed on host;
normalized apps/actions/services rows replaced for this host;
canonical-hash short-circuit that only touches synced_at).

Secrets arrive via the environment, resolved by the launcher from the
vault at run time (FACTOTUM_DB_DSN via mapping factotum-host3-sync;
POSTGRESD_ORIGIN_SECRET via mapping postgresd). Nothing secret is read from
disk or printed.

Presence posture: Host3 is an Arduino appliance that may be powered
off or unplugged for long periods, so an unreachable postgresd is a
routine outcome, not a failure: the cycle exits 0, writes nothing
(the stored registry ages honestly and carries a presence block
declaring the intermittent model), and counts consecutive misses in
a small state file beside the script. A box that answers but refuses
(401/403/503) or a local configuration fault exits nonzero: that is
breakage, not absence.
"""

import hashlib
import json
import os
import socket
import sys
import urllib.request
from datetime import datetime, timezone

HOST = "host3"
COLLECTOR_VERSION = "1.1.0"
HEALTH_URL = os.environ.get("HOST3_HEALTH_URL", "http://192.0.2.10:7871/health")
VNC_HOST = os.environ.get("HOST3_VNC_HOST", "192.0.2.10")
VNC_PORT = int(os.environ.get("HOST3_VNC_PORT", "5900"))
CATALOG_INDEX = os.environ.get(
    "HOST3_CATALOG_INDEX", "~/projects/postgresos-apps/dist/index.json")

APP_NAMES = {
    "example.annunciator": "Annunciator",
    "example.bench": "Bench",
    "example.brief": "Brief",
    "example.ops": "Ops Room",
    "example.system": "System",
}

OK_COMPONENT_STATES = {"ok", "idle"}


def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path():
    return os.environ.get(
        "HOST3_SYNC_STATE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "host3-sync-state.json"))


def load_state():
    try:
        with open(state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(state_path(), "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def canonical_hash(reg):
    blob = json.dumps(reg, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_health():
    secret = os.environ.get("POSTGRESD_ORIGIN_SECRET", "")
    if not secret:
        raise RuntimeError("POSTGRESD_ORIGIN_SECRET is not set (vault mapping postgresd)")
    req = urllib.request.Request(HEALTH_URL, headers={"x-origin-secret": secret})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_tcp(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_catalog():
    """Returns (apps_list, provenance_note). Never raises: the health
    payload is the primary source; a missing index degrades apps to an
    empty list with the gap named in the registry provenance block."""
    try:
        with open(os.path.expanduser(CATALOG_INDEX)) as f:
            idx = json.load(f)
    except Exception as e:
        return [], None, "catalog index unreadable: %s" % e
    apps = [a for a in idx.get("apps", []) if HOST in (a.get("targets") or [])]
    return apps, idx, None


def compose_registry(health, catalog_apps, catalog_idx, catalog_error, vnc_ok):
    components = health.get("components") or {}
    degraded = ["%s=%s" % (k, v.get("state")) for k, v in sorted(components.items())
                if isinstance(v, dict) and v.get("state") not in OK_COMPONENT_STATES]
    version = health.get("version") or "unknown"
    build = health.get("build_sha") or "unknown"

    services = [
        {
            "id": "postgresd",
            "kind": "systemd",
            "launchd_label": "postgresd.service",
            "desired_state": "running",
            "live": {
                "running": True,
                "loaded": True,
                "pid": health.get("pid"),
                "verdict": {
                    "ok": True,
                    "state": "ok",
                    "reason": ("health answered on the tailnet API; degraded "
                               "components: " + ", ".join(degraded))
                    if degraded else "health answered; all components ok",
                },
            },
            "health": components,
            "actions": [],
            "grant_basis": {
                "basis": "tailnet_api",
                "detail": "postgresd tailnet API (x-origin-secret), 192.0.2.10:7871",
                "identity": "postgresd",
            },
            "notes": "Single on-device PostgresOS service: broker, shell host, "
                     "link, attention, approvals. Loopback API 127.0.0.1:7870 "
                     "serves the shell; the tailnet API serves /health.",
        },
        {
            "id": "wayvnc",
            "kind": "systemd",
            "launchd_label": "wayvnc.service",
            "desired_state": "running",
            "live": {
                "running": vnc_ok,
                "loaded": None,
                "verdict": {
                    "ok": vnc_ok,
                    "state": "ok" if vnc_ok else "down",
                    "reason": ("TCP probe %s:%d connect ok" % (VNC_HOST, VNC_PORT))
                    if vnc_ok else
                    ("TCP probe %s:%d connect failed" % (VNC_HOST, VNC_PORT)),
                },
            },
            "health": None,
            "actions": [],
            "notes": "Mirrors the PostgresOS session on both addresses: "
                     "192.0.2.10:5900 (tailnet) and 192.0.2.11:5900 (LAN). "
                     "State here is the collector's own TCP probe of the "
                     "tailnet address.",
        },
        {
            "id": "postgresos-session",
            "kind": "session",
            "launchd_label": "postgresos-session",
            "desired_state": "running",
            "live": {
                "running": (components.get("local_api") or {}).get("state") == "ok",
                "loaded": None,
                "verdict": {
                    "ok": (components.get("local_api") or {}).get("state") == "ok",
                    "state": "ok"
                    if (components.get("local_api") or {}).get("state") == "ok"
                    else "indeterminate",
                    "reason": "derived from the postgresd local_api component "
                              "state; active profile '%s'" % health.get("profile"),
                },
            },
            "health": None,
            "actions": [],
            "notes": "The Chromium kiosk shell session (Host3 display). Liveness is "
                     "derived from postgresd's local_api component, the API the "
                     "shell itself consumes; postgresd v1 reports no separate "
                     "session probe.",
        },
    ]

    apps = []
    for a in catalog_apps:
        bid = a.get("id")
        apps.append({
            "bundle_id": bid,
            "name": APP_NAMES.get(bid, bid),
            "version": a.get("version"),
            "installed": True,
            "running": False,
            "path": a.get("bundle"),
            "scriptable": False,
            "capability_hints": {
                "surfaces": a.get("surfaces") or [],
                "tier": a.get("tier"),
                "targets": a.get("targets") or [],
                "sha256": a.get("sha256"),
            },
        })

    actions = [
        {
            "id": "postgresd_health",
            "app_bundle_id": "postgresd",
            "name": "PostgresOS health (postgresd)",
            "op": "read",
            "mechanism": "tailnet API GET /health (192.0.2.10:7871)",
            "risk": "read",
            "status": "native",
            "tool": "factotum-host3-sync (Host1 collector)",
            "notes": "Component states, nodes, agents, disk; the same "
                     "payload Host3 display's System app reads.",
            "grant_basis": None,
        },
        {
            "id": "host3_vnc",
            "app_bundle_id": "postgresd",
            "name": "Host3 display screen (VNC)",
            "op": "read",
            "mechanism": "wayvnc 192.0.2.10:5900 (tailnet) / "
                         "192.0.2.11:5900 (LAN)",
            "risk": "read",
            "status": "catalog",
            "tool": "VNC client",
            "notes": "Connect pointer only; Phase 1 has no invoke path.",
            "grant_basis": None,
        },
    ]
    for a in catalog_apps:
        bid = a.get("id")
        short = bid.split(".", 1)[1] if "." in bid else bid
        actions.append({
            "id": "host3.launch.%s" % short,
            "app_bundle_id": bid,
            "name": "Launch %s on Host3 display" % APP_NAMES.get(bid, bid),
            "op": "launch",
            "mechanism": "PostgresOS shell app launch (catalog pointer; "
                         "invoke arrives with Phase 2)",
            "risk": "write",
            "status": "catalog",
            "tool": "PostgresOS shell",
            "notes": "Signed catalog app, tier %s, surfaces %s."
                     % (a.get("tier"), ", ".join(a.get("surfaces") or [])),
            "grant_basis": None,
        })

    generated = now_z()
    reg = {
        "host": HOST,
        "schema_version": 1,
        "presence": {
            "model": "intermittent",
            "expected_offline": True,
            "note": "Arduino UNO Q appliance on the bench; it may be "
                    "powered off or unplugged for long periods. "
                    "Unreachable means offline, not broken: judge this "
                    "host by last contact, not by freshness alarms.",
        },
        "factotum_version": "postgresd %s+%s (factotum-host3-sync %s)"
                            % (version, build, COLLECTOR_VERSION),
        "generated_at": generated,
        "disk": health.get("disk"),
        "payload": {
            "kind": "postgresd",
            "last_contact_at": generated,
            "version": version,
            "build_sha": build,
            "profile": health.get("profile"),
            "attention": health.get("attention"),
            "uptime_s": health.get("uptime_s"),
            "pid": health.get("pid"),
            "started_at_ms": health.get("started_at_ms"),
            "nodes": health.get("nodes"),
            "agents": health.get("agents"),
            "components": components,
            "bad_frames": health.get("bad_frames"),
            "unattributed_bad_frames": health.get("unattributed_bad_frames"),
        },
        "capabilities": {},
        "coverage": {},
        "permissions": {},
        "packages": [],
        "apps": apps,
        "actions": actions,
        "services": services,
        "host3_sync": {
            "collector": "factotum-host3-sync %s (Host1)" % COLLECTOR_VERSION,
            "health_url": HEALTH_URL,
            "catalog_index": CATALOG_INDEX,
            "catalog_built_at": (catalog_idx or {}).get("built_at"),
            "catalog_key_id": (catalog_idx or {}).get("key_id"),
            "catalog_error": catalog_error,
            "apps_provenance": "signed catalog index as built on Host1; "
                               "installed=True reflects the device catalog, "
                               "per-app pull state is not reported by the "
                               "postgresd v1 API",
        },
    }
    return reg


def write_registry(reg):
    dsn = os.environ.get("FACTOTUM_DB_DSN", "")
    if not dsn:
        raise RuntimeError("FACTOTUM_DB_DSN is not set "
                           "(vault mapping factotum-host3-sync)")
    import psycopg
    from psycopg.types.json import Jsonb

    host_override = os.environ.get("HOST3_DSN_HOST_OVERRIDE", "")
    if host_override:
        # The vaulted DSN names the tailnet address (right for the Macs,
        # whose connections arrive from their own tailnet IPs). From
        # Host1 itself that source address has no pg_hba entry; the
        # loopback address does. The systemd unit sets the override.
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        parts = conninfo_to_dict(dsn)
        parts["host"] = host_override
        dsn = make_conninfo(**parts)

    chash = canonical_hash(reg)
    with psycopg.connect(dsn, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM factotum_registries "
                        "WHERE host = %s FOR UPDATE", (HOST,))
            row = cur.fetchone()
            if row and row[0] == chash:
                cur.execute("UPDATE factotum_registries SET synced_at = now() "
                            "WHERE host = %s", (HOST,))
                conn.commit()
                return {"ok": True, "host": HOST, "changed": False,
                        "content_hash": chash}
            cur.execute(
                "INSERT INTO factotum_registries "
                "(host, generated_at, synced_at, content_hash, registry) "
                "VALUES (%s, %s, now(), %s, %s) "
                "ON CONFLICT (host) DO UPDATE SET "
                "generated_at = EXCLUDED.generated_at, "
                "synced_at = now(), "
                "content_hash = EXCLUDED.content_hash, "
                "registry = EXCLUDED.registry",
                (HOST, parse_ts(reg.get("generated_at")), chash, Jsonb(reg)))
            cur.execute("DELETE FROM factotum_apps WHERE host = %s", (HOST,))
            cur.execute("DELETE FROM factotum_actions WHERE host = %s", (HOST,))
            cur.execute("DELETE FROM factotum_services WHERE host = %s", (HOST,))
            apps = [(HOST, a.get("bundle_id"), a.get("name"), a.get("version"),
                     a.get("installed"), a.get("running"))
                    for a in reg.get("apps", []) if a.get("bundle_id")]
            if apps:
                cur.executemany(
                    "INSERT INTO factotum_apps "
                    "(host, bundle_id, name, version, installed, running) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", apps)
            actions = [(HOST, a.get("id"), a.get("app_bundle_id"),
                        a.get("name"), a.get("op"), a.get("mechanism"),
                        a.get("risk"), a.get("status"))
                       for a in reg.get("actions", []) if a.get("id")]
            if actions:
                cur.executemany(
                    "INSERT INTO factotum_actions "
                    "(host, action_id, app_bundle_id, name, op, mechanism, "
                    "risk, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    actions)
            services = [(HOST, s.get("id"), s.get("launchd_label"),
                         s.get("kind"), s.get("desired_state"))
                        for s in reg.get("services", []) if s.get("id")]
            if services:
                cur.executemany(
                    "INSERT INTO factotum_services "
                    "(host, service_id, launchd_label, kind, desired_state) "
                    "VALUES (%s, %s, %s, %s, %s)", services)
            conn.commit()
    return {"ok": True, "host": HOST, "changed": True, "content_hash": chash,
            "counts": {"apps": len(apps), "actions": len(actions),
                       "services": len(services)}}


def main():
    dry_run = "--dry-run" in sys.argv
    try:
        health = fetch_health()
    except urllib.error.HTTPError as e:
        # The box answered and refused us (401/403: our credential is
        # wrong; 503: listener up but unprovisioned). That is a fault
        # worth a failing unit, not an offline appliance.
        print(json.dumps({"ok": False, "host": HOST, "stage": "health",
                          "fault": "http_%d" % e.code, "error": str(e)}))
        return 3
    except Exception as e:
        if isinstance(e, RuntimeError):
            # Local configuration faults (e.g. the vault secret did not
            # resolve) are collector faults, not an offline appliance.
            print(json.dumps({"ok": False, "host": HOST, "stage": "health",
                              "fault": "config", "error": str(e)}))
            return 3
        # Connection refused, timeout, no route: Host3 is an Arduino
        # appliance that may be powered off or unplugged for long
        # periods. Unreachable is a routine outcome for this host, so
        # the cycle succeeds, writes nothing (the stored registry ages
        # honestly), and records the miss in the collector state.
        state = load_state()
        state["consecutive_unreachable"] = \
            int(state.get("consecutive_unreachable") or 0) + 1
        state["last_unreachable_at"] = now_z()
        save_state(state)
        print(json.dumps({"ok": True, "host": HOST,
                          "host_state": "unreachable",
                          "consecutive_unreachable":
                              state["consecutive_unreachable"],
                          "error": str(e)}))
        return 0
    state = load_state()
    if state.get("consecutive_unreachable"):
        state["consecutive_unreachable"] = 0
    state["last_contact_at"] = now_z()
    save_state(state)
    catalog_apps, catalog_idx, catalog_error = load_catalog()
    vnc_ok = probe_tcp(VNC_HOST, VNC_PORT)
    reg = compose_registry(health, catalog_apps, catalog_idx, catalog_error,
                           vnc_ok)
    if dry_run:
        print(json.dumps({"ok": True, "dry_run": True,
                          "content_hash": canonical_hash(reg),
                          "services": [s["id"] for s in reg["services"]],
                          "apps": len(reg["apps"]),
                          "actions": len(reg["actions"])}, indent=1))
        return 0
    try:
        out = write_registry(reg)
    except Exception as e:
        print(json.dumps({"ok": False, "stage": "write", "error": str(e)}))
        return 3
    out["generated_at"] = reg["generated_at"]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
