# Feature Specification: Distrust the emSign roots in the panel image

**Feature Branch**: `fix/certifi-ghsa-6q48`

**Created**: 2026-10-27

**Status**: Implemented (verified on built images 2026-10-27; adversarial fix rounds 1, 2 and 3 applied)

**Input**: Finding F-CERTIFI-GHSA-6Q48. Advisory GHSA-6q48-6c7w-9gw2 (critical, 2026-10-20): certifi before 2026.10.18 bundles emSign ECC Root CA - G3, and eight eMudhra intermediates under it have compromised keys. The panel image pins certifi 2026.7.22, which carries four emSign roots. Its Debian system store carries emSign roots too: production's September image (ca-certificates 20250419~deb12u1) carries all four, and today's bookworm (20250419~deb12u2) ships three of them, without ECC G3. The base interpreter's pip and the ensurepip bundled pip wheel vendor their own certifi bundle with three emSign roots. `create_outbound_ssl_context()` trusts the system store and certifi together, and the Telegram bot and the notification and webhook client use it.

## Clarifications

### Session 2026-10-27

- Q: Which emSign roots are distrusted? → A: All four (emSign Root CA - G1, emSign ECC Root CA - G3, emSign Root CA - C1, emSign ECC Root CA - C3), in every trust store the image carries. Production already runs with all four removed from both stores, and the image must not trust more than production does.
- Q: Which certifi version? → A: 2026.10.19 with the floor certifi>=2026.10.19, as upstream #1071 ships it. It drops ECC G3 but restores G1, C1 and ECC C3, so the image removes those three from the installed bundle at build time.
- Q: Is the certifi bump alone enough? → A: No. It leaves three emSign roots in certifi and the emSign roots of the system store.
- Q: What about pip's own bundle? → A: The base interpreter's pip trusts its vendored certifi bundle. The build strips every bundle the shared content-based discovery finds that holds a distrusted root, which covers pip's copy wherever pip lives and does not need pip to exist. The ensurepip bundled pip wheel carries the same roots; editing a wheel would break its RECORD, and the runtime never uses ensurepip, so the build removes that wheel, and fails if the ensurepip lookup itself fails.
- Q: How are bundles found? → A: By content, not by name: every regular file holding a PEM certificate marker, and every zip archive (PK magic or a readable zip end record, so a zipapp with a shebang prefix counts) with such a member, nested zips included down to three levels, across the whole image filesystem except /proc, /sys, /dev and the Debian source directory /usr/share/ca-certificates, whose deselected files are not trusted. A file that starts with zip magic but cannot be read, or a member that cannot be read (encrypted, unsupported compression, corrupt), fails the check with an "unreadable archive" line.
- Q: Which files may the build rewrite? → A: Only genuine bundles. certifi.where() always qualifies; any other file qualifies only if every line outside its certificate blocks is blank or starts with "#", and Python source or bytecode never does. A file that holds a distrusted root and does not qualify fails the build with a message that names it, so a human decides; nothing is written in that case. The rewrite runs only in the image build, which sets PG_IMAGE_BUILD=1.
- Q: Are gzip-compressed bundles or raw DER files scanned? → A: No, by decision (R3-1). OpenSSL, Python's ssl and certifi in this image read only PEM files, PEM directories and certifi's PEM bundle, so a gzip or raw DER file is not a trust store here. The check does not open them; a future consumer of such a format would need this decision revisited.
- Q: How is a root recognised? → A: One shared predicate (scripts/emsign_roots.py): the SHA-256 DER fingerprint of one of the four known roots, or the whole word emSign or eMudhra in the subject or issuer O, OU or CN. "SystemSign" is not a match.
- Q: Does the strip cover source installs? → A: No. It covers the image only. A source install that runs uv sync on a host gets certifi's own 2026.10.19 bundle.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Outbound TLS never trusts an emSign root (Priority: P1)

An operator runs the panel image. Whatever the Telegram bot, a webhook, a notification or pip connects to, a certificate chain that ends at any emSign or eMudhra root is refused.

**Why this priority**: an on-path attacker holding one of the compromised keys could otherwise read or change the panel's outbound traffic, including the Telegram bot token.

**Independent Test**: scripts/image_trust_store_check.py, which the image build itself runs.

