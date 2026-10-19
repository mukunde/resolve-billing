# ADR-009 — Trust boundaries: who may call, and whose identity a decision carries

**Status:** accepted · 2026-10-08 · covers D7 and D8; D10 (what reaches the
model) is recorded here when it is settled

## Context

ADR-004 made a human the one who authorises money, and the review page made
the decision a POST from a form a person submitted. A code review found the
weak link. No route asked who was calling:

- anyone who could reach the API and knew an action id could approve it, then
  execute it;
- the approver's identity was whatever name the caller typed, in a form field
  or a JSON body;
- the review page admitted the field was not authentication. The JSON routes
  bypassed the page entirely.

Replayed against the previous code, 24 of 25 tests of this record fail. The
one that passes is `/health`, which is meant to stay open.

An approval whose author cannot be proven is not an approval. It is the
property ADR-004 exists for.

## Decision

**Every route that acts takes an authenticated caller, from one place.** A
FastAPI dependency (`app/api/auth.py`) produces an `ActorContext`: an
identity and a kind of caller. Routes read identity from it, and only from
it. A name in a form or a JSON body is no longer read.

**Two kinds of caller, each doing only its part:**

| route | orchestrator | approver |
|---|---|---|
| start an investigation | yes | 403 |
| approve or reject (page or JSON) | 403 | yes, recorded as itself |
| execute an approved action | yes | 403 |
| read an action or a run | yes | yes |
| `/health` | open | open |

The orchestrator can neither approve nor reject. A person cannot execute or
start an investigation. Approval and execution stay two steps (ADR-004), and
now two different callers.

**How each authenticates, in this demonstrator:**
- **The orchestrator** presents a service key, `Authorization: Bearer
  <key>`, read from `RESOLVE_SERVICE_KEY`. n8n reads the same variable from
  its own environment, so the key never appears in the exported workflow,
  which is public.
- **An approver** signs in with HTTP Basic against `RESOLVE_APPROVERS`. A
  browser shows its own prompt, and the review page states who is deciding.
  Secrets are compared in constant time, including for an unknown name.

**Closed by default.** A route whose kind of caller is not configured answers
503. It never falls open because a variable is missing.

## Consequences

**The seam is the point, not the mechanism.** HTTP Basic against a list in an
environment variable is a demonstrator's sign-in. In production:
- the approver authenticates with the company's identity provider (OIDC),
  through its SSO;
- `ActorContext` gains the roles and scope that come with that identity;
- the orchestrator authenticates as a workload: a signed token, or the
  identity of a durable workflow's workers (ADR-010), not a shared key.

None of these changes touches a route. They replace the one dependency.

**What production adds, described here and not built:**
- **roles that carry limits.** An approver is authorised up to an amount;
  above it, a second approver is required;
- **separation of duties.** Whoever initiated or prepared a correction
  cannot approve it;
- **an audit of authentication itself**: failed sign-ins, and who used the
  service key from where.

**Known limits that remain:**
- **The service key is shared and static.** Rotating it means restarting
  both the API and n8n with the new value.
- **HTTP Basic sends the secret with every request.** It is acceptable only
  over TLS, which a production deployment terminates in front of the API.
- **The run page is readable by either kind of caller**, since the alert
  mail links to it. It shows no customer message, only the trace and its
  findings, the same as before.

## The resume URL (D8), 2026-10-09

**The defect.** n8n's resume URL travels inside the review link, so it
reaches a browser. The review page accepted it as a query parameter,
stored it, and the server called it once a person had decided -- whatever
host it named. Anyone who could put a link in front of a reviewer could
make this service request an address of their choosing: a cloud metadata
endpoint, an internal service, a `file://` path. Ten such URLs were stored
and called by the previous code.

