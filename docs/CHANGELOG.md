# Changelog

## v1.6.1 (2026-10-24)

> **Upgrade note**: requires Devops-Glue 2.11 or later. 

- **Added**
  - The image-registry type comes from the Devops-Glue contract: Harbor (default) or a standard OCI registry (registry:2 and alike). The Image Registry page follows the current type and shows it next to the page title.
  - A standard registry supports anonymous and user/password authentication; it has no vulnerability scanning, image size or push time, and the page hides those columns, buttons and actions.
  - The CI project mapping field is read from `registry_repository` (the new name in Devops-Glue 2.11+); the old `harbor_repository` is still accepted.
  - Registries the contract declares as plain HTTP (`registry_url` starting with `http://`) are checked against the target docker daemon's `insecure-registries` before login and pull; one that is not allowed fails the deploy with copy-pasteable fix commands, and `REGISTRY_INSECURE_AUTO=true` lets CD back up and merge `/etc/docker/daemon.json` and reload docker itself (off by default).
- **Removed**
  - The local registry address and type configuration; both come only from the contract, while the registry credentials and the certificate-check setting stay local. Without an address from the contract, deploys and the Image Registry page fail with a clear error instead of building an image reference without an address; when the contract endpoint is briefly unreachable, the last known values are reused.
  - Artifact-tag deletion on the Image Registry page: neither Harbor nor a standard registry supports it, so the action and its confirmation dialog are gone.
- **Improved**
  - When the registry type has no vulnerability scanning, a scan request answers with a clear "this registry type does not support vulnerability scanning" message.
  - When the registry type cannot be retrieved, the page shows "unknown (not received from the contract)" instead of defaulting to Harbor; the actual backend selection is unchanged.
- **Fixed**
  - Syncing against a standard OCI registry no longer fails, and a failed sync no longer leaves empty  rows; leftover empty rows are cleaned up automatically on the next sync.
  - When the registry account has no permission to query, sync reports it clearly instead of faking success; a scan report that cannot be fetched states the reason instead of showing "no vulnerabilities found".
  - A pull that fails because the target daemon only speaks HTTPS to a plain-HTTP registry no longer falls back to Docker Hub, which could deploy a same-named public image; the deploy stops with the original error and the fix instructions instead.

## v1.6.0 (2026-10-14)

> **⚠️ Breaking change** — v1.6.0 is not backward-compatible with v1.5.x and earlier. Local password login is removed in favour of Devops-Glue OIDC single sign-on. Before upgrading, set `OIDC_ISSUER`, `CD_OIDC_SECRET` and `CD_OIDC_REDIRECT_URI` in the root `.env`, and enable Devops-CD under "API Integration Services → Enable" in the Glue console. Requires Devops-Glue 2.9.0 or later.

- **Added**
  - OIDC single sign-on is the only way in; local password login is gone.
  - Enablement is asked from Glue's contract endpoint. When it cannot be reached, CD fails closed with 503 instead of reporting "not enabled", and never falls back to the shared table. The answer is cached for 30 seconds by default.
  - The build page lists Git tags alongside branches; on an older Glue it falls back to branches only.
  - Deploy logs are served to CI over HTTP behind an inbound static token, with filters and pagination capped at 100 rows. The shared read-only view is removed.
  - With automatic provisioning, the first database connection creates every CD table, adds missing columns and indexes, and self-checks the inventory.
- **Fixed**
  - WebShell authentication matches the REST path: access and account-disabled checks are applied, and the permission branch fails closed.
- **Improved**
  - The gate is contract state × inventory completeness: "not enabled" and "missing table" are two distinct 503s, the latter naming the missing tables. The hint follows the provisioning mode — a restart under automatic provisioning, the init script under manual.
  - Manual provisioning issues no DDL; only idempotent column additions on tables that already exist.
  - The monitoring pages list servers immediately and probe each host concurrently, so one unreachable host only affects its own row.
  - Outbound connections can be pinned to IPv4 or IPv6 instead of trying IPv6 first.
  - CD no longer creates indexes on the shared CI tables; those belong to Glue's schema script.
  - The frontend builds without warnings.