**Acceptance Scenarios**:

1. **Given** the built image, **When** the check parses certifi.where(), /etc/ssl/certs/ca-certificates.crt, every file and hashed link under /etc/ssl/certs, every file and zip member the content-based discovery finds, and create_outbound_ssl_context().get_ca_certs() loaded from app/utils/ssl_context.py by path, **Then** none of them holds a distrusted root, and certifi, the system bundle, the system directory and the outbound context are not empty.
2. **Given** an image built from 3b10bde7f, from 3b10bde7f with only the certifi 2026.10.19 bump, or from 1d3884cdd (system store and certifi fixed, pip's bundles not), **When** the same check runs, **Then** it fails.
3. **Given** a Dockerfile change that lets any emSign root back into any of those bundles, or a bundle with an unusual name such as /opt/x/roots.txt, **When** the image is built, **Then** the build fails.

### User Story 2 - Deselection really removes trust (Priority: P1)

**Independent Test**: scripts/image_distrust_runtime_check.py inside a throwaway container of the built image. Its control and deselect steps prove the mechanism on any Debian image; its emSign subject probes fail on a stock image (verify code 85 or 7, because the stock root is found by name) and pass on the fixed one. The store check stays the main regression guard.

**Acceptance Scenarios**:

1. **Given** a throwaway test root installed the Debian way, **When** a local TLS server presents a leaf it signed, **Then** create_outbound_ssl_context() accepts the handshake (the control).
2. **Given** that root deselected with the Dockerfile's own sed expression and update-ca-certificates --fresh, **When** the handshake is repeated, **Then** it fails with a certificate-verify error and the root is in no system store.
3. **Given** a leaf from a test root carrying the subject of emSign Root CA - G1, emSign ECC Root CA - G3, emSign Root CA - C1 or emSign ECC Root CA - C3, with the real root's key type, **When** the handshake runs, **Then** it fails with "unable to get local issuer certificate", meaning no store holds an issuer with that name. On a plain upstream image the G1, C1 and ECC C3 probes find the stock root and fail with another error, so this scenario is RED there.

## Requirements *(mandatory)*

- **FR-001**: uv.lock MUST pin certifi 2026.10.19, pyproject.toml MUST require certifi>=2026.10.19, and no other package may move.
- **FR-002**: The runtime stage MUST deselect every mozilla/emSign*.crt and mozilla/eMudhra*.crt line of /etc/ca-certificates.conf and rebuild the store with update-ca-certificates --fresh; a Debian release without those files MUST still build.
- **FR-003**: The runtime stage MUST remove every distrusted root from the bundle certifi.where() names and from every other genuine bundle the discovery finds, idempotently, preserving every other byte, the mode and the owner, and MUST fail the build, writing nothing, when certifi cannot be found, a bundle cannot be read safely (byte order mark, glued or foreign PEM markers, broken base64, nothing left), a file holding a distrusted root is not a genuine bundle, or a bundle holds more than the four known distinct distrusted roots (duplicate entries of a known root are allowed).
- **FR-004**: A block the installed cryptography cannot parse MUST be matched by fingerprint, dropped when it is one of the four known roots and kept otherwise.
- **FR-005**: The ensurepip bundled pip wheel MUST be removed when it vendors a distrusted root; the base interpreter's pip MUST keep working. The filesystem-wide rewrite MUST refuse to run unless PG_IMAGE_BUILD=1, which only the Dockerfile sets.
- **FR-006**: The image build MUST run the store check after the strip and fail on any distrusted root; the check MUST NOT import the app package.
- **FR-007**: app/utils/ssl_context.py stays upstream code; the fork change lives in the Dockerfile, .dockerignore and scripts/.
- **FR-008**: A workflow MUST set up Python 3.14, build the image, run both checks inside it, and fail when the Dockerfile stops running the sed expression the mechanism check tests.

## Success Criteria *(mandatory)*

- **SC-001**: The store check fails on 3b10bde7f, on the bump-only image and on 1d3884cdd, and passes on the fixed image, whose build runs it.
- **SC-002**: The runtime check fails on a stock image, where its emSign subject probes find the stock roots (verify code 85 or 7), and passes on the fixed image, including its control step.
