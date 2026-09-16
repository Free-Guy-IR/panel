# Feature Specification: Live Traffic Log

**Feature Branch**: `feat/content-filtering` (continues on the same working branch; spec directory `specs/002-live-traffic-log`)

**Created**: 2026-09-15

**Status**: Planned (clarified, analyzed 2026-09-15)

**Input**: User description: "A 'Live traffic log' tab in the Statistics section (لاگ زنده ترافیک), as a button at the top next to the existing ones. It shows, for all users, which sites are being requested — live. Filterable by the user's subscription name; look back over a time range of at most two days; type a username to see only that subscription; a fully professional UI; and records older than two days are deleted automatically so storage does not fill up."

## Clarifications

### Session 2026-09-15

The operator delegated every decision ("از من هیچ سوالی نکن"); each item below was resolved with the documented default and applied to the sections named.

- Q: When the operator types a username, is the match exact or partial? → A: The feed and history match one exact username; while typing, the box offers matching usernames to pick from so the exact name need not be remembered. (FR-004)
- Q: Which nodes and which log lines are recorded? → A: Every connected node is attached; only lines in the destination-report format are recorded and every other line is passed through unchanged to the per-node viewer. A node that emits no destination-report lines shows "no destination reports" rather than an error. (FR-011)
- Q: What does pausing collection stop? → A: Everything: no rows are stored and the live feed stops; the per-node viewer keeps working unchanged. (FR-013)
- Q: Whose rows does a non-sudo administrator see when a customer has no owning administrator? → A: Nobody's: unowned customers are visible to sudo administrators only. (FR-014)
- Q: Is the two-day retention fixed, or can the operator change it? → A: The operator (sudo) sets it from the same screen, between 1 hour and 30 days; two days remains the default. The history range the panel will answer follows whatever retention is configured. (FR-006, FR-009)
- Q: What if storage fills before the retention window elapses? → A: The operator can purge immediately from the same screen — either everything, or everything older than a chosen age — without waiting for the scheduled cycle. (FR-018)
- Q: What do "delivered" and "refused" mean in the outcome column? → A: "Refused" is a connection sent to the discard route that the content-filter feature installs; "delivered" names the route the connection actually left through. (FR-003)

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Watch what everyone is requesting, right now (Priority: P1)

The operator opens **Statistics** and presses **لاگ زنده ترافیک** next to "All nodes (live)" and "Inbound usage". A live feed starts: every new connection accepted by any monitored node appears within seconds as one row — time, subscription (username), destination (site and port), protocol, node, endpoint, and the outcome (delivered, or refused by a filter). Typing a username in the filter box narrows the feed to that one subscription; clearing it restores everyone. The operator can pause the feed to read it and resume without losing their place. A status strip shows which nodes are being collected from, which are not, and whether any lines were dropped.

**Why this priority**: This is the request in one sentence — "show me live which sites all users are requesting, and let me narrow it to one subscription". It is the smallest slice that delivers the value on its own.

**Independent Test**: Connect the restricted and the unrestricted demo subscriptions through the test node, open a few sites on each, and confirm every site appears in the feed under the right username within seconds; type one username and confirm only that subscription's rows remain; block one site by filter and confirm the row shows the refusal.

**Acceptance Scenarios**:

1. **Given** the operator has permission to read node logs, **When** they open Statistics, **Then** a button labelled "لاگ زنده ترافیک" is visible in the same control row as the existing view buttons, and pressing it replaces the charts with the live feed.
2. **Given** the live feed is open, **When** a customer connected through a monitored node opens a site, **Then** a row for that connection appears within three seconds, showing the customer's username (not an internal number), the destination name and port, the protocol, the node, the endpoint, and the outcome.
3. **Given** the feed shows many subscriptions, **When** the operator types an exact username, **Then** only rows for that subscription remain, new rows for other subscriptions are not shown, and clearing the box shows everyone again.
4. **Given** a destination is refused by a content filter for that customer, **When** the row appears, **Then** it is visibly marked as refused rather than delivered.
5. **Given** the operator presses pause, **When** new connections arrive, **Then** the visible rows do not move, a counter shows how many rows are waiting, and resuming shows them.
6. **Given** a monitored node goes offline, **When** the operator looks at the status strip, **Then** that node is shown as "not collecting" with the time collection stopped, never as healthy.
7. **Given** a node emits more lines than can be captured, **When** the operator looks at the status strip, **Then** the number of dropped lines for that node is shown, so the feed is understood as best-effort rather than complete.
8. **Given** an administrator who lacks the node-log permission, **When** they open Statistics, **Then** the button is absent and the underlying data cannot be requested.
9. **Given** a non-sudo administrator with the permission, **When** they open the feed, **Then** only their own users' rows appear, and a username belonging to another administrator returns nothing.