## v1.5.7 (2026-10-08)

- **Added**
  - A public metrics endpoint exposes uptime, version, deploy counts per status and the running count in Prometheus format.
  - Deploy commands that are safe to re-run (`docker compose up -d`, `kubectl apply`) are retried a bounded number of times to absorb transient SSH or network blips; attempts and delay are configurable, and one attempt disables retry.
  - Compose declares container memory and CPU limits and reservations, overridable from the environment.
  - Login accepts bcrypt and argon2id password hashes, so CI switching the hash algorithm no longer breaks CD login.
- **Fixed**
  - Login tokens are opaque random strings stored as SHA-256 digests in a session table; logout deletes the row so a token is revoked immediately, and the WebShell handshake verifies against the same table.
  - Login sets an HttpOnly, SameSite cookie and authentication reads it before the bearer header; the frontend no longer carries a token in the URL or in session storage.
  - Responses carry content-type, framing, referrer and content-security-policy headers.
  - The encryption salt is derived from the deployment secret instead of one value shared by every deployment; previously encrypted values still decrypt, including those written by the short-lived salt file.
  - A read-only container started without a configured secret fails with a bilingual message naming the fix, instead of a raw OS error.
  - Database connections are closed on every failure path.
- **Improved**
  - Every CD table is required: one missing table blocks login, every authenticated endpoint, the public webhook receiver and the WebShell handshake with a 503 that names the missing tables. The gate reports, never repairs, and is not counted as a login failure.
  - The table inventory is the single source of truth: SQLite auto-creation covers every entry, a drift between the inventory and the schema fails the first connection, and the remediation hint differs per database driver.
  - A database failure is its own 503 carrying the driver and the underlying cause, kept apart from an incomplete inventory.
  - Startup logs an error naming every missing table without blocking the health, info and metrics endpoints.
  - The container drops all capabilities, forbids privilege escalation, runs read-only with `/tmp` on a tmpfs, and binds to loopback by default.
  - The preflight check requires a secret key for every database driver and rejects the two public sample values.
  - An account with an empty systems field can now be denied by configuration; the default stays permissive.
  - The compose healthcheck asserts database reachability, so a dead database no longer reports a false green.
  - CI runs the full test suite with coverage, type checking, linting, and pins every workflow action to a commit SHA.

## v1.5.6 (2026-10-05)

- **Added**
  - A read-only view exposes the audit columns of the deploy log for Devops-Glue. Large output columns and CD-internal operational columns are excluded. MySQL replaces it in place; SQLite recreates it on startup.

## v1.5.5 (2026-10-03)

- **Fixed**
  - Docker Compose deployments map to the Docker deploy permission instead of falling through to the single-host permission.
  - With no namespace configured, the effective namespace is taken from the manifest, so kubectl verifies and restarts the right namespace.
  - The stop endpoint rejects Kubernetes deploy types, as the two deploy endpoints already do.
  - With no server selected, Kubernetes clusters are no longer treated as SSH or Compose targets.
  - First-deploy metadata is parsed from the manifest file locally instead of asking the cluster for a resource that does not exist yet.
  - Compose command mode and the Helm namespace flag quote their interpolations.
  - SSH commands time out on a silent remote instead of blocking the deploy thread forever.
  - A busy project reports the same busy error on the SSH path as on the Kubernetes path, and the fallback finish path updates only that project's running record.

## v1.5.4 (2026-09-23)

- **Improved**
  - Configuration is two-layer: the root env file holds deployment values only — database, secret key, Harbor and CI credentials, listen address, timezone, proxy hops — while application parameters ship with the image. Precedence is process environment, then the root env file, then the application file, then code defaults.
  - Joint deployment needs no change to Glue's compose file; standalone Docker and bare metal both pick up application parameters without rebuilding the image.
- **Fixed**
  - A Harbor outage during a registry sync is reported as unreachable instead of "sync complete, zero images"; per-repository errors that are not connection failures stay tolerant.
  - Unknown keys in the env file no longer abort startup.