**The fix, minimal on purpose**, since ADR-010 removes this callback (a
durable workflow takes the decision as a signal):
- **Only one shape is accepted:** the orchestrator's own waiting webhook,
  on the origin configured in `RESOLVE_ORCHESTRATOR_URL`. That means the
  same scheme, host and port, the path `/webhook-waiting/<execution id>`,
  and nothing in the query but n8n's signature.
- **The address is rebuilt server-side** from those parts, never used as
  received. It is checked again at call time, so a row stored before the
  check existed cannot be rung.
- **Anything else is ignored.** The person's decision still counts;
  nobody is called back. Unset means no callback at all (closed by
  default).

**What it does not do.** The callback still carries no authority of its
own: it can only resume a run whose action a person already approved. A
cleaner design would register the execution when n8n, authenticated,
creates the case, so that no part of the address ever transits a browser.
That is the shape ADR-010 adopts; it is not built here.

**To be added to this record:** what of the customer's own message reaches
the model provider (D10). Its place is the inventory below.

## Disclosure, channel by channel (2026-10-19)

Actions are guarded; until now text was not. A correct label does not prove
that what the system writes reveals nothing it should not (taxonomy S11, L6).
Three rules, then an inventory of every channel as it stands.

**1. Text meant for the sender names only the sender's own account.** No
identifier, name or amount of another customer, ever. To a sender who is
unknown or suspected of impersonation, nothing about any account: not that it
exists, not that an invoice exists (ADR-007). Today no such text is sent --
there is no reply drafter, and the workflow's automatic-reply branch is a
placeholder gated by `automatic_reply_allowed` (ADR-011). The reply drafter
will be built with an automatic check of every outgoing text against this
rule: no other customer's identifiers, addresses, names or amounts.

**2. Every internal channel carries what its readers need, and no more.**
Finding codes and a link rather than evidence and prose wherever a link is
enough; the evidence stays behind authentication.

**3. A channel's readers are named.** A channel without a named audience,
retention and access is a leak waiting for a reader.

| Channel | Read by | Carries | Guard today | Open |
|---|---|---|---|---|
| `POST /disputes/investigate` response | the orchestrator (service key) | classification, finding codes, explanation, proposed action, sender, reply permission | service authentication; codes, not evidence | the explanation is internal prose and must never be forwarded to the sender as it is |
| Reply to the sender | the sender | nothing yet | `automatic_reply_allowed` (ADR-011) | rule 1 and its automatic check, with the reply drafter |
| Approval mail | the approvers' mailbox | run, classification, amount, explanation, link | the decision itself needs a signed-in approver | mail is forwarded and archived: the explanation could give way to codes and the link |
| Review alert, execution alert | the approvers' mailbox | codes, run, link; for an execution, the API's error text | no explanation, no evidence | -- |
| Triage mail | the triage mailbox | the unknown sender's address, codes, link | no explanation, no evidence; the request to confirm nothing (ADR-011) | -- |
| API error mail | the approvers' mailbox | sender, cited invoice, message length | never the message itself | -- |
| Review page, run page | a signed-in approver; the run page, either kind of caller | findings with evidence, explanation, trace | authentication (above) | the run page is readable by the service caller too (known limit above) |
| Audit database | whoever reads the file | the message verbatim, explanation, findings with evidence, amounts | a local file in this demonstrator | access and retention to define before production (Cloud SQL with IAM in the target, ADR-010) |
| Telemetry | the trace backend | identifiers, codes, verdict, counts, cost | never the message, the prompt, the context or an address (`tests/integration/test_tracing.py`) | the backend's own retention |
| Logs | the log sink | dispute id and error category | never an exception's text (taxonomy S7) | -- |
| Model provider | the engine's vendor | the message verbatim, the context and findings pseudonymised | aliases for every identifier in the context | the message itself is not masked (D10) |
| MCP server | whoever starts it | any customer's context, invoices and findings | read-only, no mutation reachable | no per-caller scope: it must not be exposed beyond the operator |
| Evaluation results | readers of this repository | case ids, codes, verdicts | synthetic data only | never run on real data into committed files |
