# Anthropic identity is the (account_uuid, org_uuid) pair, not account_uuid alone

One Anthropic `account_uuid` can hold more than one differently-organized subscription: an organization (Team) seat and a personal Max plan on the same `account_uuid` return different `organization.uuid` values (measured 2026-09-22). Profile identity keyed on `account_uuid` alone collapsed two separately-billed subscriptions into one Profile — registering, re-authing or importing the second silently overwrote the first's credential, with no warning.

Identity is now the pair. `find_by_account_and_org()` replaces the bare `account_uuid` match everywhere an OAuth Profile is looked up for reuse — the shared upsert, the dashboard's `POST /api/profiles`, and `export_import`. An exact pair match reuses the Profile; no match at all creates a new one.

A Profile whose organization can't be proven is never reused on a guess. Profiles written before `org_uuid` existed carry `org_uuid=None`; rather than trust whatever the *next* login happens to report, `resolve_legacy_oauth_match()` asks that Profile's own stored credential what it is (`resolve_profile_own_identity()`). Same organization: reuse and backfill, as before. A different organization: leave the legacy Profile's credential and name untouched, backfill it with its own true org fields so it stops looking legacy, and create a separate Profile for the incoming login. Unresolvable — no stored token, a decode error, a network error — means create a new Profile rather than overwrite. `cli.reauth()` is the one path allowed to reuse an unresolvable legacy target anyway, because it already knows which Profile the user picked and only reuses after its own interactive confirmation names the organization about to be recorded.

Export/import gets the same protection. A bundle carries `org_uuid`/`organization_type` alongside `account_uuid`; one written before those fields existed is resolved from its own carried credential before `apply_import()` decides reuse vs. new Profile, and a refused overwrite is reported (`profiles_overwrite_blocked`) rather than silently skipped or silently applied.

Status: accepted
