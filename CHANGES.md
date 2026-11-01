# Changes

## Public export v1 -- factotum-mcp 1.14.2

First public export. The private repo's history is not carried
over; this changelog starts here.

factotum-mcp 1.14.2 is the registry-driven MCP server for Factotum:
per-host registry custody and Postgres sync, the generated gateway
surface (gateway/ in this repo), capability candidates, and the
leo/VUIndex photo-index point queries. The full test suite lives
under `server/` and `gateway/tests/` and runs inside this tree
(`./run_tests.sh`).

Export notes: the private per-host registry seed is replaced by
`server/example-seed-registry.py` (synthetic entries for the
example host `host2`); gateway callers resolve their token from
the `FACTOTUM_GATEWAY_TOKEN` environment variable; the example
hosts are `host1` and `host2` and the operator's account is
`example`. The third-host registry collector ships as
`tools/host3_registry_sync.py` (collector 1.1.0): it polls an
intermittently-present appliance host (`host3`) and syncs its
registry into the same Postgres tables; its endpoint defaults
are documentation-range example addresses, overridable through
the environment variables named in the file.