---

### User Story 2 - Look back over the last two days (Priority: P2)

The operator switches from the live feed to **history**, chooses a range — the last 15 minutes, hour, 6 hours, 24 hours, 48 hours, or a custom start and end no further back than the configured retention window — and optionally a username, a node, an endpoint, or a destination text. The result is a table of destinations with, for each, when it was first and last seen in the range and how many connections were made, newest first, paged. A small summary above the table shows the busiest subscriptions and the most requested destinations in that range.

**Why this priority**: "Did this subscription visit that site yesterday evening?" is the question that follows the live view naturally, and it is what the two-day retention exists for. It depends on the collection introduced by P1.

**Independent Test**: Generate traffic through the test node, wait a minute, open history for "last hour" with that username, and confirm the destinations, counts and times match what was generated; ask for a range beyond two days and confirm it is refused with a clear message rather than returning an empty table.

**Acceptance Scenarios**:

1. **Given** traffic was collected, **When** the operator selects "last hour", **Then** every destination requested in that hour is listed with first-seen, last-seen and connection count, newest first.
2. **Given** a username is entered, **When** history is fetched, **Then** only that subscription's destinations are listed, and the summary reflects that subscription only.
3. **Given** the operator enters a start time older than the configured retention, **When** they apply it, **Then** the panel refuses with a message stating that retention and does not silently truncate.
4. **Given** the table has more rows than one page, **When** the operator pages, **Then** rows are stable, ordered, and no row is duplicated or skipped between pages.
5. **Given** a subscription that was deleted after its traffic was recorded, **When** its history is viewed, **Then** the rows still show, labelled with the last known username and marked as a deleted user.
6. **Given** a destination text filter such as "google", **When** history is fetched, **Then** only destinations containing that text are listed.

---

### User Story 3 - Storage stays bounded without anyone watching it (Priority: P3)

Records older than two days disappear on their own. Beyond that, a hard ceiling on the number of stored records protects the database even if traffic is far higher than expected: when the ceiling is reached, the oldest records are removed first and the panel says so. The operator can pause collection entirely from the same screen (sudo only), and collection resumes by itself after a panel restart or a node reconnection.

**Why this priority**: The operator explicitly asked that storage never fills up. Without this, the feature would be a liability on a panel with about five thousand customers. It is P3 only because P1 and P2 must exist for there to be anything to bound.

**Independent Test**: Seed records with timestamps older than two days and confirm they are gone after the next purge cycle; set a small ceiling, generate more records than it allows, and confirm the count never exceeds it and the oldest go first; pause collection and confirm no new rows arrive; restart the panel and confirm the feed resumes without any operator action.

**Acceptance Scenarios**:

1. **Given** records older than the configured retention exist, **When** the purge cycle runs, **Then** none remain afterwards and newer records are untouched.
2. **Given** the record ceiling is reached, **When** new records arrive, **Then** the oldest records are removed to make room, the total never exceeds the ceiling, and the status strip states that the ceiling is active.
3. **Given** collection is paused, **When** customers connect, **Then** no new records are stored, the live feed shows a "paused" state, and the per-node log viewer keeps working.
4. **Given** the panel restarts, **When** it is back, **Then** collection from every connected node resumes within one minute without operator action.
5. **Given** a node reconnects after being offline, **When** it is healthy again, **Then** collection from it resumes automatically.

---

### Edge Cases

