# External-provider plugins

## Purpose

This lab already has one external-provider integration built in for CI: `external-provider-fixture`, Family Librarian's own protocol-v2 conformance sample, wired via `--profile external-provider` (see `tests/test_external_provider.py`). That fixture is fake data and lives in the product repo itself.

This mechanism is for the opposite case: a real, private, third-party provider -- possibly more than one over time -- each with its own repo, its own real secrets, and sometimes a VPN sidecar for egress. Because `family-librarian-lab` is a public repo, **no tracked file may ever name a specific real provider, its upstream, or its secrets.** Every real provider's identity lives entirely in gitignored files you create locally; the tracked code (`family_librarian_lab/commands.py`) only knows how to read that data generically.

## What's tracked vs. what's yours to create

| Path | Tracked? | Contents |
| --- | --- | --- |
| `external-providers.local.yaml.example` | Yes (template) | Copy to `external-providers.local.yaml` |
| `external-providers.local.yaml` | No (gitignored) | One entry per real provider -- see fields below |
| `external-providers.overlay.local.yaml.example` | Yes (template) | Copy to `external-providers/<name>.local.yaml`, one per provider |
| `external-providers/<name>.local.yaml` | No (`external-providers/` is entirely gitignored) | That provider's own Compose services, real env var names, real secrets |
| `lab.env` | No (gitignored) | Generic `FAMILY_LIBRARIAN_<NAME>_*` values the registry/overlay files above read |

## Setup steps

1. `cp external-providers.local.yaml.example external-providers.local.yaml` and fill in one `providers:` entry for your real provider. Field reference:
   - `name` -- a short local id, your own choice. This is the value you pass to every `--ep NAME` flag below.
   - `repo_url` -- the private repo to check out. Landed at its own keyed checkout (`repos/keyed/<name>`, separate from Family Librarian's own `repos/family-librarian`) so multiple providers never collide.
   - `compose_file` -- path to that provider's own overlay, normally `external-providers/<name>.local.yaml`.
   - `internal_url` -- where Family Librarian reaches this provider over the Compose network. If the provider has a VPN sidecar, this **must** be the sidecar's own service name (`network_mode: "service:<vpn>"` means the app has no network identity of its own -- see the overlay example's comments).
   - `app_service` -- the provider's own app container, used for `compose exec` (metadata sync) and restarts.
   - `vpn_service` -- optional; omit entirely for a provider with no VPN requirement.
   - `source_dir_env` -- the env var name (pick anything under `FAMILY_LIBRARIAN_<NAME>_*`) that the overlay's `build: context:` reads.
   - `api_key_env` -- the env var name in `lab.env` holding the bearer token Family Librarian should send this provider.
   - `sync_command` / `rebuild_command` -- optional; omit either (or both) if the provider has no such step.
2. `cp external-providers.overlay.local.yaml.example external-providers/<name>.local.yaml` and fill in the real Compose services: real image, real container-facing env var names, real volumes. Keep every literal secret and real env-var name in this file only -- read it from a generic `FAMILY_LIBRARIAN_<NAME>_*` variable in `lab.env`, never hardcode it here.
3. Add the generic variables the overlay references to `lab.env` (not `lab.env.example` -- that file is tracked and must stay generic; see its own comment block).
4. `./lab up --ep <name>` -- brings up the base profile plus this provider's overlay. Checks out the provider's source (if `repo_url` is set), builds its image, and waits for the stack to be healthy.
5. `./lab base register-external-provider --ep <name>` -- registers the provider with Family Librarian (create, set API key, test connection, enable). Deliberately separate from `up`: this makes a real, live call against the provider's own upstream, which can be slow or hang (a VPN-gated provider refuses rather than silently bypassing a missing VPN).
6. `./lab base sync-external-provider-metadata --ep <name>` -- if the provider has `sync_command`/`rebuild_command` configured, runs them inside the running app container. This is a one-time, persistent-volume operation against potentially real, slow, live data -- re-running `up`/`run` reuses the already-populated data volume rather than re-syncing.

## Known limitation: VPN sidecar restarts

If a provider's VPN sidecar container restarts for any reason, the app container sharing its network namespace goes silently network-dead -- `network_mode: "service:<vpn>"` binds to a specific namespace instance that doesn't survive the sidecar being recreated, and there's no autoheal for this yet. Run `./lab base restart-external-provider --ep <name>` (restarts the sidecar, then the app, in that order) if a provider that was working suddenly can't reach anything.

## Tearing down

`./lab base down` removes provider containers too, via Compose's own `--remove-orphans` (they're "orphans" relative to a down invocation that doesn't pass the provider's overlay file) -- no separate teardown command needed.
