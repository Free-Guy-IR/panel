# Feature Specification: Content Filtering

**Feature Branch**: `feat/content-filtering`

**Created**: 2026-09-15

**Status**: Draft

**Input**: User description: "A content-filtering section for the PasarGuard panel, so a restricted config can be handed to a child. A new section in the panel platform area, like AdGuard Home, that blocks categories — social media, adult sites, games. It must be applicable to a specific node, and if possible to a specific inbound. Normal users must be unaffected. The UI/UX must be designed very professionally. Must work end to end on the test panel, with panel access for the operator and two subscription configs to verify with — one unrestricted, one restricted."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Hand a child a restricted config (Priority: P1)

The operator opens a new **Filtering** section in the panel, creates a profile called "Kids" and ticks the categories to block — adult, social media, gambling, games. They choose where it applies: one node, and one connection endpoint on that node. They save. They then hand a family member a subscription link that uses that endpoint. On that link, the blocked categories do not load. Every other customer on the same node, using any other endpoint, sees no change whatsoever.

**Why this priority**: This is the entire reason the feature exists. Without it there is nothing to demonstrate, and every other story is an enhancement to this one. It is also the smallest slice that delivers real value — one profile, one scope, one working config.

**Independent Test**: Create one profile, apply it to one endpoint on the test node, connect with the restricted config and confirm a blocked site fails while a neutral site loads. Then connect with an unrestricted config through the same node and confirm the blocked site loads normally. The pair of configs is the test.

**Acceptance Scenarios**:

1. **Given** no filter profile exists anywhere, **When** the operator opens the Filtering section, **Then** they see an empty state that explains what a profile does and offers a single obvious way to create the first one.
2. **Given** a profile blocking "adult" and "social" applied to endpoint E on node N, **When** a client connected through E requests a domain in either category, **Then** the request does not reach its destination.
3. **Given** the same profile, **When** a client connected through E requests a domain in no blocked category, **Then** the request succeeds normally.
4. **Given** the same profile, **When** a different client connects through a different endpoint on node N, **Then** every category loads normally for them, including the ones blocked on E.
5. **Given** a profile exists but is not applied to any scope, **When** any client connects anywhere, **Then** no traffic is affected anywhere in the fleet.
6. **Given** the operator selects an endpoint whose protocol cannot enforce domain rules, **When** they attempt to apply a profile to it, **Then** the panel refuses and states plainly which protocols can and cannot be filtered, rather than saving a rule that would silently do nothing.

---

### User Story 2 - Prove the filter is actually in force (Priority: P2)

The operator wants to know the filter is live, not merely saved. From the profile's page they can see, per node it targets, whether the node currently has the rules loaded, when it was last confirmed, and whether anything drifted. If a node is offline or out of date, that is stated as a fact rather than shown as success.

**Why this priority**: A filter that silently stops working is worse than no filter, because a parent believes a child is protected when they are not. This turns the feature from a setting into a guarantee. It is P2 only because P1 must exist first.

**Independent Test**: Apply a profile, confirm the panel reports it enforced on the target node, then take that node offline and confirm the panel reports it as not enforced rather than continuing to claim success.

**Acceptance Scenarios**:

1. **Given** a profile applied to node N, **When** the node has the rules loaded, **Then** the panel shows it as enforced with the time it was last confirmed.
2. **Given** a profile applied to node N, **When** node N is unreachable, **Then** the panel shows that enforcement cannot be confirmed and does not display it as protected.
3. **Given** a profile applied to node N, **When** the rules present on the node no longer match what the profile says, **Then** the panel reports the mismatch and offers to re-apply.

---

### User Story 3 - Change or lift a filter without collateral damage (Priority: P3)

The operator edits a profile (adds or removes a category), moves it to a different scope, or deletes it entirely. Customers who were never in scope are never interrupted. Customers who were in scope see the new rules take effect without losing their connection or needing a new subscription link.

**Why this priority**: Filters are not set once. A child grows up, a category turns out to be too broad, a parent wants games unblocked at the weekend. Without this the operator would have to rebuild the config each time. P3 because a first working version can be edited by delete-and-recreate.

**Independent Test**: Apply a profile, verify a site is blocked, remove that category from the profile, and verify the same site loads again — with the same subscription link and without reconnecting other users.

**Acceptance Scenarios**:

1. **Given** an applied profile, **When** the operator removes a category, **Then** that category becomes reachable for in-scope clients and no other category changes.
2. **Given** an applied profile, **When** the operator deletes it, **Then** all its restrictions lift and the affected endpoint returns to exactly its pre-filter behaviour.
3. **Given** any profile change, **When** it is saved, **Then** customers outside its scope experience no interruption of service.

