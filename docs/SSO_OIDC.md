# CD ↔ Devops-Glue Single Sign-On (OIDC / BFF) Design

> **Status**: design frozen (v1); implementation lives on branch `feature/oidc-bff` (L1).
> **Upstream**: Devops-Glue acts as the OAuth2 / OIDC Provider (see its own `docs/单点登录.md`).
> **Related**: [`ARCHITECTURE.md`](ARCHITECTURE.md) (deployment gate & CD table inventory),
> [`ADMIN_MANUAL.md`](ADMIN_MANUAL.md) (`.env` and switch/rollback steps), [`FAQ.md`](FAQ.md) (login troubleshooting).

---

## 1. Background & Goals

**Today** CD and Devops-Glue share `admin_users` / `roles` / `role_permissions`: login verifies the password locally with
bcrypt (`backend/routers/auth.py::login` → `backend/auth.py::authenticate`), identity and permissions are read straight
from the shared tables on every request (`get_current_user` → `_query_user_with_systems` + `_query_permissions`), and the
session is an opaque server-side token (`cd_sessions` stores only a SHA-256 digest).

**Goal (split of concerns)**: make Devops-Glue the **single source of identity (IdP)** for CD:

1. login moves to the OIDC Authorization Code flow (a BFF confidential client — the browser never handles Glue credentials);
2. identity (`username` / `role` / `systems` / `status`) and permissions (`perm_key` list) are **fetched over Glue's HTTP API**;
   the request path no longer reads the shared tables;
3. local password login stays as a **rollbackable second track**: switching on and off is one env var plus a restart, no image rollback.

**Non-goals**: no shared-schema change; no change to the behaviour of Glue's existing clients (Jenkins / Harbor / GitLab / Grafana);
no new database or external cache.

## 2. v1 Decisions & Non-Goals

| Item | v1 decision | Rationale |
|---|---|---|
| Flow | **BFF** (server-side confidential client + Authorization Code) | CD serves the SPA from the same origin (`main.py:195/275`); the session stays in the HttpOnly `cd_token` cookie |
| PKCE | **Not used** | Confidential client: the `code` is exchanged server-side, so PKCE adds nothing under BFF |
| `end_session` | **Not used** | Glue's `OAuthController::authorizeSubmit` (`:66-127`) only issues a `code`, it **creates no session/cookie** — there is nothing to log out of. CD logout stays "delete the `cd_sessions` row + drop the cookie" |
| `refresh_token` | **Not used** (deferred to P2) | CD holds no user-level Glue token (§5); also Glue's `issueAccessToken:152-167` does not check account status, so a refresh path must call `refreshAccount()` first |
| SPA talking to the IdP directly | **No** | It would ship `client_secret` to the browser — the confidential-client model would break |
| `systems` claim in the id_token | **P2** | The `systems` needed for admission already comes back from `GET /api/rbac/users/{u}`, so the claim only saves one call |
| `userinfo` / access_token as the long-lived session | **No** | Glue access tokens are bounded by `ACCESS_TOKEN_TTL=3600`, far too short for a 24 h CD session ⇒ the CD-side `AUTH_TOKEN_TTL_HOURS` governs session life and Glue is only used for periodic revalidation |

## 3. Roles & Data Flow

### 3.1 Login (Authorization Code + BFF callback)

```text
Browser                    CD (BFF, same origin)                     Devops-Glue
  │  GET /api/auth/oidc/start
  │───────────────────────>│ cd_deploy_gate() runs first (503 if not enabled / not deployed)
  │                        │ state = token_urlsafe(32); nonce = token_urlsafe(32)
  │                        │ Set-Cookie: cd_oidc_state=<state>.<nonce> (HttpOnly / Lax / 600s)
  │  302 → /oauth/authorize?response_type=code&client_id=cd&
  │        redirect_uri=…&scope=openid profile email&state&nonce
  │<───────────────────────│
  │  (user logs in and authorises; after P0-0 only accounts with systems='cd' pass)
  │  302 → /api/auth/oidc/callback?code=…&state=…
  │───────────────────────>│ (1) verify state (compare with cookie, single use)
  │                        │ (2) POST /oauth/token (HTTP Basic: cd + secret;
  │                        │     grant_type=authorization_code&code&redirect_uri)
  │                        │──────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────│ {access_token, id_token, expires_in, scope}
  │                        │ (3) verify id_token: JWKS(RS256) + iss + aud=cd + exp + nonce → username
  │                        │ (4) GET /api/rbac/users/{username} (Bearer dg_ service token)
  │                        │──────────────────────────────────────────>│ {username, role, systems, status}
  │                        │ (5) admission: status≠0 and systems contains cd (reuses _has_system / ALLOW_EMPTY_SYSTEMS)
  │                        │ (6) GET /api/rbac/roles/{role}/permissions (Bearer dg_)
  │                        │──────────────────────────────────────────>│ {permissions:[…]}
  │                        │ (7) _create_session() → Set-Cookie: cd_token → 302 /
  │  302 → /                │
  │<───────────────────────│
  │  any failure → 302 /login?error=<error_key>
```

