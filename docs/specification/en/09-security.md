# Chapter 9. Security, Authentication and Authorization

## 9.1. Security model (overview)

Platform security follows **defense-in-depth** and **zero-trust** principles: each layer has
independent controls, and internal services do not trust the network by default.

| Layer | Mechanism |
|---|---|
| Perimeter | WAF, DDoS protection, TLS 1.3 termination |
| Authentication | JWT (Bearer), OAuth with Steam |
| Authorization | RBAC + resource ownership check |
| Transport (internal) | mTLS via service mesh |
| Data | encryption at rest (AES-256), password hashing |
| Secrets | Vault / Sealed Secrets |
| Audit | immutable action log |

---

## 9.2. Authentication

### 9.2.1. Authentication flows

| Flow | Usage |
|---|---|
| Steam OpenID/OAuth | login via Steam account |
| Email + password | classic registration |
| Refresh token | session renewal without re-login |
| Service-to-service | mTLS + short-lived tokens |

### 9.2.2. JWT — structure and policy

| Parameter | Value |
|---|---|
| Signing algorithm | RS256 (asymmetric) |
| Access token TTL | 15 minutes |
| Refresh token TTL | 30 days (rotation) |
| Claims | `sub`, `role`, `plan`, `iat`, `exp`, `jti` |
| Revocation | denylist by `jti` in Redis |
| Key rotation | JWKS endpoint, kid versioning |

```json
{
  "sub": "account-uuid",
  "role": "premium",
  "plan": "premium_monthly",
  "iat": 1752400000,
  "exp": 1752400900,
  "jti": "token-uuid"
}
```

### 9.2.3. Password storage

| Aspect | Implementation |
|---|---|
| Algorithm | Argon2id (or bcrypt cost ≥ 12) |
| Salt | unique per user |
| Password policy | minimum 12 characters, breach-list checks |
| Brute-force protection | rate limit + progressive delay + CAPTCHA |

---

## 9.3. Authorization (RBAC)

### 9.3.1. Roles and permissions

| Role | Permissions |
|---|---|
| `anonymous` | public meta, landing |
| `free` | upload (limited), basic analysis |
| `premium` | AI Coach, plans, history, extended metrics |
| `pro` (B2B) | team analytics, export, higher limits |
| `admin` | user management, moderation |
| `service` | internal calls (by mTLS identity) |

### 9.3.2. Access matrix (example)

| Resource / Role | anonymous | free | premium | pro | admin |
|---|---|---|---|---|---|
| `GET /meta/*` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `POST /matches/upload` | ❌ | ✅ (limit) | ✅ | ✅ | ✅ |
| `GET /players/{id}/training-plan` | ❌ | ❌ | ✅ | ✅ | ✅ |
| Team analytics | ❌ | ❌ | ❌ | ✅ | ✅ |
| Admin panel | ❌ | ❌ | ❌ | ❌ | ✅ |

### 9.3.3. Resource ownership check

Beyond role, ownership is checked: a player can see their own private breakdowns but not others'
(unless the owner shared access). Enforced at the API Gateway and data-service level.

---

## 9.4. Threat model (STRIDE)

| Category (STRIDE) | Threat | Mitigation |
|---|---|---|
| **S**poofing | Identity forgery | JWT signature, mTLS, MFA (opt.) |
| **T**ampering | Data change in transit | TLS 1.3, mTLS, payload integrity |
| **R**epudiation | Denial of actions | Immutable audit log with `trace_id` |
| **I**nformation Disclosure | PII leakage | Encryption at rest, data minimization |
| **D**enial of Service | Overload | Rate limiting, autoscaling, WAF |
| **E**levation of Privilege | Privilege escalation | Strict RBAC, least privilege |

### 9.4.1. Domain-specific threats

| Threat | Mitigation |
|---|---|
| Uploading a malicious "`.dem`" | magic/size validation, parser sandbox |
| API scraping/abuse | rate limit, anomaly detection, API keys |
| Prompt injection into the LLM | input sanitization, guardrails, context isolation |
| Data exfiltration via RAG | limit context sources, ACL on Vector DB |

---

## 9.5. Data and ML pipeline protection

| Aspect | Measure |
|---|---|
| Parser isolation | run in sandbox/gVisor, seccomp, no network |
| Schema validation | Schema Registry, reject incompatible messages |
| Model integrity | artifact signing, checksum verification |
| Data poisoning defense | anomaly detection of input matches, quarantine |
| Secrets in pipelines | injection from Vault, no hardcoding |

---

## 9.6. Secret and key management

| Secret | Store | Rotation |
|---|---|---|
| JWT keys (RSA) | Vault | 90 days |
| DB passwords | Vault (dynamic secrets) | per policy |
| External API keys | Vault | per provider |
| TLS certificates | cert-manager / mesh CA | auto (short TTL) |
| Data encryption keys | KMS | yearly (envelope encryption) |

---

## 9.7. Compliance and privacy (GDPR)

| Requirement | Implementation |
|---|---|
| Right to erasure | erasure endpoint → cascading PII deletion |
| Right to export | user data export (JSON) |
| Data minimization | store only what is necessary |
| Consent | explicit processing consent |
| Pseudonymization | separate PII and analytical identifiers |
| Retention | retention policies (see Ch. 4.6) |

---

## 9.8. Infrastructure and SDLC security

| Area | Practice |
|---|---|
| Container images | scanning (Trivy), distroless, non-root |
| Dependencies | SCA (Dependabot/Snyk), SBOM |
| Code | SAST in CI, secret scanning |
| Cluster | Network Policies, Pod Security Standards |
| Access | least privilege RBAC in Kubernetes, kube-api audit |
| Response | incident runbooks, rotation on compromise |

### 9.8.1. Security requirements (summary)

| ID | Requirement |
|---|---|
| SEC-AUTH-01 | All external connections use TLS 1.3 |
| SEC-AUTH-02 | All internal calls use mTLS |
| SEC-AUTH-03 | Access tokens live ≤ 15 min |
| SEC-DATA-01 | PII encrypted at rest (AES-256) |
| SEC-DATA-02 | Passwords hashed with Argon2id/bcrypt |
| SEC-PIPE-01 | Parser isolated in a sandbox without network |
| SEC-COMP-01 | GDPR erasure/export support |
