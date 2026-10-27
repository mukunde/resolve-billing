# ADR-009 — Trust boundaries: who may call, and whose identity a decision carries

**Status:** accepted · 2026-10-08 · covers D7 and D8; D10 (what of the
message reaches the model) added 2026-10-19; the evaluation's model
providers added 2026-10-24, and its prepaid ones (OpenAI, Scaleway)
2026-10-26

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

**What of the customer's message reaches the model (D10, 2026-10-19).**
The Claude adapter masks the message before the call
(`app/decision/masking.py`): invoice references the provider recognises take
the context's alias, or a neutral numbered token for one outside the context;
other context identifiers take their aliases; the customer's address and
display name, any other address, IBANs, card numbers and telephone numbers
become placeholders; a token that looks like one of our aliases is
neutralised. It is masking, not anonymisation: other people's names and free
text pass, and recognition is by pattern -- formats outside the tested ones
pass too. Invoice references in the provider's own formats are recognised
only when the provider supplies the hook; the API and the evaluation runner
wire it. The dispute itself is not modified. Bug record
`.specify/bugs/d10-message-masking/`.

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

In the inventory, a guard is marked **tested** when a test holds it,
**convention** when only practice does, and **to build** when it does not
exist yet. One limit runs across every row: a signed-in approver and the
service caller can read any customer's runs -- nothing scopes a reader to a
customer, which production would need (roles that carry a scope, above).

| Channel | Read by | Carries | Guard today | Open |
|---|---|---|---|---|
| `POST /disputes/investigate` response | the orchestrator (service key) | classification, finding codes, explanation, proposed action, sender, reply permission | service authentication; codes, not evidence (**tested**); for a suspected impersonator, explanation and reason name none of the account's identifiers (**tested**) | the explanation is internal prose and must never be forwarded to the sender as it is |
| JSON reads: `GET /actions/{id}`, `GET /runs/{id}` | the orchestrator, an approver | the action, its amount and fingerprint; the run with its findings, evidence and trace | authentication | no per-customer scope (above) |
| HTTP errors, 422 included | the caller, then whatever logs or mails it | the location and reason of the problem | a 422 never echoes the value sent (**tested**) | other error texts are not reviewed one by one |
| Reply to the sender | the sender | nothing yet | `automatic_reply_allowed` (ADR-011) | rule 1 and its automatic check, with the reply drafter (**to build**) |
| Approval mail | the approvers' mailbox | run, classification, amount, explanation, link | the decision itself needs a signed-in approver | mail is forwarded and archived: the explanation could give way to codes and the link |
| Review alert | the approvers' mailbox | codes, run, link | no explanation, no evidence (**tested**) | -- |
| Execution alert | the approvers' mailbox | action, run, link, and the API's error text | the API's errors no longer echo input | the error text is shown as received (**to build**: a category instead) |
| Triage mail | the triage mailbox | the unknown sender's address, codes, link | no explanation, no evidence (**tested**); the instruction to confirm nothing (ADR-011) | -- |
| API error mail | the approvers' mailbox | sender, cited invoice, message length, the API's error text | the customer's message only by its length (**tested**); a 422 echoes no input (**tested**) | the error text is shown as received (**to build**: a category instead) |
| n8n's execution data | whoever administers n8n | each run's input and the API's answers, as n8n keeps them | n8n holds no financial state (ADR-010) | execution retention and access, set in n8n (**convention**) |
| Review page | a signed-in approver | the proposal, its amount, codes, the policy's reason | authentication | no per-customer scope (above) |
| Run page | an approver or the service caller | findings with evidence, explanation, trace | authentication | readable by the service caller too (known limit above) |
| Audit database | whoever reads the file | the message verbatim, explanation, findings with evidence, amounts | a local file in this demonstrator (**convention**) | access and retention to define before production (Cloud SQL with IAM in the target, ADR-010) |
| Telemetry | the trace backend | identifiers, codes, verdict, counts, cost, an error's type | the attributes set never include the message, the prompt, the context or an address (**tested**); an exception leaves its type, never its text (**tested**) | the backend's own retention; spans other libraries emit are not reviewed |
| Logs | the log sink | this code's own warnings: dispute id and error category | the provider-outage warning carries no exception text (**tested**) | logs from the server, n8n and the SDKs are not reviewed |
| Model provider | the engine's vendor: Anthropic for the service; for evaluation runs on the synthetic development cases only, also Google (Gemini) and Mistral (Medium 3.5; Large 4 and GLM 5.3 -- a Z.ai model Mistral serves -- were planned, and refused on the free level, 2026-10-24); and, on prepaid accounts (spec 007, 2026-10-26), OpenAI (`gpt-6-luna`, `gpt-6.1-sol`; no regional commitment without an approved retention arrangement) and Scaleway (Paris, France; serving `qwen3.5-397b-a17b`, a model Alibaba developed: the developer is not the processor) | the message masked; the context and findings with identifiers replaced by aliases -- amounts, dates and line descriptions stay as they are | aliases for every identifier; in the message, aliases, placeholders for addresses, the customer's name, IBANs, cards and phones (**tested**, D10); the evaluation's vendors refused anywhere but the development set (**tested**) | pseudonymising identifiers does not anonymise the data; masking is by pattern, so other people's names, free text and unusual formats pass; on free tiers the vendor may train on what it receives -- each run records its data terms as declared, `unknown` included; OpenAI keeps abuse-monitoring logs up to 30 days and does not train on API data by default -- every request asks it not to store the response (`store` false, **tested**: the request, not what the vendor keeps); Scaleway keeps no request content by default, and may retain full content "for up to two weeks" when a request triggers an HTTP 500 or harms the service, and when traffic may be malicious (no duration given); it does not train base models, and the models' creators have no access; its prompt cache is per project, ephemeral, and keeps no prompt text |
| MCP server | whoever starts it | any customer's context, invoices and findings | read-only, no mutation reachable (**tested**) | no per-caller scope: it must not be exposed beyond the operator (**convention**) |
| Evaluation outputs | the person running them; the repository for committed files | stdout, `--json` files and baselines: case ids, codes, verdicts, and the engine's classifications | synthetic data only (**convention**) | a run on real data must stay private, out of committed files, with its own retention |
