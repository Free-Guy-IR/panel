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
7. **Given** a profile in strict mode applied to endpoint E, **When** an in-scope client opens a connection whose destination name cannot be determined at all, **Then** the connection is refused rather than allowed through unclassified.
8. **Given** a profile NOT in strict mode applied to endpoint E, **When** an in-scope client opens a connection whose destination name cannot be determined, **Then** the connection is allowed, and the panel has already told the operator that this is the trade-off they chose.
9. **Given** a customer whose subscription contains only filtered endpoints, **When** that customer edits their client configuration by hand to point at an unfiltered endpoint on the same node, **Then** their credentials are rejected there.

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

- **A child bypasses the filter by resolving names themselves.** If a client looks a domain up locally and connects to the raw address, there is no name in the request to match. Recovering the name from inside the traffic closes the common case, but not every case.
- **The destination name is genuinely unavailable.** Some traffic carries no recoverable name at all — the name may be encrypted as part of the connection handshake, or the destination may simply be an address with no name attached. Category matching works on names, so this traffic cannot be classified. The feature must decide what happens to it rather than letting it through by default and calling the endpoint protected.
- **A child edits the configuration file by hand.** Handing out a subscription that lists only filtered endpoints controls what the client is *told* about. It does not, on its own, stop a determined child from editing the file to point at an unfiltered endpoint. Isolation has to be enforced where credentials are checked, not only in what the subscription advertises.
- **A category list is stale or missing on a node.** Nodes carry their own copy of the category data. If a node's copy is absent or a different vintage, the same profile could enforce differently on different nodes. The panel must detect and surface this rather than assume uniformity.
- **A profile targets a node that is offline when it is saved.** The change must not be silently lost, and must not block the save for every other node.
- **Two profiles target the same endpoint.** There must be one defined outcome, not a race.
- **A blocked domain is also needed for something the child legitimately uses.** The operator needs a way to let a specific domain through without abandoning the whole category.
- **The endpoint being filtered is also used by paying customers.** The panel must make the blast radius visible before the operator saves, not after.
- **A node restarts.** Rules applied while it was running must survive, or the panel must report them as no longer enforced.
- **The operator applies a profile to a node but an unfiltered route to the same destination exists via another node in the same subscription.** The subscription handed to a child must not contain an unfiltered escape route.

## Clarifications

### Session 2026-09-17

- Q: Several nodes can be attached to the same core configuration. When a profile is applied to ONE of them, what happens to its peers? → A: Nothing. FR-007 and FR-009 are absolute; a peer node is outside the scope and MUST NOT be affected.
- Q: A rule written into a shared core configuration reaches every node on that core, so how is a node-pinned filter delivered? → A: It is delivered live to that node alone; only filters that legitimately reach every node on a core may be written into the shared configuration.
- Q: A live-delivered rule does not survive a restart on its own. What restores it? → A: The panel re-applies it — periodically, and promptly when a node comes back — and reports the filter as unenforced until it is restored, per FR-020.

- **FR-009a**: When several nodes share one core configuration, a profile applied to one of them MUST NOT reach the others. The system MUST NOT write a node-scoped restriction into a configuration that other nodes also load; only a restriction that reaches every node on that configuration may be stored there.
- **FR-009b**: The panel MUST show the operator, before they save, exactly which nodes a choice will reach, and MUST name the peer nodes that share a configuration with the chosen node.
- **FR-020a**: A restriction that is delivered to a node live rather than stored in its configuration MUST be re-applied automatically after that node restarts or reconnects, and MUST be reported as unenforced until it is confirmed present again.
- **FR-020b**: Re-application MUST NOT impose a per-node cost on the routine health cycle that scales with the fleet; it runs on its own schedule and on a node's return, not on every health tick.

## Requirements *(mandatory)*

### Functional Requirements

**Profiles and categories**

