# Tasks: Distrust the emSign roots in the panel image

**Input**: specs/004-certifi-emsign-distrust/spec.md and plan.md

## Phase 1: Change

- [X] T001 Pin certifi 2026.10.19 in uv.lock with no other package moving (FR-001)
- [X] T002 Deselect the emSign and eMudhra mozilla roots and rebuild the store with update-ca-certificates --fresh in the Dockerfile runtime stage (FR-002)
- [X] T003 [P] Add scripts/distrust_certifi_roots.py and run it in the runtime stage; re-include it in .dockerignore (FR-003, FR-007)
- [X] T004 [P] Record the Dockerfile override in OVERRIDES.md and the two upstream certifi commits in UPSTREAM_SYNC.md

## Phase 2: Checks

- [X] T005 [P] tests/test_distrust_certifi_roots.py on the host
- [X] T006 [P] scripts/image_trust_store_check.py inside the built image (US1)
- [X] T007 [P] scripts/image_distrust_runtime_check.py inside a throwaway container (US2)
- [X] T008 .github/workflows/image-trust-store.yml (FR-008)

## Phase 3: Proof

- [X] T009 RED on 3b10bde7f and on the bump-only image, GREEN on 1d3884cdd (SC-001, SC-002). Evidence in the CERTIFI-FIX scratchpad, certifi/: build-red-main-1.log with checks-red-main.log, build-bump-main-1.log with checks-bump-main.log, build-green-main-1.log with checks-green-main.log

## Phase 4: Adversarial fix round 1

- [X] T010 [P] One predicate in scripts/emsign_roots.py: four fingerprints plus whole-word O, OU and CN match (CA-3)
- [X] T011 [P] Byte-exact, fail-closed fixer with an independent post-check, owner and mode kept, symlink target rewritten, fingerprint match for unparsable blocks (CA-2, CA-7, CA-8, FR-003, FR-004)
- [X] T012 Store check finds every bundle and every bundle inside a wheel or zip; strip pip's vendored bundle and remove the ensurepip wheel (CA-1, FR-005)
- [X] T013 Run the store check as a build step (CA-4, FR-006)
- [X] T014 certifi>=2026.10.19 in pyproject.toml, re-locked (CA-5, FR-001)
- [X] T015 Container markers in the runtime guard and the workflow step for the sed expression (CA-6, FR-008)
- [X] T016 RED on 3b10bde7f and on 1d3884cdd with the new check, GREEN on the fix-round head with the check inside its build (SC-001, SC-002). Evidence in the CERTIFI-FIX scratchpad, certifi/: build-r1-red.log with checks-r1-red.log, build-r1-prev.log with checks-r1-prev.log, build-r1-head.log (the in-build store check) with checks-r1-head.log

## Phase 5: Adversarial fix round 2

- [X] T017 Set up Python 3.14 in the workflow before the step that parses the runtime check (C2-1)
- [X] T018 [P] Strip every discovered bundle, with one shared content-based discovery; a missing pip no longer stops the build (C2-2, C2-5)
- [X] T019 [P] Replace the 100-certificate floor with removed <= 4 known roots and kept > 0 in the fixer, and non-empty core sources in the check (C2-3)
- [X] T020 [P] Load app/utils/ssl_context.py by path in both checks (C2-4)
- [X] T021 Fail the build when the ensurepip lookup fails (R2-2)
- [X] T022 GREEN on both fix-round-2 heads and a RED build that plants /opt/x/roots.txt after the strip (SC-001). Evidence in the CERTIFI-FIX scratchpad, certifi/: build-r2-head.log with checks-r2-head.log, build-r2-prod.log with checks-r2-prod.log, build-r2-planted-RED.log

## Phase 6: Adversarial fix round 3

- [X] T023 [P] Rewrite only genuine bundles: certifi.where(), or files whose lines outside the blocks are blank or # comments; never Python source or bytecode; plan every target before writing any (C3-1)
- [X] T024 [P] --image refuses to run unless PG_IMAGE_BUILD=1 (C3-2)
- [X] T025 [P] Zip discovery by magic or a readable end record, nested zips to three levels, every per-archive and per-member error an actionable failure (C3-3)
- [X] T026 [P] Cap removals at four distinct fingerprints, allowing duplicate entries (C3-4)
- [X] T027 [P] Probe the G1, C1, ECC C3 and ECC G3 subjects in the mechanism check (C3-5)
- [X] T028 Record the gzip and raw DER limitation in spec.md (R3-1)
- [X] T029 GREEN on both fix-round-3 heads, the mechanism probes RED on plain 3b10bde7f, and a RED build in which a planted Python file embedding an emSign PEM fails the build (SC-001, SC-002). Evidence in the CERTIFI-FIX scratchpad, certifi/: build-r3-head.log with checks-r3-head.log, build-r3-prod.log with checks-r3-prod.log, build-r3-plain.log with checks-r3-plain-RED.log, build-r3-planted-py-RED.log

## Phase 7: Delivery re-cut

- [X] T030 Re-cut the reviewed change (fix/certifi-ghsa-6q48 at dd49b9f9c) into single commits on 3b10bde7f that each fit the gate view, with a minimal four-root Debian fixture, the test file split, and the mechanism check described as failing on a stock image (C4-3, C4-4)
