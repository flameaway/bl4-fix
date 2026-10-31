"""
Checks that a BL4 Fix release's manifest.json was signed by BL4 Fix's release key, so a release
nobody signed (someone who got into the GitHub account, a swapped file) is refused even though
it's on the right GitHub repository.

tools/release.py signs the exact bytes of manifest.json and publishes the signature next to it as
manifest.json.sig (base64 of the 64-byte ECDSA P-256 signature over SHA-256, r || s). This file
holds the public keys: the main key lives in Windows' key storage on the release PC and can't be
exported; the backup key is a file kept offline, for when the main one is lost
(tools/release_key.py). Either signs a valid release. To replace a key, ship a Mod Menu (signed
with a key it already trusts) that lists the new one.

The check runs through Windows' own crypto (bcrypt.dll), so it needs no package. Plain Python
3.11+, no SDK imports: the setup app has a byte-identical copy (installer/signing.py; dev/
signing_test.py fails when they differ).
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
from ctypes import wintypes

# ECCPUBLICBLOB (BCRYPT_ECCKEY_BLOB: magic "ECS1", 32, X, Y), base64. Main key first.
PUBLIC_KEYS: tuple[str, ...] = (
    "RUNTMSAAAAB+/MPZtyrnkKrApl2O9GTcDOdd5VQAXxuTtbxroUabsDAhQ84Xk98CWxzY9swl9YBYXBJAr2AEKinqOEbKKvaq",  # main, 2026-10-30
    "RUNTMSAAAAC6qGY7FxtbMnFu3cWvVXgcAYE4WbQeyiwFuQGqRVbwAg0HM/ju76+/yMIPIoBHrJMMVVXtN7ppUSw6FuxH608f",  # backup, 2026-10-30
)

SIGNATURE_SUFFIX = ".sig"
SIGNATURE_SIZE = 64
_PUBLIC_MAGIC = 0x31534345  # BCRYPT_ECDSA_PUBLIC_P256_MAGIC
_STATUS_INVALID_SIGNATURE = 0xC000A000


class SignatureError(RuntimeError):
    pass


def _bcrypt() -> ctypes.WinDLL:
    # Own DLL object: shared windll prototypes stay as other code set them
    lib = ctypes.WinDLL("bcrypt")
    handle, ulong = ctypes.c_void_p, wintypes.ULONG
    lib.BCryptOpenAlgorithmProvider.argtypes = (ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong)
    lib.BCryptImportKeyPair.argtypes = (handle, handle, wintypes.LPCWSTR, ctypes.POINTER(handle), ctypes.c_char_p, ulong, ulong)
    lib.BCryptVerifySignature.argtypes = (handle, ctypes.c_void_p, ctypes.c_char_p, ulong, ctypes.c_char_p, ulong, ulong)
    lib.BCryptDestroyKey.argtypes = (handle,)
    lib.BCryptCloseAlgorithmProvider.argtypes = (handle, ulong)
    for name in ("BCryptOpenAlgorithmProvider", "BCryptImportKeyPair", "BCryptVerifySignature", "BCryptDestroyKey",
                 "BCryptCloseAlgorithmProvider"):  # fmt: skip
        getattr(lib, name).restype = ctypes.c_long
    return lib


def _status(value: int) -> int:
    return value & 0xFFFFFFFF


def verify_with(public_key: bytes, data: bytes, signature: bytes) -> bool:
    """Whether signature is a valid ECDSA P-256 signature of SHA-256(data) by this public key blob."""
    if len(signature) != SIGNATURE_SIZE or len(public_key) != 72:  # noqa: PLR2004
        return False
    if int.from_bytes(public_key[:4], "little") != _PUBLIC_MAGIC or int.from_bytes(public_key[4:8], "little") != 32:  # noqa: PLR2004
        return False
    lib = _bcrypt()
    alg, key = ctypes.c_void_p(), ctypes.c_void_p()
    if _status(lib.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "ECDSA_P256", None, 0)) != 0:
        raise SignatureError("Windows' crypto (bcrypt) isn't available")
    try:
        if _status(lib.BCryptImportKeyPair(alg, None, "ECCPUBLICBLOB", ctypes.byref(key), public_key, len(public_key), 0)) != 0:
            return False
        try:
            digest = hashlib.sha256(data).digest()
            status = _status(lib.BCryptVerifySignature(key, None, digest, len(digest), signature, len(signature), 0))
        finally:
            lib.BCryptDestroyKey(key)
    finally:
        lib.BCryptCloseAlgorithmProvider(alg, 0)
    if status not in (0, _STATUS_INVALID_SIGNATURE):
        raise SignatureError(f"Windows' crypto failed to check the signature (0x{status:08X})")
    return status == 0


def parse_signature(text: bytes) -> bytes:
    """The raw 64-byte signature from a .sig file (base64, whitespace ignored)."""
    try:
        raw = base64.b64decode(b"".join(text.split()), validate=True)
    except ValueError as ex:
        raise SignatureError("the signature file is damaged") from ex
    if len(raw) != SIGNATURE_SIZE:
        raise SignatureError("the signature file is damaged")
    return raw


def verify(data: bytes, signature_file: bytes, keys: tuple[str, ...] | None = None) -> None:
    """Raises SignatureError unless signature_file (manifest.json.sig) signs data with a trusted key."""
    keys = PUBLIC_KEYS if keys is None else keys
    if not keys:
        raise SignatureError("this build has no release key to check against")
    signature = parse_signature(signature_file)
    if not any(verify_with(base64.b64decode(k), data, signature) for k in keys):
        raise SignatureError("the release isn't signed by BL4 Fix's release key")