- **FR-001**: The system MUST let an operator create, rename, edit and delete named filter profiles.
- **FR-002**: A profile MUST consist of a selection drawn from a supplied catalogue of named content categories, each with a human-readable label and a short description of what it covers. The operator MUST be able to build a complete, useful profile **without typing a single domain**.
- **FR-002a**: The catalogue MUST be **two levels deep**: a group the operator recognises (Social networks, Adult, Games, Gambling, Advertising) and, beneath it, the individual services inside that group.
- **FR-002b**: The operator MUST be able to block an entire group with one action, **or** pick individual services within it — block Instagram and TikTok while leaving WhatsApp reachable, for example. Selecting some but not all children MUST be shown as a distinct, partially-selected state, never as either fully on or fully off.
- **FR-003**: The catalogue is derived from the category data already present on the nodes, so it stays current without a panel code change. Confirmed present in the shipped data: 1,429 categories, including individually separable `instagram`, `telegram`, `whatsapp`, `facebook`, `twitter`, `x`, `tiktok`, `youtube`, `discord`, `reddit`, `pinterest`, `linkedin`, `threads`, alongside the umbrella `category-porn`, `category-games`, `category-ads-all`, `category-social-media-!cn` and `category-communication`.
- **FR-003a**: Because a category's breadth varies enormously — `threads` covers 2 domains, `category-porn` covers 6,635, `category-ads-all` covers 170,624 — the panel MUST show the operator the size of what they are switching on, at both group and service level, before they save.
- **FR-004**: A profile MUST support an allow-list of individual destinations that are permitted even when a category they belong to is blocked.
- **FR-005**: A profile MUST support a block-list of individual destinations that are blocked even when no selected category covers them.
- **FR-006**: The system MUST show, for each category, an indication of how broad it is, so the operator understands the scope of what they are switching on before they save.

**Scoping and assignment**

- **FR-007**: An operator MUST be able to apply a profile to a specific node.
- **FR-008**: An operator MUST be able to narrow that application to a specific connection endpoint on that node.
- **FR-008a**: Different endpoints MUST be able to carry **different** profiles at the same time on the same node — one endpoint blocking adult content only, another blocking adult content and all social networks — so a household can be given several tiers without needing several nodes.
- **FR-009**: The system MUST NOT alter traffic for any customer who is not within the scope of an applied profile.
- **FR-010**: Before saving, the system MUST show how many customers fall within the scope about to be affected.
- **FR-011**: When two or more profiles apply to the same scope, the system MUST apply the union of their restrictions, and MUST show the operator that an overlap exists.
- **FR-011a**: When a node-wide assignment and an endpoint-specific assignment both cover the same endpoint, the system MUST apply the union of both, never only the narrower one, and MUST show both as sources on that endpoint.
- **FR-011b**: Within a single profile, precedence MUST be: allow-list entry wins over block-list entry, and a block-list entry wins over a category selection. The order MUST be stated in the UI where the lists are edited.
- **FR-011c**: Where two profiles disagree on the same destination — one allowing it, another blocking it — the block MUST win, consistent with the union rule, and the conflict MUST be shown to the operator.
- **FR-011d**: A destination entry MUST state whether it covers a single name or that name and everything beneath it, and the panel MUST show which interpretation is in effect for each entry.
- **FR-012**: The system MUST refuse to apply a profile to an endpoint whose protocol cannot enforce destination rules, and MUST name those protocols in the refusal.

**Enforcement integrity**

- **FR-013**: On any endpoint a profile protects, the system MUST recover the destination name from the traffic itself wherever the protocol allows it, so that a client which resolves names locally and connects by address is still matched against the profile.
- **FR-013a**: The system MUST NOT claim that name recovery is complete. Where the destination name is encrypted or absent, it MUST be treated as a distinct outcome — *unclassifiable* — and not silently as *allowed*.
- **FR-013b**: Every profile MUST carry a strict-mode setting that decides what happens to unclassifiable traffic on the endpoints it protects: refuse it, or permit it. The setting MUST default to refusing, because a profile's purpose is protection.
- **FR-013c**: The panel MUST state, at the point the operator chooses strict mode, what each choice costs: refusing unclassifiable traffic will also block legitimate services that connect by address, and permitting it leaves a way past the filter.
- **FR-013d**: The system MUST make the residual gap observable — the operator MUST be able to see how much traffic on a protected endpoint was unclassifiable, so a bypass in use is visible rather than invisible.
- **FR-013e**: The credentials of a customer within a profile's scope MUST be rejected on unfiltered endpoints, so that editing the client configuration by hand does not obtain an unfiltered route.
- **FR-014**: Restrictions MUST take precedence over any pre-existing routing for the same traffic, so a filtered destination cannot be rescued by a more general rule.
- **FR-015**: Applying, changing or removing a profile MUST NOT require an action that interrupts service for customers outside its scope.
- **FR-016**: The system MUST record, per targeted node, whether the restrictions are currently loaded and when that was last confirmed.
- **FR-017**: The system MUST detect when the restrictions present on a node differ from what the profile specifies, and MUST report it rather than hide it.
- **FR-018**: The system MUST detect when a node's category data is missing or of a different vintage from its peers, and MUST report it.
- **FR-019**: When a targeted node is unreachable, the system MUST report enforcement as unconfirmed, and MUST NOT present it as protected.
- **FR-020**: Restrictions MUST survive a node restart, or the node MUST be reported as no longer enforced until they are restored.

**Operator experience**

