#!/usr/bin/env python3
"""Lock feed.json so the repo can stay public without publishing the watch list.

The repo is public: anyone can read every file in it and every past version of
every file. That is fine for collect.py - the method is not a secret - but the
feed names all 26 accounts and carries their posts, so it cannot be committed
in the clear.

So the feed is encrypted here, in the runner, and only the ciphertext is
committed. The browser decrypts it with a passphrase the reader types in.

Format (feed.enc, JSON so it survives a text-mode git):

    {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 600000,
     "salt": "<base64, 16 bytes>", "iv": "<base64, 12 bytes>",
     "ct": "<base64, AES-256-GCM ciphertext with the tag appended>"}

Deliberately chosen to be exactly what SubtleCrypto does natively, so the page
needs no crypto library: PBKDF2-SHA256 -> AES-GCM is the intersection of "safe"
and "built into every browser".

On the iteration count: the ciphertext is downloadable by anyone, so guesses
are made offline with no rate limit and the passphrase is the only real
defence. 600k iterations makes each guess cost real work - it cannot save a
weak passphrase, only buy time. Use a long random one.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12


def derive(passphrase, salt, iterations=ITERATIONS):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))


def existing_salt(path):
    """Reuse the salt already in feed.enc, if there is one.

    A fresh salt every run would mean a fresh key every run, and the browser
    would have to redo 600k PBKDF2 rounds on every poll - about a second of
    stall, every thirty seconds. The salt only has to be unique to this feed,
    not to each encryption; what must never repeat for a given key is the IV,
    and that is generated fresh below. So keep the salt stable and let the
    browser derive the key once.
    """
    try:
        blob = json.loads(Path(path).read_text(encoding="utf-8"))
        salt = base64.b64decode(blob["salt"])
        return salt if len(salt) == SALT_BYTES else None
    except Exception:
        return None


def encrypt(plaintext, passphrase, salt=None):
    salt = salt or os.urandom(SALT_BYTES)
    iv = os.urandom(IV_BYTES)
    key = derive(passphrase, salt)
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITERATIONS,
            "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(blob, passphrase):
    """Only used to prove the file we just wrote can be read back."""
    d = base64.b64decode
    key = derive(passphrase, d(blob["salt"]), int(blob.get("iter", ITERATIONS)))
    return AESGCM(key).decrypt(d(blob["iv"]), d(blob["ct"]), None)


def unlock(args, passphrase):
    """feed.enc -> feed.json, so the merge can accumulate onto previous runs.

    The repo holds only the locked feed, but collect.py --merge needs the
    previous feed in the clear to fold this run's posts into it. The plaintext
    exists only in the runner's working directory and is never committed
    (.gitignore keeps it out).

    A missing feed.enc is not an error: that is simply the first run, or the
    first run after the passphrase changed. Say so and let the merge start from
    what this run scraped.
    """
    src = Path(args.src)
    if not src.exists():
        print(f"no {src} yet - starting from this run's posts only")
        return 0
    try:
        blob = json.loads(src.read_text(encoding="utf-8"))
        plaintext = decrypt(blob, passphrase)
    except Exception as exc:
        # Do NOT fall back to an empty feed here: that would silently discard
        # the archive and quietly republish a feed built from one run.
        print(f"could not unlock {src}: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("If the passphrase changed, delete feed.enc to start a new archive.",
              file=sys.stderr)
        return 1
    Path(args.dst).write_bytes(plaintext)
    print(f"unlocked {src} -> {args.dst} ({len(plaintext):,} bytes)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", default="feed.json")
    ap.add_argument("--out", dest="dst", default="feed.enc")
    ap.add_argument("--unlock", action="store_true",
                    help="decrypt instead (defaults become feed.enc -> feed.json)")
    args = ap.parse_args()

    passphrase = os.environ.get("FEED_PASSPHRASE", "")
    if not passphrase:
        print("FEED_PASSPHRASE is not set - refusing to write an unlocked feed.",
              file=sys.stderr)
        return 1

    if args.unlock:
        if args.src == "feed.json":
            args.src, args.dst = "feed.enc", "feed.json"
        return unlock(args, passphrase)

    src = Path(args.src)
    if not src.exists():
        print(f"{src} does not exist - nothing to encrypt.", file=sys.stderr)
        return 1

    plaintext = src.read_bytes()
    blob = encrypt(plaintext, passphrase, existing_salt(args.dst))

    # Never ship a file we have not proved is readable. A feed that cannot be
    # decrypted looks exactly like a feed that is simply empty, and it would be
    # committed and served before anyone noticed.
    if decrypt(blob, passphrase) != plaintext:
        print("round-trip check failed - refusing to write.", file=sys.stderr)
        return 1

    Path(args.dst).write_text(json.dumps(blob, separators=(",", ":")),
                              encoding="utf-8")
    print(f"locked {len(plaintext):,} bytes -> {args.dst} "
          f"({Path(args.dst).stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