> The returned `access_token` is **discarded immediately**: every subsequent Glue call from CD uses the `dg_` service token
> (`CI_API_TOKEN`, non-expiring).

### 3.2 Authenticated requests (lazy periodic revalidation)

CD does not call Glue on every request. `verify_token` / `get_current_user` still validate the `cd_sessions` row and session
TTL; only when **more than `OIDC_REVALIDATE_SECONDS` (default 60) have passed since the last check** does CD revalidate
against Glue using an in-process identity cache (key = `username`, holding `role` / `systems` / `status` / `permissions` / `checked_at`):

```text
cache hit and younger than 60s → allow (zero Glue calls)
older than 60s → GET /api/rbac/users/{username}
                 ├ status=0 or systems without cd → delete session row + 401 (frontend useAuth.handle401 → logout)
                 └ role changed → fetch that role's permissions again and refresh the cache
Glue unreachable → within the soft-fail window (OIDC_STALE_GRACE_SECONDS, default 600) serve last-known-good;
                   past the window → fail-closed 401/503 (policy configurable, see §6)
```

Clock fields are in-process only (never persisted): CD runs a single uvicorn process (`Dockerfile:26`, no `--workers`);
with several replicas each one simply revalidates on its own, which is semantically harmless.

## 4. Key Mechanics (why the design looks like this)

### 4.1 Identity comes from `GET /api/rbac/users/{u}`, not from id_token claims

Glue's id_token claims are `iss` / `sub` (**= username**) / `aud` (= `cd`) / `iat` / `exp` / `nonce` (echoed when given) /
`name` / `preferred_username` (= username) / `email` / `email_verified` / `groups` (**= `[role]`**, `[]` when the role is empty)
— see `OAuthController::buildIdTokenClaims:316-328` and `OidcService::signIdToken:206-232`.

There is **no** `role` claim and **no** `status`. Therefore:

- `sub` / `preferred_username` only determine the **username**;
- `role` / `systems` / `status` always come from the rbac endpoint (one call returns all three); the id_token `groups[0]` is
  **not used in v1**, so there is never a second source of truth.

### 4.2 Permissions must come from a new Glue endpoint (not optional)

`GET /api/rbac/roles` returns only `name` / `description` (`AdminUserRepository::listRolesForApi:119-124`) and **no
role→permissions API exists anywhere** — `TokenService::loadPermissions` is internal to the web login session. So
`GET /api/rbac/roles/{name}/permissions` is a **P0 requirement**, not an optimisation: without it CD cannot obtain `perm_key`
and `require_perm` is dead across the board.

### 4.3 Why "disable kicks the session out" and "privilege changes take effect immediately" hold

- **On the Glue side** `OAuthService::validateAccessToken` calls `refreshAccount` every time, re-reading
  `admin_users.status/role` (`OAuthService.php:190-193`, `:209-235`) — the access token is not a permissions snapshot;
- **On the CD side** the lazy revalidation re-checks `status` and `role` the same way (§3.2), with a window of at most
  `OIDC_REVALIDATE_SECONDS` (default 60 s).
  ⇒ Disabling or demoting a user never waits for the 24 h CD session to expire and never needs a restart.

### 4.4 Why "no more shared-table reads" is safe

In OIDC mode every admission decision (login and each request) comes from Glue's HTTP contract
(token + `/.well-known/*` + `/api/rbac/*`); the only shared table CD still touches is the `integrated_services` contract row
inside the **deployment gate** (that is the "is CD enabled" switch, not identity). Everything else is CD-owned `cd_*` tables.
⇒ The shared tables are demoted from "identity source" to "an implementation detail of Glue"; CD has no coupling point left.

## 5. Zero Schema Change (the single most important engineering constraint)

The reasoning chain (every step backed by code):