- A username is renamed after traffic was recorded: history is keyed by the customer's identity, so old rows display under the current name.
- A customer connects by raw address rather than by name: the destination column shows the address; the filter by destination text still applies.
- Two nodes carry the same customer at the same time: rows from both appear, each labelled with its node.
- The same customer opens the same site many times in a short period: the live feed shows each connection; history shows one row per destination per short interval with a count, so bursts do not swamp the table.
- The panel is deployed with more than one worker process: the live feed cannot be served correctly from a single process, so the tab states that live collection is unavailable in that deployment instead of showing a partial feed.
- The per-node log viewer is opened while collection is active: it must keep showing the complete stream; collection must not steal lines from it, nor it from collection.
- Clocks on nodes differ from the panel: times shown are the panel's time of receipt, so ordering is consistent across nodes.
- The purge or the ceiling removes rows while a history page is open: the next page may have fewer rows; no error is shown.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The Statistics page MUST offer a "لاگ زنده ترافیک" button in the same control row as the existing view buttons, visible only to administrators permitted to read node logs.
- **FR-002**: While the live view is open, the panel MUST show every newly accepted connection from every monitored node as one row within three seconds of the node reporting it.
- **FR-003**: Each row MUST show the time of receipt, the customer's username, the destination name or address, the port, the protocol, the node, the endpoint, and the outcome: "refused" when the connection went to the discard route installed by the content-filter feature, otherwise "delivered" with the name of the route it left through.
- **FR-004**: The live view and history MUST narrow rows to one subscription by exact username; the username box MUST offer matching usernames while typing; clearing the box MUST restore all subscriptions.
- **FR-005**: The live view MUST offer pause and resume without losing rows that arrived while paused, and MUST cap the rows kept in the browser so the page stays responsive.
- **FR-006**: The panel MUST retain a record of destinations for a configurable window, two days (48 hours) by default and settable between 1 hour and 30 days, and MUST let the operator query any range within that window, optionally narrowed by username, node, endpoint, and destination text.
- **FR-007**: History results MUST be paged, ordered newest first, and MUST show first-seen, last-seen and connection count per destination row.
- **FR-008**: A requested range older than the configured retention MUST be refused with an explicit message naming that retention; the panel MUST NOT silently clamp or return an empty result. The panel MUST NOT offer a range the configured retention cannot answer.
- **FR-009**: Records older than the configured retention MUST be removed automatically by the panel itself, on a schedule, without operator action.
- **FR-010**: The panel MUST enforce a configurable ceiling on stored records; when reached, the oldest records MUST be removed first and the condition MUST be visible to the operator.
- **FR-011**: Collection MUST attach to every connected node by itself when the panel starts and when a node becomes healthy, MUST detach when a node becomes unreachable, and MUST show the state and time per node; only destination-report lines are recorded, every other line is passed through, and a node that never emits destination reports is shown as "no destination reports" rather than as an error.
- **FR-012**: The panel MUST count lines it could not keep (dropped by the node or by the panel under load) and MUST show the count per node, so the operator knows the log is best-effort.
- **FR-013**: A sudo administrator MUST be able to pause and resume collection from the same screen; while paused, nothing is stored, the live feed stops and says so, and the per-node log viewer keeps working unchanged.
- **FR-014**: Access MUST be limited to administrators permitted to read node logs; a non-sudo administrator MUST see only rows belonging to their own users, in both the live view and history; customers without an owning administrator are visible to sudo administrators only.
- **FR-015**: Collection MUST NOT alter any node configuration, restart any core, or change what customers can reach; the existing per-node log viewer MUST continue to show the full stream while collection is active.
- **FR-016**: The stored record MUST NOT include the customer's source address; the feature records destinations only.
- **FR-017**: All labels MUST be provided in English and Persian, with the Persian label of the button exactly "لاگ زنده ترافیک".
- **FR-018**: A sudo administrator MUST be able to purge stored records on demand from the same screen — all records, or only those older than a chosen age — with a confirmation step, and the panel MUST report how many records were removed. Because removing records does not by itself return disk space, the operator MUST be able to ask for that space to be reclaimed as part of the same action, and the panel MUST report how much was returned or state plainly that none was.
- **FR-019**: A sudo administrator MUST be able to change the retention window from the same screen; the new value MUST take effect for both the automatic purge and the range the panel will answer, without a restart.

### Key Entities

- **Traffic Event**: one accepted connection as reported by a node — receipt time, customer, destination, port, protocol, node, endpoint, outcome. Shown live; not stored one-by-one.
- **Traffic Record**: the stored form — one row per customer, node, endpoint, destination, port, protocol and outcome within a short time bucket, with first-seen, last-seen and a connection count. Retained for the configured retention window, subject to the ceiling.
- **Collection Status**: per node — collecting / not collecting / paused, since when, lines received, lines dropped, records written.
- **Collection Setting**: whether collection is enabled, the configured retention window (1 hour to 30 days, 48 hours by default), and the record ceiling.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A connection made through a monitored node appears in the live feed within 3 seconds in at least 95% of trials.
- **SC-002**: With a username typed, 100% of the rows shown belong to that subscription.
- **SC-003**: A history query over any range up to the configured retention returns its first page within 2 seconds when the store holds records at its ceiling.
- **SC-004**: Fifteen minutes after a record passes the configured retention window, it is no longer retrievable.
- **SC-005**: The number of stored records never exceeds the configured ceiling, verified under a synthetic load that exceeds it.
- **SC-006**: After a panel restart, collection from every connected node resumes within 60 seconds with no operator action.
- **SC-007**: A non-sudo administrator cannot obtain a single row of another administrator's users, in the live feed or history, across the full test matrix.
- **SC-008**: The existing traffic matrix (restricted vs unrestricted subscriptions) passes unchanged while collection is active, and the per-node log viewer shows the same lines it showed before the feature.

