# P4 MCP Server — Impersonation Edge Case Testing Report

## Test Environment

- **P4 Server:** `localhost:1666` (P4D 2025.2)
- **MCP Super User:** `lakshya.singh` (super)
- **Impersonation:** `MCP_IMPERSONATION_ENABLED=true`
- **Test Users:** sarah.chen (restricted: modem/, camera/isp/driver/), david.wong (restricted: bluetooth/, audio/subsystem/latency/)

---

## Flaws Found

### FLAW 1 — Empty string `as_user` bypasses impersonation (MCP-level)

**Severity: HIGH**

```
as_user="" → falls through to super user identity → full unrestricted access
```

Empty string is treated as falsy/absent by the MCP server, so it skips impersonation and runs as the super user (`lakshya.singh`). This bypasses all protections.

**Not fixable by P4 server config.** Must be fixed in MCP server code — reject empty strings with `AS_USER_REQUIRED`.

---

### FLAW 2 — Whitespace `as_user` creates/impersonates junk user (MCP-level + P4-level)

**Severity: HIGH (mitigable at P4 level)**

```
as_user=" "  → P4 trims to user "_"  → no protection rules → full access
as_user="  " → P4 trims to user "__" → no protection rules → full access
```

The MCP server passes whitespace through to P4, which creates or impersonates a user with a trimmed name. Since protections only deny specific named users, the junk user gets default `write * *` access.

**P4-level mitigation:** `p4 configure set dm.user.noautocreate=2` — P4 rejects operations by non-existent users with `"User _ doesn't exist."` This blocks the whitespace bypass.

**MCP-level fix still recommended:** Trim and validate `as_user` before creating the P4 connection.

---

### FLAW 3 — Non-existent user impersonation (P4-level, mitigable)

**Severity: MEDIUM (mitigable at P4 level)**

```
as_user="ghost_user_xyz" → P4 auto-creates user → no protection rules → full access
```

By default, P4 auto-creates users on first access when a super user impersonates them. The MCP server does not validate user existence.

**P4-level fix:** `p4 configure set dm.user.noautocreate=2` — completely blocks this. P4 returns `"User ghost_user_xyz doesn't exist."`

**Verified:** With `noautocreate=2`, both MCP and CLI correctly reject non-existent users.

---

## P4 Server-Level Fix: `dm.user.noautocreate=2`

This single P4 setting fixes Flaws 2 and 3:

```bash
p4 configure set dm.user.noautocreate=2
```

| Test | Before | After |
|------|--------|-------|
| `as_user=" "` (whitespace) | SUCCESS — full access as user `_` | **DENIED** — `User _ doesn't exist` |
| `as_user="  "` (double space) | SUCCESS — full access as user `__` | **DENIED** — `User __ doesn't exist` |
| `as_user="ghost_user_xyz"` | SUCCESS — auto-creates user, full access | **DENIED** — `User ghost_user_xyz doesn't exist` |
| `as_user=""` (empty) | SUCCESS — runs as super user | SUCCESS — **still bypasses** (MCP-level issue) |

**Flaw 1 (empty string) is NOT fixed by this setting** because the empty string never reaches P4 — the MCP server treats it as absent and falls back to the super user identity.

---

## Correctly Working (no flaws)

| Test | Result |
|------|--------|
| Omitting `as_user` entirely | **DENIED** — `AS_USER_REQUIRED` on all tools |
| Wildcard `as_user="*"` | **DENIED** — P4 rejects wildcards |
| Case sensitivity (`Sarah.Chen` vs `sarah.chen`) | **CORRECT** — protections enforced regardless of case |
| Mixed-access CL (files filtered, description visible) | **CORRECT** — matches CLI behavior |
| CL list by restricted depot_path | **CORRECT** — returns empty |
| File content/history/annotations on restricted paths | **CORRECT** — all denied |
| Wildcard file listing filtering | **CORRECT** — restricted files excluded |
| Write operations on restricted paths | **CORRECT** — denied |
| `current_user` identity | **CORRECT** — shows impersonated user, not super user |
| Self-impersonation (`as_user=<superuser>`) | **BY DESIGN** — P4 allows this |

---

## Recommendations

### Required (MCP server code changes)

1. **Reject empty string `as_user`** — Treat `""` the same as omitted: return `AS_USER_REQUIRED`. This is the only flaw not fixable by P4 server configuration.

2. **Trim and reject whitespace-only `as_user`** — Defense-in-depth even though `dm.user.noautocreate=2` mitigates this at P4 level.

### Required (P4 server configuration)

3. **Set `dm.user.noautocreate=2`** — Prevents P4 from auto-creating users on impersonation. Blocks non-existent user access and whitespace junk users. Should be documented as a prerequisite for impersonation mode.

### Optional (hardening)

4. **Optional super-user self-impersonation block** — Config flag to prevent `as_user` from being set to the same identity as `P4USER`, closing the privilege escalation vector where an AI client passes the super user name.

5. **User existence pre-check** — Before creating the P4 connection, call `p4 users -m 1 <as_user>` to validate the user exists. Adds latency but provides defense-in-depth beyond `noautocreate`.
