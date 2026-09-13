#!/usr/bin/env python3
import hashlib
import re
import sys

MAX_LEN = 128
DIGEST_LEN = 8
INVALID = re.compile(r"[^a-zA-Z0-9_.-]")
LEADING_OK = re.compile(r"[a-zA-Z0-9_]")


def image_tag(ref: str) -> str:
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:DIGEST_LEN]
    slug = INVALID.sub("-", ref).lstrip("-.")
    if not slug or not LEADING_OK.match(slug):
        slug = "ref"
    slug = slug[: MAX_LEN - DIGEST_LEN - 1].rstrip("-.")
    if not slug:
        slug = "ref"
    return f"{slug}-{digest}"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: image_tag.py <ref>\n")
        raise SystemExit(2)
    print(image_tag(sys.argv[1]))