1. CD **does not need to store user-level Glue tokens**: login only verifies the `id_token` to learn who the user is, and every
   later Glue call uses the `dg_` service token ⇒ there is no `access_token` / `refresh_token` to persist, so **no new column**;
2. a persisted "this session is OIDC" marker is unnecessary too: `AUTH_MODE` is a **global switch**, not a mixed mode (§6), so in
   OIDC mode every session comes from OIDC;
3. "last revalidation time" fits in an **in-process cache** (single-process uvicorn; after a restart one extra lookup is harmless);
4. ⇒ **`CD_REQUIRED_TABLES` is untouched**.

**Why adding a table had to be avoided**: the gate means "missing any table from the inventory = not deployed", so a new table would
immediately 503 **every existing deployment** (and in `manual` mode force operators to re-run `database/init_mysql.sql`).
⇒ This change introduces no DDL at all: upgrading is "new image + flip the switch".

## 6. Dual-Track Switch & Rollback

### 6.1 Switches

```ini
# config/app.env (application settings, shipped with the image; restart after editing inside the container)
AUTH_MODE=local                    # local | oidc (default local = byte-for-byte v1.6.0 behaviour)
OIDC_ISSUER=                       # public Glue base URL; must equal the issuer in its discovery document
OIDC_REVALIDATE_SECONDS=60         # lazy revalidation window
OIDC_STALE_GRACE_SECONDS=600       # soft-fail window while Glue is unreachable (0 = fail-closed immediately)
OIDC_ID_TOKEN_MAX_SKEW=60          # iat/exp clock-skew tolerance
```

```ini
# repository-root .env (deployment layer, same class as CI_API_TOKEN; never baked into the image)
CD_OIDC_SECRET=                    # must equal oauth_clients['cd']['secret'] on the Glue side
CD_OIDC_REDIRECT_URI=              # must match the value registered in Glue character for character
```

### 6.2 Behaviour matrix

| Behaviour | `AUTH_MODE=local` | `AUTH_MODE=oidc` |
|---|---|---|
| `POST /api/login` (password) | unchanged | **403** `errors.local_login_disabled` (code kept; rollback restores it) |
| `/api/auth/oidc/start` / `callback` | 404 (route availability decided by the mode) | enabled |
| `verify_token` / `get_current_user` / WebShell `_ws_verify` | as today: reads `admin_users` + `roles/role_permissions` | validates `cd_sessions` only + lazy Glue revalidation |
| shared `cache`-table login-failure lock (`login_guard`) | unchanged | falls away naturally (`/api/login` no longer runs) |
| `/api/me` response shape | `{username, role, systems, permissions}` | **identical** ⇒ frontend `useAuth` / route guards need zero changes |
| Cost of rollback | — | flip back to `local` + restart (≤30 s, no image rollback) |

### 6.3 Fail-closed: an incomplete configuration refuses to start

If `AUTH_MODE=oidc` and any of `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` / `OIDC_ISSUER` is empty, the container **aborts on
startup** with a bilingual (zh/en) message — the same pattern as the existing `SECRET_KEY` check in `docker-compose.yml:45-62`.
The rationale matches Glue's client filter: a half-configured OIDC setup means running with a public default secret.

### 6.4 Switch / rollback procedure (operations)

| Action | Steps |
|---|---|
| Switch to OIDC | (1) fill `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` in the root `.env`; (2) set `AUTH_MODE=oidc` + `OIDC_ISSUER` in `config/app.env`; (3) `docker compose up -d --build`; (4) recommended: clear in-flight local sessions with `DELETE FROM cd_sessions;` |
| Roll back to local | `AUTH_MODE=local` + restart. Existing OIDC sessions stay valid until their TTL (clear `cd_sessions` to force a fresh login) |
| Validate without switching | keep `local` and open `/api/auth/oidc/start` directly: the whole chain can be exercised, and `/api/auth/oidc/*` does not interfere with `/api/login` |

> Clearing sessions is the **only** operational action required: one SQL statement, no schema change (the implementation does no DDL,
> so nothing has to be imported).

## 7. Implementation Checklist (file level)

> Line anchors are relative to the **v1.6.0 (`45fc553`)** baseline. All L1 code is **behaviour-neutral** under the default
> `AUTH_MODE=local`, so it can be merged first.

### 7.1 New files

