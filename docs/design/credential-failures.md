# Credential failure classification and fallback

The gateway classifies a provider rejection only from a structured terminal response joined
to the exact admitted request ([`provider_protocol.category`](../../gateway/provider_protocol.py)).
Message text, a bare HTTP status, an interrupted stream and any shape outside the tables below
stay `unknown`: they keep the existing uncertainty and reconciliation behaviour and never start
an attempt on another account.

## Categories and what selection does

| Category | Meaning | Exclusion on this task | Next step |
| ---- | ---- | ---- | ---- |
| `auth` | The credential was refused, or its stored material is removed, revoked or expired | That record version, no deadline | Next matching entry now |
| `model_denied` | The credential may not use the requested model | That record version for that model only, no deadline | Next matching entry now, same model |
| `quota_exhausted` | A definite spending or credit limit | That record version, no deadline | Next matching entry now |
| `rate_limited` | A definite request-rate limit | That record version until its deadline | Next matching entry now |
| `unavailable` | Rejected for throttling or a spend limit, which the response does not distinguish | That record version until its deadline | Next matching entry now |
| `budget_denied` | Concurro's own native budget hook refused the request | None | Pauses as `budget_exhausted` |
| `unknown` | Anything else, including timeouts and unsettled requests | None | Existing uncertainty handling |

A deadline is the provider's numeric `Retry-After`, in seconds, when the rejection carries one
for `rate_limited` or `unavailable`. Otherwise it is a local backoff of 60 seconds that doubles
per consecutive failure of that exact record version on the task, capped at one hour; the
exclusion records which of the two set it. A completed call on that version ends the streak.
No deadline is ever invented for `auth`, `model_denied` or `quota_exhausted`, and one record's
deadline never applies to another.

Several turns can fail on one record version. Their exclusions merge: a failure without a
deadline is never replaced by a cooldown, and cooldowns keep the latest deadline.

## Supported response shapes

"Documented" means the provider's public API reference describes the shape; fixtures are
synthetic and built only from the documented fields. "Reported" means pilot diagnostics named
the code but no sanitized payload is in this repository.

| Route | Structured shape | Category | Evidence |
| ---- | ---- | ---- | ---- |
| `openai-api-key` | `error.code` `invalid_api_key` | `auth` | Existing classifier |
| `openai-api-key` | `error.code` `insufficient_quota`, `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`, `organization_usage_limit_exceeded`; or `error.type` `insufficient_quota` | `quota_exhausted` | Documented ([error codes](https://platform.openai.com/docs/guides/error-codes)) |
| `openai-api-key` | `error.code` `rate_limit_exceeded`, `slow_down` | `rate_limited` | `slow_down` documented; `rate_limit_exceeded` existing classifier |
| `openai-api-key` | `error.code` `model_not_found` | `model_denied` | Named by the open-source Codex client; not in the API reference |
| `chatgpt-subscription` | `error.code` `token_expired` | `auth` | Reported; payload not captured here |
| `anthropic-api-key`, `claude-subscription` | `error.type` `authentication_error` | `auth` | Documented ([errors](https://docs.anthropic.com/en/api/errors)) |
| `anthropic-api-key`, `claude-subscription` | `error.type` `billing_error` | `quota_exhausted` | Documented (HTTP 402) |
| `anthropic-api-key`, `claude-subscription` | `error.type` `rate_limit_error` with `error.details.error_code` `enforced_spend_limit_reached` | `quota_exhausted` | Documented ([rate limits](https://docs.anthropic.com/en/api/rate-limits)): the tier's monthly spend cap, with no `retry-after` |
| `anthropic-api-key`, `claude-subscription` | `error.type` `rate_limit_error` otherwise | `unavailable` | Documented: request limits, ramp limits and workspace spend limits share the type. A Claude 429 `rate_limit_error` is also reported by pilot diagnostics |
| `anthropic-api-key`, `claude-subscription` | `error.type` `permission_error`, `not_found_error` | `model_denied` | Documented types. The gateway fixes the request path, so the refused or missing resource is the model |

The Anthropic rows apply to `claude-subscription` because it uses the same API host and error
envelope. No rejection has been captured on a subscription token, so subscription behaviour
beyond that envelope is unverified.

## Unsupported and unverified cases

These stay `unknown`, so they pause without fallback until a writer retries:

- **Anthropic spend limit set by the organization or workspace.** Documented as HTTP 400
  `invalid_request_error`, which is structurally identical to a malformed request.
- **Anthropic `overloaded_error`, `api_error`, `timeout_error`.** Service conditions, not
  evidence about the credential.
- **ChatGPT subscription usage limits, plan entitlement and model access.** No structured
  shape is documented or captured. The OpenAI API codes are deliberately not applied to this
  route.
- **Claude subscription usage limits distinguished from request throttling.** Both arrive as
  `rate_limit_error` and are handled as `unavailable`.

The headline case, exhausted Anthropic API credits falling through to subscriptions, is
supported through `billing_error`, the spend-cap detail code and the coarse `unavailable`
path. Confirming which of those a prepaid-credit account actually returns, and capturing the
ChatGPT limit shapes, needs a separately authorized live check.

## Task state

Exclusions live in the task checkpoint as `credential_exclusions`, keyed by sponsor, record
identity and version, plus the model for a model denial. Each holds its category, deadline and
source, its local failure count and the turns already counted, so a repeated observation,
a list edit or a restart changes nothing. They never alter the owner's enabled toggle and do
not apply to other tasks: another task may spend one attempt discovering the same dead token.

A pause records `credential_recovery`: the paused call's role, profile, model and sponsor, the
candidate found for it, or the time a cooling record is due. The registry turns that plan into
one queued receipt at a time:

| Reason | Due | Source |
| ---- | ---- | ---- |
| A candidate exists | Now | The plan made when the call paused or was last reselected |
| The sponsor's credentials or list changed | Now | A vault event for one of the plan's owners; a rewrapped key is not a change |
| A cooling record is due | Its deadline | The earliest deadline among the call's excluded entries |
| The list could not be read, or trailed its own event | 60 seconds later | One later look |

The owned tick then rereads consent and the list, scans from the top and either resumes the
paused call or stays paused. Each receipt is consumed once, together with the outcome it led
to; a tick interrupted by a transient error leaves it deliverable. `/concurro retry` forgets failures
that have no deadline; a deadline still in force, spent calls and completed results remain.