## v1.5.3 (2026-09-14)

- **Added**
  - After approval, only the requester can claim and execute a ticket, synchronously or over a stream. The background auto-execution poller is removed, and restart recovery converges in-flight tickets by the actual deploy-record state.
  - A deploy ticket can carry an optional due time; a background daemon executes due approved tickets as the requester, with or without a matching approval rule. Invalid formats are rejected.
  - Deploy records link back to their approval ticket, with history backfilled. Rollback accepts a custom note, and the ArgoCD rollback summary is written to the note column.
- **Fixed**
  - A failed execution releases the project concurrency lock instead of wedging the ticket.
  - Cancelling keeps the original approver and sends the cancel notification.
  - Target-environment resolution no longer silently skips approval, and startup recovery no longer reopens finished deploys.
  - Approval rule matching resolves permissions in one pass, and the deploy-log badge follows the ticket status.
  - The legacy default approver role is removed; new rules start with no approver role.
- **Improved**
  - Account write and password-change endpoints are removed — accounts are managed in Glue — and the user views are deleted from the frontend.

## v1.5.2 (2026-09-13)

- **Added**
  - Image tags are validated against the OCI character set on every deploy entrypoint — Compose, SSH, every Kubernetes mode, and manual-tag rollback — and rejected with 400 before any remote command runs.
  - Login shares Glue's lockout: five failures in fifteen minutes per IP and username, checked before password verification and cleared on success.
  - A requester can no longer approve or reject their own ticket, super admins included; another approver must handle it. Execution after approval still runs as the requester.
  - The client IP comes from the TCP peer unless a trusted-proxy hop count is configured, so a forged forwarded header can no longer spoof it.
  - A preflight container aborts startup with bilingual guidance when MySQL connection settings are missing, and advises when the secret key is unset.
- **Fixed**
  - The Compose deployer rewrites remote env files without shell interpolation, and every Docker, Compose and grep argument is quoted; FluxCD invocations are quoted too.
  - Webhook events are persisted with the driver-neutral insert id, fixing inserts on SQLite.
  - Empty or blank usernames are rejected as invalid credentials without touching the lockout counter.
- **Improved**
  - The two public sample secret keys are treated as unset, and the sample env file ships no default secret.

## v1.5.1 (2026-09-10)

- **Improved**
  - The startup shared-database check verifies Glue's canonical pipeline-artifact table instead of the removed tag table; requires Glue 2.8.0 or later.
  - The init script bootstraps the CI indexes on that table.
  - Documentation states that all CI data is fetched over the CI HTTP API, never read from CI tables.
- **Fixed**
  - A partial deploy stays a distinct terminal outcome instead of being folded into failed, both in aggregate counts and in the UI badge.
  - A blank namespace still means "pass no namespace flag"; only an explicit namespace is validated and forwarded to the cluster.
  - The missing MySQL migration for registry artifact fields is added, so older installs keep working.
  - The ArgoCD rollback wait loop checks that the revision still matches the previous one.

## v1.5.0 (2026-08-31)

- **Added**
  - Rule-based deployment approval: rules per project or one global default, with an enable switch, target environments matched against server tags, approvers by name or by role, and a notification bot. A matching deployment creates a pending ticket, notifies the approvers, and runs only after approval.
  - Ticket states: pending, approved, deploying, deployed, failed, rejected and cancelled. Approval is an atomic persisted transition, execution runs in the background, and a restart re-queues in-flight tickets.
  - An approvals view with a ticket list and a rules tab.
  - One-click rollback, synchronously or as a stream: native undo for kubectl, Helm and ArgoCD; replay of the previous successful deployment from its stored parameters for FluxCD, SSH and Compose. Records without a parameter snapshot are skipped.
  - Two approval tables and a deploy-approve permission.
  
- **Improved**
  - Rollback goes through the same approval gate, on by default.

## v1.4.0 (2026-08-19)

- **Added**
  - Build a historical view to identify custom_push items and display them as read-only (hide trigger buttons, action layer guard)
  - Change build record to display completion time (updated_at)
  - Regenerate front-end build artifacts