| File | Responsibility | Notes |
|---|---|---|
| `backend/oidc.py` | OIDC client: discovery / JWKS cache, `authorize` URL building, `token` exchange, id_token verification | RS256 verification hand-written with **`cryptography`, already a dependency** (no PyJWT); an unknown `kid` refreshes the JWKS exactly once; iss/aud/exp/iat-skew/nonce all verified |
| `backend/services/glue_identity.py` | Glue client for identity + permissions, in-process TTL cache, lazy revalidation | Mirrors the `requests.Session` + Retry shape of `ci_client.py`; **never appends `?format=json`**; judges errors by HTTP status only; fail-closed after the soft-fail window |
| `backend/routers/oidc.py` | `GET /api/auth/oidc/start`, `GET /api/auth/oidc/callback` | Deployment gate first; state/nonce single-use with cookie cleanup; every failure → `302 /login?error=<error_key>` |
| `backend/tests/test_oidc_flow.py` | Authorization-code flow and id_token regression | state replay/missing, **the token request must carry `redirect_uri` (mock assertion)**, one failure branch each for iss/aud/exp/nonce/signature, admission 403, cookie flags |
| `backend/tests/test_glue_identity.py` | Revalidation semantics regression | 60 s window hit/miss, immediate demotion, disable kicks the session out (session row deleted), soft-fail window, fail-closed, recovery |

### 7.2 Modified files (with anchors)

| File | Location | Change |
|---|---|---|
| `backend/config.py` | after `:48` (`allow_empty_systems`) | add `auth_mode` / `oidc_issuer` / `oidc_client_id="cd"` / `oidc_client_secret` / `oidc_redirect_uri` / `oidc_scopes` / `oidc_revalidate_seconds` / `oidc_stale_grace_seconds` / `oidc_id_token_max_skew`; whitelist-validate `AUTH_MODE`; add `oidc_ready()` |
| `backend/auth.py` | `:155-187` `verify_token` | add the `AUTH_MODE=oidc` branch: validate the `cd_sessions` row + lazy revalidation; the local branch stays untouched |
| `backend/auth.py` | `:190-228` `get_current_user` | same branch; **response shape unchanged** (`{username, role, systems, permissions}`), permissions come from Glue (cached per role) |
| `backend/auth.py` | `:78-95` | `_check_cd_access` / `_check_disabled` stay for local mode; the OIDC side reuses the same `_has_system` / `CD_SYSTEM` semantics (same `ALLOW_EMPTY_SYSTEMS` behaviour) |
| `backend/auth.py` | `:331-369` `authenticate` | **unchanged** (local only) |
| `backend/routers/auth.py` | between `:37-43` | insert the mode guard after `cd_deploy_gate` and **before** the login-failure lock lookup: `oidc` → 403 `errors.local_login_disabled` (so the shared `cache` table is never touched) |
| `backend/routers/terminal.py` | `:58-95` `_ws_verify` | **must be branched too** (WebShell is a separate auth path; otherwise in OIDC mode the WS handshake would read `admin_users`) |
| `main.py` | around `:164` | `app.include_router(oidc.router)` |
| `main.py` | `:44-99` `lifespan` | log `AUTH_MODE` at startup; ERROR when OIDC mode is misconfigured (following the existing "gate warning does not block startup" style) |
| `main.py` | `:206-235` `/api/info` | add `"auth_mode": settings.auth_mode` (**mode only, never any secret**) so the login page can pick its rendering |
| `config/app.env` | after `:38` (`SUPER_ADMIN_ROLE`) | add the five non-secret keys of §6.1 with comments |
| `.env.example` | after `:65` (`CI_API_TOKEN`) | add `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` plus the "empty = disabled" note |
| `docker-compose.yml` | `:45-62` startup script, `:79-90` env | (1) `AUTH_MODE=oidc` with missing OIDC keys → **abort startup** (bilingual, same shape as the `SECRET_KEY` check); (2) pass through `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` |
| `frontend/src/views/LoginView.vue` | `:1-14` template, `:30-57` | in OIDC mode render "Sign in with Devops-Glue" (`<a href="/api/auth/oidc/start">`) and collapse the local form; on mount read `route.query.error` and render it via the **existing `:47-52` `te(key)/t(key)` strategy**, then clear the query |
| `frontend/src/locales/zh.json` / `en.json` | — | **only 4 new keys**: `login.ssoBtn` / `login.ssoHint` / `login.localDisabled` / `errors.local_login_disabled`. `errors.no_cd_access` (`zh.json:934`) and `errors.user_disabled` (`:938`) **already exist and are reused** |
| `backend/tests/test_auth_login.py` | after `:95` | add one case: "in OIDC mode `/api/login` → 403 and neither the lock nor the counter is consulted" |
| `docs/` | see §13 | this document + `ADMIN_MANUAL(_ZH)` / `ARCHITECTURE(_ZH)` / `FAQ(_ZH)` / `USER_MANUAL(_ZH)` / `CHANGELOG(_ZH)` |