---

### Edge Cases

- **A child bypasses the filter by resolving names themselves.** If a client looks a domain up locally and connects to the raw address, there is no name left to match. The feature must close this on any endpoint it protects, otherwise the protection is cosmetic.
- **A category list is stale or missing on a node.** Nodes carry their own copy of the category data. If a node's copy is absent or a different vintage, the same profile could enforce differently on different nodes. The panel must detect and surface this rather than assume uniformity.
- **A profile targets a node that is offline when it is saved.** The change must not be silently lost, and must not block the save for every other node.
- **Two profiles target the same endpoint.** There must be one defined outcome, not a race.
- **A blocked domain is also needed for something the child legitimately uses.** The operator needs a way to let a specific domain through without abandoning the whole category.
- **The endpoint being filtered is also used by paying customers.** The panel must make the blast radius visible before the operator saves, not after.
- **A node restarts.** Rules applied while it was running must survive, or the panel must report them as no longer enforced.
- **The operator applies a profile to a node but an unfiltered route to the same destination exists via another node in the same subscription.** The subscription handed to a child must not contain an unfiltered escape route.

## Requirements *(mandatory)*

### Functional Requirements

**Profiles and categories**

- **FR-001**: The system MUST let an operator create, rename, edit and delete named filter profiles.
- **FR-002**: A profile MUST consist of a selection drawn from a fixed catalogue of named content categories, each with a human-readable label and a short description of what it covers.
- **FR-003**: The category catalogue MUST at minimum cover adult content, social media, gambling and games, and MUST be extensible without a code change to the panel.
- **FR-004**: A profile MUST support an allow-list of individual destinations that are permitted even when a category they belong to is blocked.
- **FR-005**: A profile MUST support a block-list of individual destinations that are blocked even when no selected category covers them.
- **FR-006**: The system MUST show, for each category, an indication of how broad it is, so the operator understands the scope of what they are switching on before they save.

**Scoping and assignment**

- **FR-007**: An operator MUST be able to apply a profile to a specific node.
- **FR-008**: An operator MUST be able to narrow that application to a specific connection endpoint on that node.
- **FR-009**: The system MUST NOT alter traffic for any customer who is not within the scope of an applied profile.
- **FR-010**: Before saving, the system MUST show how many customers fall within the scope about to be affected.
- **FR-011**: When two or more profiles apply to the same scope, the system MUST apply the union of their restrictions, and MUST show the operator that an overlap exists.
- **FR-012**: The system MUST refuse to apply a profile to an endpoint whose protocol cannot enforce destination rules, and MUST name those protocols in the refusal.

**Enforcement integrity**

- **FR-013**: On any endpoint a profile protects, the system MUST close the path where a client supplies a raw address instead of a name, so that the filter cannot be bypassed by the client choosing its own name resolution.
- **FR-014**: Restrictions MUST take precedence over any pre-existing routing for the same traffic, so a filtered destination cannot be rescued by a more general rule.
- **FR-015**: Applying, changing or removing a profile MUST NOT require an action that interrupts service for customers outside its scope.
- **FR-016**: The system MUST record, per targeted node, whether the restrictions are currently loaded and when that was last confirmed.
- **FR-017**: The system MUST detect when the restrictions present on a node differ from what the profile specifies, and MUST report it rather than hide it.
- **FR-018**: The system MUST detect when a node's category data is missing or of a different vintage from its peers, and MUST report it.
- **FR-019**: When a targeted node is unreachable, the system MUST report enforcement as unconfirmed, and MUST NOT present it as protected.
- **FR-020**: Restrictions MUST survive a node restart, or the node MUST be reported as no longer enforced until they are restored.

**Operator experience**

- **FR-021**: The Filtering section MUST be reachable as its own area of the panel, not buried inside an unrelated screen.
- **FR-022**: The section MUST present an empty state that explains the concept and offers one clear first action.
- **FR-023**: Every destructive action MUST state what it will affect, in customer terms, before it is confirmed.
- **FR-024**: The operator MUST be able to see, for any customer, whether a filter applies to them and which profile it comes from.
- **FR-025**: The system MUST provide a way to check a single destination against a profile and report whether it would be blocked, without the operator needing to connect a client.
- **FR-026**: The section MUST be fully usable in the panel's existing languages and reading directions, and MUST match the panel's existing visual system rather than introducing a second design language.

**Safety**

