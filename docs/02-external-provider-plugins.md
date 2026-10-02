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
   - `branch` -- optional; the branch `up --ep <name>` switches the checkout to on every run. Omit it to keep refreshing whatever branch the checkout is already on. `--ep <name>@<branch>` overrides it for one run.
   - `compose_file` -- path to that provider's own overlay, normally `external-providers/<name>.local.yaml`.
   - `supporting_compose_files` -- optional list of shared overlays, loaded before the provider overlay and deduplicated when multiple providers use one VPN.
   - `internal_url` -- where Family Librarian reaches this provider over the Compose network. If the provider has a VPN sidecar, this **must** be the sidecar's own service name (`network_mode: "service:<vpn>"` means the app has no network identity of its own -- see the overlay example's comments).
   - `app_service` -- the provider's own app container, used for `compose exec` (metadata sync) and restarts.
   - `vpn_service` -- optional; omit entirely for a provider with no VPN requirement.
   - `companion_services` -- optional list of *other* services that share the VPN sidecar's network namespace (a provider that ships its own downloader or indexer manager inside the tunnel). Restarted after the sidecar by `restart-external-provider`, including when another provider sharing that sidecar is selected, and reported by `status` as `degraded` if one is down.
   - `source_dir_env` -- the env var name (pick anything under `FAMILY_LIBRARIAN_<NAME>_*`) that the overlay's `build: context:` reads.
   - `api_key_env` -- the env var name in `lab.env` holding the bearer token Family Librarian should send this provider.
   - `sync_command` / `rebuild_command` -- optional; omit either (or both) if the provider has no such step.
2. `cp external-providers.overlay.local.yaml.example external-providers/<name>.local.yaml` and fill in the real Compose services: real image, real container-facing env var names, real volumes. Keep every literal secret and real env-var name in this file only -- read it from a generic `FAMILY_LIBRARIAN_<NAME>_*` variable in `lab.env`, never hardcode it here.
3. Add every variable the overlay references to `lab.env` (not `lab.env.example` -- that file is tracked and must stay generic; see its own comment block).
4. `./lab up --ep <name>` -- brings up the base profile plus this provider's overlay. Checks out the provider's source (if `repo_url` is set), refreshing it to the latest commit on every run -- on `<branch>` if you pass `--ep <name>@<branch>`, else on the registry's `branch:` if set, else on the checkout's current branch -- then builds its image and waits for the stack to be healthy.
5. `./lab base register-external-provider --ep <name>` -- registers the provider with Family Librarian (create, set API key, test connection, enable). Deliberately separate from `up`: this makes a real, live call against the provider's own upstream, which can be slow or hang (a VPN-gated provider refuses rather than silently bypassing a missing VPN).
6. `./lab base sync-external-provider-metadata --ep <name>` -- if the provider has `sync_command`/`rebuild_command` configured, runs them inside the running app container. This is a one-time, persistent-volume operation against potentially real, slow, live data -- re-running `up`/`run` reuses the already-populated data volume rather than re-syncing.

## Known limitation: VPN sidecar restarts

If a provider's VPN sidecar container restarts for any reason, the app container sharing its network namespace (and any `companion_services`) goes silently network-dead -- `network_mode: "service:<vpn>"` binds to a specific namespace instance that doesn't survive the sidecar being recreated, and there's no autoheal for this yet. Run `./lab base restart-external-provider --ep <name>` (restarts the sidecar, then all companions and apps using it, in that order) if a provider that was working suddenly can't reach anything.

## Tearing down

`./lab base down` removes provider containers too, via Compose's own `--remove-orphans` (they're "orphans" relative to a down invocation that doesn't pass the provider's overlay file) -- no separate teardown command needed.

## Providers that bring their own stack

A provider's own repo may ship a `compose.*.yml` for *its* standalone development (its own Compose project name, published ports, helper scripts). Those files are not this lab: this lab never reads them and their project name is unrelated to the `family-librarian-lab` project. Whatever the provider needs at runtime -- extra services, volumes, secrets -- is re-declared in `external-providers/<name>.local.yaml`, so the whole stack joins the lab's project and network and is torn down with `./lab base down`.

A provider that needs more than its own container (e.g. an indexer manager and downloaders that must egress through the same VPN tunnel) lists the extras in `companion_services`. Two things to get right in its overlay:

- Every service that shares the tunnel uses `network_mode: "service:<vpn>"`, so ports are published on the sidecar (Compose refuses `ports:` combined with `network_mode: service:*`) and any two listeners in the namespace need distinct ports.
- Anything that has to exist before `up --wait` can succeed (a config file, a directory owned by the right uid) must be created by a run-once init service the dependent container waits on with `condition: service_completed_successfully`. A manual post-`up` setup script cannot work: `up` waits for healthchecks and fails first.

## Shared VPN and management links

When providers share a VPN, declare its Compose service in one local supporting overlay and list that file in each provider's `supporting_compose_files`. Give the VPN a neutral service name. Publish each provider's distinct listener port on the VPN service. A single provider's `up --ep` must include its supporting overlay. `up --ep` leaves other providers running rather than removing them as Compose orphans; use `base down` for full teardown.

`LAB_EXTERNAL_HOST` is the browser-facing host name. Provider overlays construct their absolute management URL from that host, their published admin port, and the provider's `/admin` path; the internal provider URL remains the Compose service address. Do not reuse Family Librarian's `FAMILY_LIBRARIAN_PUBLIC_ORIGIN` for a provider on another port. Keep indexer and downloader UIs bound to loopback even when the provider admin port is reachable remotely. Read every credential from `lab.env` with a required interpolation; do not put literal credential fallbacks in an overlay.