### 7.3 Deliberately untouched (to keep the regression surface small)

| Part | Reason |
|---|---|
| `backend/auth.py::_create_session` / `logout` | session mechanics and logout semantics (delete row + drop cookie) are correct in both modes; **Glue `end_session` is not called** (§2) |
| `frontend/src/composables/useAuth.js`, route guards, WebSocket client | the `/api/me` shape and the `cd_token` cookie are unchanged |
| `backend/login_guard.py`, shared `cache` table | still used by local mode and naturally bypassed in OIDC mode (removable in L2) |
| `backend/services/approval_service.py::load_user_context` | scheduled execution / crash recovery rebuilds identity in the background with no HTTP context — see the L2 list in §14 |

## 8. Frozen Interface Contract (CD ↔ Glue)

> The CD implementation follows this table exactly; "never append `?format=json`" and "judge errors by status code only" are **hard constraints**.

### 8.1 `POST /oauth/token`

| Item | Value |
|---|---|
| Auth | **HTTP Basic** (`client_secret_basic`; Glue `OAuthController:144-153` supports it) |
| Body | `grant_type=authorization_code`, `code`, **`redirect_uri` (must be retransmitted character for character: `consumeCode($code,$clientId,$redirectUri)` compares it exactly)**, `scope=openid profile email` |
| Success 200 | `{"access_token":…,"token_type":"Bearer","expires_in":3600,"id_token":…,"scope":…}` (`Cache-Control: no-store`) |
| ⚠️ | **Without `openid` in `scope` no `id_token` is issued at all** (`hasOpenidScope:302-309`) — no error, just no identity |
| ⚠️ | `expires_in` is a **literal 3600** (`:179`) which may drift from the real access-token TTL ⇒ **CD v1 does not rely on it** |
| Failure | `{"error":"invalid_client"}` 401 (wrong secret) / `{"error":"invalid_grant"}` 400 (code invalid or already used, **redirect_uri mismatch**) / `unsupported_grant_type` 400 |

### 8.2 id_token claims (verified against the source)

| Claim | Value | CD usage |
|---|---|---|
| `iss` | the discovery issuer | must be verified |
| `sub` | **= username** | username source |
| `aud` | **= `cd`** | must be verified |
| `iat` / `exp` | `exp = now + OIDC_ID_TOKEN_TTL` (default 3600) | verified (tolerating `OIDC_ID_TOKEN_MAX_SKEW`) |
| `nonce` | echoed from the authorization request | **must be verified** |
| `preferred_username` / `name` | = username | username source (`preferred_username` first) |
| `email` / `email_verified` | optional | unused by CD |
| `groups` | `= [role]` | unused in v1 (role comes from the rbac endpoint) |

### 8.3 `GET /api/rbac/users/{username}`

```text
Request:  GET /api/rbac/users/{username}      Authorization: Bearer dg_…
          ⚠️ never append ?format=json        ⚠️ strip().lower() the username first (Glue normalises with strtolower)
Success 200 (bare object, no wrapper):
          {"username":"zhangsan","role":"dev","systems":"cd","status":1}
          · systems: comma-separated string (may be ""); status: 1=enabled / 0=disabled (int)
Failure:  404 {"code":404,"message":"<translated text>"}; other errors use the same shape
```

### 8.4 `GET /api/rbac/roles/{role}/permissions` (**new on the Glue side, P0**)

```text
Request:  GET /api/rbac/roles/dev/permissions  Authorization: Bearer dg_
Success 200 (bare object): {"permissions":["cd.deploy","cd.deploy.approve","cd.registry.read"]}
Unknown role → 200 {"permissions":[]} (deny-by-default, no error logging)
Internal error → 500 {"code":500,"message":"…"}
```

Suggested implementation: reuse `TokenService::loadPermissions($role)`; **no new scope required**
(`ApiScopeResolver:45-47` matches the whole `/api/rbac/*` prefix to `rbac.user.write`, so sub-paths are covered automatically).

### 8.5 `/.well-known/openid-configuration` and `/.well-known/jwks.json`