- **FR-027**: Existing subscription links MUST continue to work unchanged, whether or not a filter applies to them.
- **FR-028**: The presence of the feature, with no profile applied, MUST produce no observable change to any customer's traffic.
- **FR-029**: All profile creation, modification, scope change and deletion MUST be recorded with who did it and when.

### Key Entities

- **Filter Profile**: A named set of restrictions the operator can reason about as a unit — for example "Kids". Holds the selected categories, the per-destination allow-list and block-list, and audit metadata. Exists independently of where it is applied.
- **Category**: A named group of destinations with a human-readable label, a description, and an indication of breadth. Supplied from a catalogue rather than typed by the operator.
- **Assignment**: The link between a profile and the place it takes effect — a node, optionally narrowed to one connection endpoint on that node. A profile with no assignment affects nothing.
- **Enforcement Status**: Per assignment and per node, the record of whether the restrictions are currently live, when that was last confirmed, and what discrepancy (if any) was found.
- **Destination Exception**: A single allowed or blocked destination attached to a profile, overriding the category selection for that one destination.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Starting from an empty Filtering section, an operator can produce a working restricted configuration in under 5 minutes without consulting documentation.
- **SC-002**: With a profile applied, 100% of tested destinations in a blocked category fail to load for an in-scope client, and 100% of tested destinations outside every blocked category load normally.
- **SC-003**: A client that resolves names locally and connects by raw address is blocked at the same rate as one that does not — the bypass closes completely, not partially.
- **SC-004**: Zero customers outside an applied profile's scope experience any change in reachability, latency or connection stability attributable to the feature, measured across the full apply / edit / delete cycle.
- **SC-005**: The operator can determine whether a given filter is currently in force on a given node within 10 seconds of opening the panel, without running a command or connecting a client.
- **SC-006**: When a node is offline or has drifted, the panel reports that state correctly in 100% of cases, and never reports protection that is not actually in place.
- **SC-007**: Applying, editing or removing a profile causes no disconnection for any customer outside its scope.
- **SC-008**: Two subscription configurations — one restricted, one unrestricted — demonstrate the difference end to end on the test panel and produce opposite, repeatable results against the same destination.

## Assumptions

Defaults chosen where the request did not specify. Each is a decision that can be revisited.

- **Scope of this delivery is the test panel only.** Production is not touched. Nothing here is promoted until the operator asks separately.
- **Enforcement point is the connection endpoint, not the individual customer.** A customer becomes filtered by being given a subscription that uses a filtered endpoint. This follows directly from the operator's request for per-node and per-endpoint control, and means a "restricted config" is a real, handable artefact rather than a per-account flag.
- **Blocked traffic is refused rather than left hanging.** A blocked request fails promptly instead of timing out, so the child's device shows an error quickly rather than appearing frozen. The panel does not attempt to display a custom block page, which would require intercepting encrypted traffic.
- **Category definitions come from the data already present on the nodes.** No new download or installation is introduced. This was verified on nine node containers spanning several image generations; it has not been verified on any remote node, so the plan must re-confirm it on the actual targets before rollout.
- **Overlapping profiles combine to the most restrictive outcome.** The union is the safe default: an overlap can only ever block more, never accidentally unblock something a parent asked to block.
- **The initial catalogue ships with adult, social media, gambling and games**, matching the categories the operator named, with room to add more later.
- **The restricted subscription contains only filtered endpoints.** Otherwise the child's own client would route around the filter using another endpoint in the same subscription, and the feature would be defeated by its own delivery mechanism.
- **The operator is the only role that manages filters in this version.** Handing a parent a limited view of their own child's profile is a later concern.
- **The existing oversight and review gates continue to apply** to every change this feature introduces; nothing here overrides them.

## Open Risks Carried Into Planning

These are not assumptions — they are known unknowns the plan must resolve before implementation commits to an approach.

- **R-1**: Applying rules to a running node without restarting it is the only way to satisfy FR-015 on a shared node. That capability appears to exist but has never been exercised. Insertion order, removal, survival across restart and behaviour while a node is offline are all unestablished. If it proves unreliable, the alternative — a change that restarts every node on a core — is unacceptable for a shared production core and would force a different scoping model.
- **R-2**: Category data uniformity across the fleet is strongly indicated but unproven on remote nodes. If it varies, the same profile enforces differently in different places, and FR-018 becomes load-bearing rather than defensive.
- **R-3**: Closing the raw-address bypass (FR-013) changes how an endpoint inspects traffic. The performance and compatibility cost of enabling that on an endpoint that currently does not do it is unmeasured.