- **FR-021**: The Filtering section MUST live in the panel's **Platform** area as its own destination, not buried inside an unrelated screen.
- **FR-021a**: Reaching a usable state MUST NOT require the operator to consult anything outside the screen: the categories are presented, the scopes are pickable from what already exists, and nothing has to be typed in a syntax the operator must learn. Free-text entry exists only for the per-destination exceptions in FR-004 and FR-005, which are an escape hatch, not the main path.
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
- **SC-002**: With a profile applied, 100% of tested destinations in a blocked category fail to load for an in-scope client, and 100% of tested destinations that are outside every blocked category *and whose name is recoverable* load normally.
- **SC-003**: A client that resolves names locally and connects by address, but whose destination name is still recoverable from the traffic, is blocked at the same rate as one that sends the name outright.
- **SC-003a**: Under strict mode, 100% of connections whose destination cannot be identified are refused. Under permissive mode, 100% are allowed. In both cases the outcome matches what the panel told the operator it would be — the measure is that the setting is honest, not that the gap is zero.
- **SC-003b**: The residual bypass surface is demonstrated rather than asserted: the delivery includes a worked example of traffic that cannot be classified, showing what each mode does with it.
- **SC-003c**: A restricted customer's credentials, presented on an unfiltered endpoint, are rejected in 100% of attempts.
- **SC-004**: Zero customers outside an applied profile's scope experience any change in reachability, latency or connection stability attributable to the feature, measured across the full apply / edit / delete cycle.
- **SC-005**: The operator can determine whether a given filter is currently in force on a given node within 10 seconds of opening the panel, without running a command or connecting a client.
- **SC-006**: When a node is offline or has drifted, the panel reports that state correctly in 100% of cases, and never reports protection that is not actually in place.
- **SC-007**: Applying, editing or removing a profile causes no disconnection for any customer outside its scope.
- **SC-008**: Two subscription configurations — one restricted, one unrestricted — demonstrate the difference end to end on the test panel and produce opposite, repeatable results against the same destination.

## Delivery

What the operator receives when this is done. These are acceptance conditions, not nice-to-haves.

- **D-001**: The Filtering section is live on the test panel and reachable from its navigation.
- **D-002**: The operator is given the test panel's address and a working account with full rights over the Filtering section, and that account is verified by actually signing in and completing a create / apply / verify / remove cycle — not merely issued.
- **D-003**: Two subscription links are handed over: one restricted by a profile, one not. Both are generated the normal way, so they are ordinary subscriptions rather than hand-built artefacts.
- **D-004**: A short verification script the operator can follow themselves, naming the exact destinations to try on each link and the result to expect, including one unclassifiable destination so the strict-mode trade-off is visible rather than theoretical.
- **D-005**: A written statement of what this does **not** stop, so the protection is not oversold to a parent.

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
- **Strict mode defaults to on for new profiles.** A profile exists to protect someone, so the safe default is to refuse what cannot be identified. The operator can turn it off per profile once they see what it costs.
- **The existing oversight and review gates continue to apply** to every change this feature introduces; nothing here overrides them.

## Open Risks Carried Into Planning

These are not assumptions — they are known unknowns the plan must resolve before implementation commits to an approach.

- **R-1**: Applying rules to a running node without restarting it is the only way to satisfy FR-015 on a shared node. That capability appears to exist but has never been exercised. Insertion order, removal, survival across restart and behaviour while a node is offline are all unestablished. If it proves unreliable, the alternative — a change that restarts every node on a core — is unacceptable for a shared production core and would force a different scoping model.
- **R-2**: Category data uniformity across the fleet is strongly indicated but unproven on remote nodes. If it varies, the same profile enforces differently in different places, and FR-018 becomes load-bearing rather than defensive.
- **R-3**: Recovering destination names (FR-013) changes how an endpoint inspects traffic. The performance and compatibility cost of enabling that on an endpoint that currently does not do it is unmeasured.
- **R-4**: Name recovery has a hard ceiling. When the destination name is encrypted during the connection handshake, or when there is no name at all, no amount of inspection produces one. Strict mode (FR-013b) is the answer to that traffic, but its real-world cost is unknown: planning must measure how much ordinary, legitimate traffic on a protected endpoint is unclassifiable, because if that share is large, strict mode is unusable and the protection claim has to be weakened rather than the setting quietly defaulted off.
- **R-5**: Rejecting a restricted customer's credentials on unfiltered endpoints (FR-013e) depends on how credentials are scoped today. If they are currently accepted fleet-wide, this is a larger change than the rest of the feature combined, and planning must establish that before the delivery promise stands.
- **R-6**: Node-scoped restrictions are delivered live rather than stored, so their persistence depends entirely on re-application. If re-application is unreliable or too slow after a restart, a parent believes a child is protected during the gap. The window between a node returning and its filter being restored must be measured, not assumed.