| Field | CD usage |
|---|---|
| `issuer` | compared with the configured `OIDC_ISSUER` (**rejected on mismatch**, never silently trusted) |
| `authorization_endpoint` / `token_endpoint` | used to build requests (**discovery is the only source**; no hard-coded paths) |
| `jwks_uri` | key retrieval; supports `kid` rotation (unknown `kid` triggers one refresh) |
| `id_token_signing_alg_values_supported` | must contain `RS256` |
| JWKS `keys[]` | `kty=RSA` / `use=sig` / `alg=RS256` / `kid` / `n` / `e` (base64url) |

### 8.6 CD's outbound call allow-list (must be permitted by Glue)

| Endpoint | Method | Credential | Scope |
|---|---|---|---|
| `/oauth/authorize` | GET | — | public |
| `/oauth/token` | POST | Basic(`cd`, secret) | — |
| `/.well-known/openid-configuration`, `/.well-known/jwks.json` | GET | — | public |
| `/api/rbac/users/{username}` | GET | `dg_` service token | `rbac.user.write` |
| `/api/rbac/roles/{role}/permissions` | GET | `dg_` service token | `rbac.user.write` |

### 8.7 Seven pitfalls (shared by both sides)

| # | Pitfall | Symptom |
|---|---|---|
| 1 | appending `?format=json` | the response is wrapped as `{"data":{…}}` and parsing yields nothing |
| 2 | branching on the `message` text | logic silently breaks whenever Glue rewords/translates (`message` is translated text, not a stable key) |
| 3 | forgetting `openid` in `scope` | **no `id_token` in the response** (and no error) |
| 4 | omitting or mismatching `redirect_uri` | `invalid_grant` (looks like a code problem) |
| 5 | deriving `iss` / `jwks_uri` from an internal URL | verification fails (easily mistaken for a key problem) |
| 6 | not lower-casing the username | the rbac endpoint returns 404 |
| 7 | treating `expires_in` as the real access-token TTL | drifts from the actual `ACCESS_TOKEN_TTL` and misleads session-lifetime reasoning |

## 9. Glue-side Prerequisites

