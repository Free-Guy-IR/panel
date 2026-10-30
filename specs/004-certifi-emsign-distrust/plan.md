# Implementation Plan: Distrust the emSign roots in the panel image

**Branch**: `fix/certifi-ghsa-6q48` | **Date**: 2026-10-27 | **Spec**: specs/004-certifi-emsign-distrust/spec.md

## Summary

Bump certifi to 2026.10.19, deselect the emSign roots in the Debian store, strip them from every certifi bundle the image installs, drop the ensurepip wheel that vendors them, and make the image build fail when any trust store still holds one.

## Technical Context

- Runtime base: python:3.14-slim-bookworm with Debian ca-certificates; OpenSSL reads /usr/lib/ssl/cert.pem, a link to /etc/ssl/certs/ca-certificates.crt, and /etc/ssl/certs through ssl.create_default_context(), and create_outbound_ssl_context() adds certifi.where() on top.
- certifi bundle contents, parsed with cryptography x509: 2026.7.22 has G1, ECC G3, C1 and ECC C3 (121 certificates); 2026.10.18 has none (117); 2026.10.19 has G1, C1 and ECC C3 (120); 2026.10.23 has G1 (118). The stripped 2026.10.19 bundle is byte-identical to 2026.10.18.
- Debian store: ca-certificates 20250419~deb12u1 (production's September image) has all four; 20250419~deb12u2 (bookworm today) has G1, C1 and ECC C3.
- The base interpreter's pip 26.2.2 vendors a certifi bundle with G1, C1 and ECC C3, and so does the ensurepip bundled pip wheel.
- Constraints: no explanatory comments; app/utils/ssl_context.py stays upstream; scripts/ is excluded from the build context, so .dockerignore re-includes the three scripts the build runs.

## Design

1. pyproject.toml requires certifi>=2026.10.19; uv.lock moves only the certifi entry and its requires-dist specifier.
2. Dockerfile: upstream's ca-certificates step runs `sed -i -E 's@^(mozilla/(emSign|eMudhra)[^/]*\.crt)$@!\1@' /etc/ca-certificates.conf` and `update-ca-certificates --fresh`. A new step finds the base interpreter through sys.base_prefix, looks up ensurepip (failing the build if that fails), removes `ensurepip/_bundled/pip-*.whl`, and runs `PG_IMAGE_BUILD=1 python -B /code/scripts/distrust_certifi_roots.py --image`, which strips certifi.where() and every discovered genuine bundle that holds a distrusted root, and fails the build on any other file that holds one. The last new step runs `python -B /code/scripts/image_trust_store_check.py`.
3. scripts/emsign_roots.py holds what the fixer and the checks share: the four SHA-256 DER fingerprints and the whole-word emSign or eMudhra match on O, OU and CN; the content-based discovery over the whole filesystem except /proc, /sys, /dev and /usr/share/ca-certificates (regular files with a PEM certificate marker, zip archives by PK magic); and the loader that executes app/utils/ssl_context.py by path, so no check imports the app package.
4. scripts/distrust_certifi_roots.py reads and writes bytes, splits the bundle line by line, refuses anything it cannot read safely, matches unparsable blocks by fingerprint, refuses to remove more than the four known roots or to leave nothing, writes atomically through the link to the target with the original mode and owner, and re-parses what it wrote with x509.load_pem_x509_certificates. It never edits an archive.
5. scripts/image_trust_store_check.py and scripts/image_distrust_runtime_check.py also run mounted read-only into the built image.
6. .github/workflows/image-trust-store.yml sets up Python 3.14 (the scripts use 3.14 syntax), checks the sed expression, builds the image and runs both checks with no network.

## Verification

- tests/test_distrust_certifi_roots.py covers removal, over- and under-match, the fingerprints, idempotency, CRLF, symlinks, mode, every fail-closed input, the minimum, the post-check branches, non-positive serials, blocks a future cryptography refuses, the CLI, the store check and the runtime guard.
- RED/GREEN on images built from 3b10bde7f, from the bump-only tree, from 1d3884cdd and from each fix-round head, plus a RED build with a planted /opt/x/roots.txt that the build must refuse.
