# PasarGuard Fork Constitution

## Core Principles

### I. Upstream Boundary

Upstream code is never edited to add a feature. Every addition lives under the fork seams
(`app/fork/*`, `dashboard/src/fork/*`, the registered router/model/job/nav hooks) and reaches
upstream only through those seams. A seam that cannot be honoured MUST fail loudly at startup,
never silently degrade. Route paths, table names and nav ids MUST NOT shadow upstream ones.

Rationale: the fork must keep merging upstream releases; every line inside upstream files is a
future conflict and an invisible behaviour change.

### II. Verified Ground Before Design

A specification or plan MUST rest on facts measured on the real system, not on assumptions about
it. Wire formats, rates, limits and behaviours are probed and the evidence is recorded with the
spec. Where a fact cannot be measured, the document MUST say so explicitly and state the
assumption used in its place, together with the bound the design is sized for.

Rationale: two features in this repository were nearly built on wrong beliefs (rule survival
across restart, log-line format); the probe was cheaper than the rewrite would have been.

### III. Test Server First, Production Untouched

Every feature is built, deployed and proven end to end on the test server before it exists
anywhere else. Production is not modified, restarted or reconfigured by feature work; reading
production for evidence is allowed only through the pinned ssh wrapper. Existing subscription
links, user traffic and the operator's own backup system MUST be unaffected by the presence of a
feature that is not yet assigned or enabled.

### IV. Fleet Safety

A change scoped to one node, inbound or user MUST NOT restart a shared core or re-push users who
are not in scope. Whatever a feature installs on a node MUST survive a node restart and a panel
restart. Failure is explicit: an unreachable node, a dropped stream or a full buffer is shown as
such in the panel, never displayed as success. Storage and memory used by a feature MUST be
bounded by a stated cap and a stated retention, enforced by the feature itself.

### V. Clean Code

No explanatory comments or docstrings are written in any file, including scripts and tests; the
code carries its meaning through names and structure. A change MUST NOT add TypeScript errors
against the recorded baseline nor ruff findings; `vite build` does not type-check, so `tsc` is
run explicitly. Migrations are reversible and rehearsed on a copy of real data before they run
on the test server.

### VI. Security and Privacy

No secret, server address, token or dump is committed or pushed. Administrative credentials are
transmitted only over loopback or an ssh tunnel, never over plaintext HTTP. New endpoints reuse
existing permission scopes with the least privilege that fits and are refused to any admin
lacking them. Data about customers (destinations, addresses, identities) is kept only as long as
the feature needs it, with the retention stated in the spec and purged automatically.

### VII. Independent Review and Record

No change is done until three reviewers approve it independently and unanimously; partial
approval is reported as partial. Every work step is written to the knowledge graph as it
happens. Commits carry the fork identity only, with no assistant attribution, and every push is
followed by a CI verification for that exact commit.

## Operational Constraints

- Panel runtime is Python ≥ 3.14 and Bun; validation of Python imports happens on the server.
- Servers are reached only through `scratchpad/ssh_fleet.sh` (key-only, pinned host keys).
- The panel database is changed only through the panel API, CLI or Alembic — never by hand.
- Versions move by exactly one step; PATCH by default, MINOR only for a finished milestone.
- The test server's operator backup script and its cron are never touched.

## Development Workflow

Every request classified as SPEC-WORK runs the Spec Kit pipeline before code:
constitution → specify → clarify → plan → tasks → analyze → implement. Clarifications the
operator has delegated are resolved with the documented default and recorded in the spec.
Implementation is decomposed into parallel, non-overlapping owners (backend, dashboard,
verification) and every batch includes an independent verifier. A feature is complete when it is
proven on the test server with real proxied traffic, the reviewers approve, and the knowledge
graph holds the full record.

## Governance

This constitution supersedes habits and templates. An amendment is a change to this file with a
version bump: MAJOR for removing or redefining a principle, MINOR for adding one or materially
expanding guidance, PATCH for wording. Every plan records a Constitution Check listing each
principle and how the design satisfies it; a violation MUST be justified in writing in the plan's
Complexity Tracking table or the plan is not accepted. Reviewers verify compliance against this
file.

**Version**: 1.0.0 | **Ratified**: 2026-09-15 | **Last Amended**: 2026-09-15