## Assumptions

- Retention defaults to 48 hours and is operator-adjustable between 1 hour and 30 days; the bound exists so a careless value cannot turn the feature into unbounded storage.
- The record ceiling defaults to 2,000,000 stored records and is adjustable by configuration; at roughly 120 bytes per record this bounds the table near 250 MB before indexes.
- History aggregates connections per destination within five-minute buckets; the live feed is not aggregated.
- Times are the panel's time of receipt, not the node's own clock.
- The production panel runs as a single all-in-one process (verified: no worker-count override, default role); the live feed is specified for that deployment and declares itself unavailable in multi-worker deployments.
- The node's own capture buffer can drop lines under burst; the feature is best-effort by nature and says so rather than claiming completeness.
- Only the test panel is touched; production receives nothing from this work until the operator decides.

## Deployment shape this feature requires

Live collection runs inside the process that owns the node connections. It is therefore available
only when the panel runs as a single web worker in a role that runs nodes, which is how the operator's
panel runs today. In any other shape — several web workers, or a split where the API role and the node
role are separate processes — the collector reports itself unavailable with the reason shown on the
page, and records nothing rather than recording partially.

Three consequences follow and are accepted rather than worked around:

- The live feed, the in-memory buckets and the manual purge's bucket invalidation all live in that one
  process. A purge issued from a different process would clear the database but not that process's
  pending buckets.
- The storage reclamation single-flight guard is per-process.
- Retention deletes whole five-minute buckets whose start is older than the cutoff, so up to one bucket
  width of slightly newer data can go with them. Deleting early is the safe direction for a retention
  promise.

Making the feature work across processes needs an inter-process transport for live events and for
maintenance commands. That is a larger change than this feature and is not attempted here.

## A property of history paging, stated so it is not mistaken for a bug

A record's last-seen time keeps moving while its five-minute bucket is still receiving hits. History
pages are ordered by last-seen and then by id, so a row in the current bucket can move between pages
while an operator is paging through it, appearing twice or not at all. Rows in every earlier bucket
are settled and page stably. Widening the range or paging again resolves it, and no data is lost.

## One data path the owner-only policy deliberately does not cover

The panel's pre-existing raw per-node log viewer streams the same access lines, and those raw lines
carry the client's source address as well as the destination. That viewer stays gated on the
`nodes.logs` permission rather than on full panel access, because the operator scoped this change to
the four named surfaces. An administrator holding `nodes.logs` can therefore still see a superset of
what the traffic log shows, live, through that older screen. This is a recorded decision, not an
oversight; closing it means putting the same owner gate on the raw viewer.

## What "purge everything" means for a five-minute bucket that is still open

A purge deletes every stored record and then clears the collector's memory. The moment it
starts is read when the request arrives, before any lock is taken, because that is when the
operator asked. Anything the collector accepted from that instant onward survives, whether it
arrived while the purge waited for the flusher, while it waited for a database connection, or
while the rows were being deleted.

A surviving bucket keeps every one of its hits rather than only the ones not yet written,
because the purge has just deleted every row: after it, nothing is written any more. Keeping
only the unwritten portion would have silently discarded any hit that happened to be flushed
in the moments between the request arriving and the purge acquiring its lock.

One imprecision remains, and it is a consequence of bucketing rather than a defect. A bucket
is a five-minute aggregate with one first-seen and one last-seen time and no per-connection
timestamps. If such a bucket was still open when the purge ran, there is no way to tell which
of its hits happened before the purge and which after. The collector keeps all of them and
re-dates the bucket to the purge moment.

The alternative would be to drop the whole bucket, which discards connections that genuinely
happened after the operator asked for a clean slate. Between showing a little more than was
asked for and silently losing live traffic, this feature chooses the former. The effect is
bounded by one bucket per destination key and disappears at the next bucket boundary.
`experiments/15_purge_race.py` pins both halves of this behaviour so it cannot change by
accident.
