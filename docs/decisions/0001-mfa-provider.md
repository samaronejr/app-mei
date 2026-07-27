# 0001 — MFA provider: `django-allauth[mfa]`, not `django-otp`

- **Status**: Accepted
- **Gate**: T-002a (verify-first) in `.omo/plans/accounting-mei-saas.md`
- **Verified against**: `django-allauth 65.18.0` on `django 5.2.16`, Python 3.13
- **Full probe transcript**: `.evidence/T-002a-verify.txt` (git-ignored; regenerate with the
  commands quoted below)

## Question

The plan pins `django-allauth[mfa]` and deliberately omits `django-otp`. That decision is only
safe if the installed `allauth.mfa` actually provides **(a)** TOTP enrolment and verification,
**(b)** recovery codes, and **(c)** a way to *require* MFA for a subset of users (firm-side
roles). If any of the three is missing, the fallback is `django-otp` and the dependency list
in T-002 changes.

## Finding

| Capability | Result | Primary citation (installed package) |
| --- | --- | --- |
| (a) TOTP enrolment | **Present** | `allauth/mfa/totp/internal/auth.py:91` `TOTP.activate(cls, user, secret)` |
| (a) TOTP verification | **Present** | `allauth/mfa/totp/internal/auth.py:75` `validate_totp_code(secret, code)`; `:100` `TOTP.validate_code` |
| (b) Recovery codes | **Present** | `allauth/mfa/recovery_codes/internal/auth.py:20` `RecoveryCodes.activate`; `:48` `generate_codes`; `:105` `validate_code` |
| (c) Per-role enforcement | **Present as primitives, not as a drop-in** | `allauth/mfa/utils.py:16` `is_mfa_enabled(user, types=None)`; `allauth/mfa/adapter.py:124` overridable via the `MFA_ADAPTER` setting (`allauth/mfa/app_settings.py:20`, resolved at `adapter.py:164`); `allauth/mfa/stages.py:17` `AuthenticateStage(LoginStage)` with the overridable `_should_handle` gate at `:28` |

Supporting detail for (a): `allauth/mfa/adapter.py:74` `build_totp_url` emits the `otpauth://`
provisioning URI and `:89` `build_totp_svg` renders the enrolment QR code; tuning lives in
`allauth/mfa/app_settings.py:50/57/64/88` (`MFA_TOTP_PERIOD`, `_DIGITS`, `_ISSUER`,
`_TOLERANCE`).

Supporting detail for (b): `MFA_RECOVERY_CODE_COUNT` defaults to 10 and
`MFA_RECOVERY_CODE_DIGITS` to 8 (`allauth/mfa/app_settings.py:32`, `:39`), and
`MFA_SUPPORTED_TYPES` defaults to `["recovery_codes", "totp"]` (`:94`) — recovery codes are on
by default, not an add-on.

## The one honest caveat, and why it does not fail the gate

allauth ships **no** ready-made "require MFA for group X" middleware. Two searches against the
installed tree establish this rather than assuming it:

```sh
grep -rn 'RequireMFA\|require_mfa' allauth/          # -> no matches
find allauth -name 'middleware*.py'                  # -> account/, usersessions/ only; neither enforces MFA
```

That is acceptable because:

1. The plan already owns this layer. T-016 says verbatim: *"Add middleware or a mixin that
   **forces** any user holding an active `Membership` into MFA enrolment before reaching any
   tenant view"*. Enforcement was always ours to write; the gate asks whether allauth gives us
   a supported way to write it, and `is_mfa_enabled()` plus the `MFA_ADAPTER` override plus the
   login-stage framework are exactly that.
2. **The fallback is strictly worse.** `django-otp` does not provide per-role enforcement
   either — it provides `otp_required` / `user.is_verified()` and leaves the policy to the
   application — and it has no first-class TOTP recovery-code flow (`otp_static` is a manual
   admin-managed static-token plugin). Switching would lose capability (b) without gaining (c).

## Decision

Proceed as planned. `django-allauth[mfa]` is pinned in T-002; `django-otp` stays absent and
T-002's `grep -q "django-otp" pyproject.toml` assertion remains in its non-inverted form.
Enforcement middleware is built in T-016 on top of `allauth.mfa.utils.is_mfa_enabled`.
