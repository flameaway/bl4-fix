"""
Checks release signing offline (mods/bl4_mod_menu/signing.py, tools/release_key.py): a throwaway key
pair signs and verifies through Windows' crypto, tampering is caught, the setup app's copy of the
verifier is byte-identical, and the pinned public keys are well-formed. Never touches the real
release key (only reads its public half if it exists, for `check`).

    python dev/signing_test.py
"""

from __future__ import annotations

import base64
import ctypes
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import release_key  # noqa: E402
import signing  # noqa: E402  # release_key put mods/bl4_mod_menu on sys.path

results: list[tuple[bool, str]] = []


def check(ok: object, what: str) -> None:
    results.append((bool(ok), what))
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def throwaway_key() -> bytes:
    """A new private key blob (bcrypt), never saved."""
    _, bc = release_key._libs()
    alg, key = release_key.handle(), release_key.handle()
    release_key._ok(bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "ECDSA_P256", None, 0), "open")
    try:
        release_key._ok(bc.BCryptGenerateKeyPair(alg, ctypes.byref(key), 256, 0), "generate")
        release_key._ok(bc.BCryptFinalizeKeyPair(key, 0), "finalize")
        try:
            return release_key._export_bc(bc, key, "ECCPRIVATEBLOB")
        finally:
            bc.BCryptDestroyKey(key)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)


def main() -> int:
    import hashlib

    private = throwaway_key()
    public = release_key.backup_public(private)
    data = b'{"format": 1, "release": "v-test", "mods": {}}'
    raw = release_key._sign_backup(private, hashlib.sha256(data).digest())
    check(len(raw) == 64 and len(public) == 72, "a P-256 signature (64 bytes) and public blob (72 bytes)")
    check(signing.verify_with(public, data, raw), "the signature verifies")
    check(not signing.verify_with(public, data + b" ", raw), "changed data is refused")
    check(not signing.verify_with(public, data, bytes([raw[0] ^ 1]) + raw[1:]), "a changed signature is refused")
    other = release_key.backup_public(throwaway_key())
    check(not signing.verify_with(other, data, raw), "another key's signature is refused")
    sig_file = base64.b64encode(raw) + b"\n"
    key = base64.b64encode(public).decode()
    signing.verify(data, sig_file, (base64.b64encode(other).decode(), key))  # the second trusted key signs
    check(True, "verify() accepts a signature from any trusted key")
    for payload, sig, what in ((data + b"x", sig_file, "tampered data"), (data, b"not base64!!", "a damaged .sig"),
                               (data, base64.b64encode(raw[:63]), "a short signature")):  # fmt: skip
        try:
            signing.verify(payload, sig, (key,))
            check(False, f"verify() raises for {what}")
        except signing.SignatureError:
            check(True, f"verify() raises for {what}")
    try:
        signing.verify(data, sig_file, ())
        check(False, "no trusted keys: refused")
    except signing.SignatureError as ex:
        check("no release key" in str(ex), "no trusted keys: refused")
    check(not signing.verify_with(b"\0" * 72, data, raw), "a malformed public key is refused, not a crash")

    copy = REPO / "installer" / "signing.py"
    check(copy.is_file() and copy.read_bytes() == (REPO / "mods" / "bl4_mod_menu" / "signing.py").read_bytes(),
          "installer/signing.py is byte-identical to the Mod Menu's")  # fmt: skip
    check(len(signing.PUBLIC_KEYS) >= 2, f"two release keys pinned (main + backup): {len(signing.PUBLIC_KEYS)}")
    for k in signing.PUBLIC_KEYS:
        blob = base64.b64decode(k)
        check(len(blob) == 72 and blob[:4] == b"ECS1", "pinned key is a P-256 public blob")

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
