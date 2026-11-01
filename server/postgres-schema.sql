-- Factotum registry sync — Postgres Postgres schema.
--
-- Each Mac host is authoritative for its own Factotum registry
-- (~/Library/Application Support/Factotum/registry.json). The host's
-- factotum-mcp syncs a read replica into these tables for cross-host
-- visibility, parity checks, and future coordination. A host only ever
-- writes its own rows (host = 'host2' | 'host1').
--
-- Applied ad hoc (see the factotum repo's plan doc,
-- "Registry sync"). A formal Postgres migration adopting these tables is
-- the tracked follow-up; this file is the reviewable source of that DDL.
--
-- The factotum_sync role's password is pipeline-managed: it is set at
-- provisioning time and stored ONLY in each host's factotum-mcp .env as
-- FACTOTUM_DB_DSN (0600). It never appears in this file or any doc.

BEGIN;

CREATE TABLE IF NOT EXISTS factotum_registries (
    host         text PRIMARY KEY,
    generated_at timestamptz,
    synced_at    timestamptz NOT NULL DEFAULT now(),
    content_hash text,
    registry     jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS factotum_actions (
    host                  text NOT NULL,
    action_id             text NOT NULL,
    app_bundle_id         text,
    name                  text,
    op                    text,
    mechanism             text,
    risk                  text,
    status                text,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    last_exercised_at     timestamptz,
    last_exercise_outcome text,
    last_exercise_detail  text,
    PRIMARY KEY (host, action_id)
);

CREATE TABLE IF NOT EXISTS factotum_services (
    host          text NOT NULL,
    service_id    text NOT NULL,
    launchd_label text,
    kind          text,
    desired_state text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (host, service_id)
);

CREATE TABLE IF NOT EXISTS factotum_apps (
    host       text NOT NULL,
    bundle_id  text NOT NULL,
    name       text,
    version    text,
    installed  boolean,
    running    boolean,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (host, bundle_id)
);

-- Normalized package inventory (v1.4.0): one row per installed
-- top-level package per host, flattened from the registry's
-- packages section by the sync. `class` is the factotum-pkg policy
-- class (for casks: the artifact classification; for outdated items:
-- the plan class — auto / held / held-major / pinned /
-- self-updating / needs-manual). pipx/uv rows never carry
-- outdated=true: their outdated state is not enumerable.
CREATE TABLE IF NOT EXISTS factotum_packages (
    host              text NOT NULL,
    manager           text NOT NULL,
    name              text NOT NULL,
    kind              text,
    version           text,
    available_version text,
    class             text,
    outdated          boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (host, manager, name)
);

-- v1.5.0: per-row coverage state (inventoried / unversioned /
-- refused). factotum-mcp's sync probes for the column and degrades
-- to the pre-1.5.0 column set when it is absent, so this ALTER is
-- safe to apply before or after the 1.5.0 deploy.
ALTER TABLE factotum_packages ADD COLUMN IF NOT EXISTS coverage text;

-- v1.11.0 (internal ticket): canary exercise fields as queryable
-- columns on factotum_actions. On the live database these were
-- applied by a Postgres schema migration; the sync probes for the
-- columns and degrades to the pre-1.11.0 column set when they
-- are absent, so these ALTERs are safe to apply before or after
-- the 1.11.0 deploy. They are kept here so a from-scratch
-- application of this file stays equivalent.
ALTER TABLE factotum_actions ADD COLUMN IF NOT EXISTS last_exercised_at timestamptz;
ALTER TABLE factotum_actions ADD COLUMN IF NOT EXISTS last_exercise_outcome text;
ALTER TABLE factotum_actions ADD COLUMN IF NOT EXISTS last_exercise_detail text;

-- Sync role: login, no password here (set at provisioning). Grants cover
-- exactly the factotum_* tables and nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factotum_sync') THEN
        CREATE ROLE factotum_sync LOGIN;
    END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    factotum_registries, factotum_actions, factotum_services, factotum_apps,
    factotum_packages
    TO factotum_sync;

COMMIT;