| # | Item | State | Impact |
|---|---|---|---|
| **P0-0** | Decide `systemType` per OAuth client (add `'system' => env('CD_OIDC_SYSTEM','cd')` to `oauth_clients['cd']`; `OAuthController:97` then uses `clientSystem($clientId) ?: AppConfig::SYSTEM_CI`) | ⛔ **blocking** | Without it **every** CD account fails to log in. And this is the **default path, not an edge case**: CD has no account-creation entry point at all (`routers/users.py` is read-only, `ci_client.rbac_users/rbac_roles` are read-only, none of the 21 frontend views manages users, and `ADMIN_MANUAL_ZH.md:464/476` states plainly that CD does not manage accounts), while both Glue creation paths produce `systems='cd'` (`AdminController::userCreate` defaults to `SYSTEM_CD`, `RbacController::userCreate:99` hard-codes it). Clients that do not register a `system` key fall back to CI, so Jenkins/Harbor/GitLab/Grafana keep their exact behaviour |
| **P0-1** | Register the `cd` client (`secret` + `redirect_uri` matching CD's `CD_OIDC_REDIRECT_URI` **character for character**) | ⛔ blocking | otherwise `invalid_client` / `invalid_grant` |
| **P0-2** | Store and verify the `cd` client secret the same way as the others (sha256) | ⛔ blocking | otherwise the secret check fails |
| **P0-4** | Add `GET /api/rbac/roles/{role}/permissions` (§8.4) | ⛔ blocking | otherwise CD cannot obtain `perm_key` and every `require_perm` is dead |
| **P0-5** | **Set `OIDC_ISSUER=<public URL>` explicitly** (in the **Devops-Glue repository**, `config/app.env:164` is currently empty, i.e. derived at runtime from the request host) | ⛔ blocking (new finding) | The derived value may be an internal address ⇒ (1) CD's `iss` check fails; (2) discovery's `jwks_uri` points inside the network ⇒ keys cannot be fetched (presents as "signature verification failed", very easy to misdiagnose as a key problem) |

**P2 (v1 does not depend on these)**:

- **`systems` claim in the id_token**: all **three** places must change together or the claim is **silently dropped** (no error) —
  `OAuthController::buildIdTokenClaims:316-328` (assembly), the **claim allow-list** in `OidcService::signIdToken:219-229` (the critical one),
  and `OidcService::CLAIMS_SUPPORTED:30-33` (discovery advertisement).
- **`refresh_token`**: `issueAccessToken:152-167` does not check account status ⇒ a refresh path must call `refreshAccount()` first
  (`status=0` → `invalid_grant`); the `expires_in => 3600` literal should reference the `ACCESS_TOKEN_TTL` constant.
- **Inventory/repair of existing `'cd'` accounts**: `AdminUserRepository::updateUser($username, ?$passwordHash, ?$role, ?$email)` has
  **no `systems` parameter**, so already-created `'cd'`-only accounts have no API repair path (SQL or re-create only).

## 10. Security Invariants

| Invariant | How it is guaranteed |
|---|---|
| The browser never touches Glue credentials | BFF: `client_secret` stays server-side; the session is CD's own HttpOnly cookie (`cd_token`) |
| An authorization code is redeemable once | Glue's `consumeCode` invalidates on use; CD consumes `state` once |
| CSRF / replay | `state` + `nonce` double check (nonce echo comparison) |
| Minimal token exposure | the `id_token` is discarded right after verification; the `access_token` is discarded and **never persisted or logged** |
| No privilege escalation at login | three admission criteria: `systems` contains `cd` (reusing `ALLOW_EMPTY_SYSTEMS` semantics) + `status≠0` + deny-by-default role permissions |
| Disable/demote latency | ≤ `OIDC_REVALIDATE_SECONDS` (§4.3), independent of the session TTL |
| Upstream outage | fail-closed after the soft-fail window; **startup aborts** on incomplete configuration (§6.3) |
| Deployment state cannot be bypassed | `/api/auth/oidc/*` runs `cd_deploy_gate` exactly like `/api/login` |
| Information surface | `/api/info` exposes `auth_mode` only; issuer/secret/redirect_uri are never exposed |

## 11. Test Matrix

| # | Group | Cases | File |
|---|---|---|---|
| 1 | Switch / rollback | `local`: `/api/login` works, `/api/auth/oidc/*` → 404; `oidc`: `/api/login` → 403 and **neither the lock nor the counter is consulted** | `test_auth_login.py` |
| 2 | Fail-closed config | `oidc` with any of secret/redirect_uri/issuer missing → startup aborts (compose layer) and `oidc_ready()` is False | `test_oidc_flow.py` + compose-script assertion |
| 3 | Authorization-code flow | `state` mismatch/replay/missing `code` → 400; **the token request body must contain `redirect_uri` (mock assertion)**; wrong secret → 502; `invalid_grant` → 400 | `test_oidc_flow.py` |
| 4 | id_token verification | one rejection case each for signature/`iss`/`aud`/`exp`/`nonce`; unknown `kid` → passes after one JWKS refresh | `test_oidc_flow.py` |
| 5 | Admission | `systems` without `cd` → 403 `errors.no_cd_access`; `status=0` → 403 `errors.user_disabled`; success → 302 `/` with a `cd_token` cookie (HttpOnly/Lax, Secure follows `COOKIE_SECURE`) | `test_oidc_flow.py` |
| 6 | Cache / demotion | cache hit within 60 s (zero Glue calls); after a `role` change `/api/me` reflects the new role once the window elapses; `status=0` → next request 401 **with the session row deleted** | `test_glue_identity.py` + `test_session_auth.py` |
| 7 | Glue unreachable | inside the soft-fail window last-known-good is served; past the window fail-closed; normal revalidation resumes after recovery | `test_glue_identity.py` |
| 8 | WS | in OIDC mode the WebShell handshake goes through `_lookup_session` + Glue revalidation; a disabled account is refused | appended to `test_cd_deploy_gate.py` |
| 9 | Regression | the whole existing suite is unchanged in `local` mode (including the 215-case baseline) — proof that the two tracks do not harm each other | all existing tests |
| 10 | Joint acceptance | Glue-side cases + CD-side A/B/C/D (§12) | manual |

## 12. Acceptance Cases (A–E)

| # | Scenario | Steps | Expected |
|---|---|---|---|
| **A** | Local mode unchanged | keep `AUTH_MODE=local`, restart | password login, `/api/me`, deploys, audit and WebShell behave exactly as in v1.6.0; `/api/auth/oidc/start` → 404; the full suite (215-case baseline) is green |
| **B** | End-to-end SSO | `oidc` with `OIDC_ISSUER` / `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` set, open `/` in a browser | 302 to Glue → login/authorise → back to `/api/auth/oidc/callback` → 302 `/` and the dashboard loads; `docker logs` contain **no** secret / id_token / access_token |
| **C** | No access & disable | (1) log in with `systems='ci,monitor'`; (2) log in with `status=0`; (3) after a successful login disable the account in Glue, wait > 60 s, then use CD | (1) → `/login?error=errors.no_cd_access`; (2) → `errors.user_disabled`; (3) next request → 401, back to the login page, and the `cd_sessions` row is gone |
| **D** | Demotion takes effect | after login change the role in Glue / remove `cd.deploy.approve`, wait > 60 s | `/api/me` shows the new role and permissions; the corresponding buttons disappear and `require_perm` rejects the call (403) |
| **E** | Rollback | set `AUTH_MODE=local` again + restart | password login works immediately; CD tables and data are intact (**zero DDL**, no script to import); `static/` assets need no rebuild |

## 13. Rollout Phases & Operations

| Phase | Content | Risk |
|---|---|---|
| **L1a** (this change) | config keys + `oidc.py` + `glue_identity.py` + `routers/oidc.py` + tests; `AUTH_MODE=local` by default | **zero behaviour change**, safe to merge into `release/1.6.0` |
| **L1b** | staging validation: fill the three root `.env` values → switch `app.env` to `oidc` → run A–E, watch the 60 s revalidation and the soft-fail window | staging only, switch back any time |
| **L1c** | production cutover: `docker compose up -d --build` + clear `cd_sessions` + watch for 30 minutes | rollback switch available (§6.4), no schema change |
| **L2** | cleanup and enhancements (§14) | separate version |

**Observability**: the `AUTH_MODE` startup log line; `auth_mode` in `GET /api/info`; revalidation-failure / soft-fail log lines;
401/403 counts (the distribution of the login page's `error` query parameter).

**Documents to update with this change**: this document (new), `INDEX.md` (register it), `ARCHITECTURE(_ZH).md` (new "authentication & identity source" section),
`ADMIN_MANUAL(_ZH).md` (new env keys, switch/rollback steps, fail-closed note), `FAQ(_ZH).md` (SSO login failures and `?error=` troubleshooting),
`USER_MANUAL(_ZH).md` (how to sign in), `CHANGELOG(_ZH).md` (with the version entry), `README(_ZH-CN).md` doc list (optional).

## 14. L2 Backlog (out of v1 scope)

| Item | Notes |
|---|---|
| Background identity rebuild | `backend/services/approval_service.py::load_user_context` (scheduled execution / crash recovery, no HTTP context) must switch to the `glue_identity` service token + role-permission cache in OIDC mode, otherwise that path still reads the shared tables |
| `systems` claim in the id_token | Glue-side P2 (§9), saves one rbac call |
| `refresh_token` | needed only if the access token must represent the user long-term (e.g. CD calling user-level Glue APIs) |
| Remove the local branch | once every environment has switched and been stable for one version, drop the shared-table read code and the password cases in `test_auth_login.py` |
| `login_guard` / shared `cache` login lock | naturally bypassed in OIDC mode; can be removed together with the local branch |
| Self-service account management | CD does not manage accounts today (`ADMIN_MANUAL_ZH.md:464/476`) and will keep delegating that to Glue — no reverse proxy |

## 15. Environment Variables

| Variable | Default | Location | Notes |
|---|---|---|---|
| `AUTH_MODE` | `local` | `config/app.env` | `local` \| `oidc`; the single master switch |
| `OIDC_ISSUER` | empty | `config/app.env` | **public** Glue URL; must equal the discovery `issuer` |
| `OIDC_REVALIDATE_SECONDS` | `60` | `config/app.env` | lazy revalidation window (upper bound on demotion/disable latency) |
| `OIDC_STALE_GRACE_SECONDS` | `600` | `config/app.env` | soft-fail window while Glue is unreachable; `0` = fail-closed immediately |
| `OIDC_ID_TOKEN_MAX_SKEW` | `60` | `config/app.env` | `iat` / `exp` clock-skew tolerance (seconds) |
| `CD_OIDC_SECRET` | empty | root `.env` (deployment layer) | `oauth_clients['cd']['secret']`; required in `oidc` mode |
| `CD_OIDC_REDIRECT_URI` | empty | root `.env` (deployment layer) | must match the Glue registration **character for character**; required in `oidc` mode |
| `CD_OIDC_CLIENT_ID` | `cd` | code constant | not exposed as env (to prevent pointing it at another client) |

> In `oidc` mode, a missing `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` / `OIDC_ISSUER` aborts container startup (§6.3).