- **Improved**
  - Harbor related features
  - Docker runtime environment
  - Fixed Python version dependencies

## v1.3.1 (2026-08-17)

- **Added**
  - An in-progress deployment can be cancelled; the low-level loops poll the signal and the record is persisted as terminated.
  - One running deployment per project. The concurrency check runs before a record is inserted, and Kubernetes deploy types are rejected from the SSH and Compose paths.
  - Deploy records persist total duration, per-server per-stage timings, and a free-text note shown as a column.
  - A custom confirm dialog replaces the native browser confirm on stop and cancel actions.
- **Fixed**
  - A Kubernetes deploy type reaching the SSH path no longer raises an attribute error.
  - An unexpected exception marks the running record failed instead of leaving it orphaned and deadlocking the concurrency lock.
  - A reused deploy id no longer inherits a previous cancel signal.
- **Improved**
  - The SSH page drops the stop button and the Kubernetes page drops the external monitoring shortcut; both keep the cancel button always visible. Stop stays for Docker and Kubernetes behind a confirm dialog.

## v1.3.0 (2026-08-13)

- **Improved**
  - CI connections use Glue's dedicated API token.
  - Documentation covers the token configuration; lint findings fixed.

## v1.2.2 (2026-08-08)

- **Added**
  - A public webhook receiver authenticated by a random token in the URL path: events are persisted and browsed paginated, optionally forwarded to a bot with template placeholders, and can be forwarded manually, disabled or deleted.
  - CI build management: triggering, history, logs, build variables and branch lists for both CI engines, with a token cache that refreshes and retries.
  - A public info endpoint returns app name, version, database driver and connection state, and uptime.
  - Two webhook tables, a webhook management view, and the build and webhook sidebar entries under the notification permission.
- **Fixed**
  - Deployment validation errors reach the frontend as streamed messages instead of being swallowed.
  - A full type-check pass; the database layer converts placeholders between drivers so business code stays driver-agnostic.
  - Missing indexes added across the server, deploy-log, alert-rule, custom-monitor and webhook tables.
  - Language switching fixed and hardcoded UI strings moved into the locale files.
  - Test deprecation warnings eliminated.
- **Improved**
  - The health endpoint returns a bare ok status for liveness probes.
  - Errors carry a translation key that the frontend maps to the localized message; the webhook errors are covered.
  - Copyright, readme links and compose networking cleaned up.

## v1.2.1 (2026-07-29)

- **Improved**
  - Deployments re-check permissions against Glue's RBAC at the service layer.
  - Deploy logs deduplicate success and failure output and show the running version comparison.
  - Several settings moved into the compose file.

## v1.2.0 (2026-07-28)

- **Added**
  - A landing page for signed-out visitors.
  - Chinese and English, with independent switches on the landing and login pages.
  - Accounts carry a systems field, and CD admits a login only when it lists CD.
  - A Vue 3 frontend replacing the jQuery one.
- **Fixed**
  - Locale operator precedence, the dev-server proxy path, the ignored dependency directory, and a removed startup call.
- **Improved**
  - CD can no longer create, modify or delete admin-role users; admin is hidden from the user list and dropped from the new-user form.

## v1.1.0 (2026-07-24)

- **Added**
  - Harbor registry integration: browse projects, images and tags, read vulnerability scan reports, and pick a tag for deployment.
  - The sync interval is configurable from the UI and applied by restarting the background thread, with no service restart.
  - An unreachable Harbor returns a clear error instead of crashing.
  - Server passwords and certificates are stored encrypted, with a script that encrypts existing plaintext.
  - Server tags: an aggregation endpoint and filtering by tag.
- **Fixed**
  - The registry config endpoint handles database errors instead of returning an error page, and the config table is created solely by the init script.

## v1.0.0 (2026-07-23)

- **Added**
  - SSH, Docker Compose and Kubernetes deployment modes.
  - Server management with password or certificate login.
  - Paginated deployment history with live log viewing.
  - DingTalk and WeCom notifications.
  - Project management.
  - SQLite, MySQL and MariaDB support.
