"""
BL4 Fix's release signing keys (ECDSA P-256), through Windows' own crypto: no package needed.

- The main key is made in Windows' key storage for this user (Microsoft Software Key Storage
  Provider, name KEY_NAME) and can't be exported: it never exists as a file, so it can't be copied
  off this PC. Signing with it works only here, as this Windows user.
- The backup key is written to a file once (create). Move that file somewhere offline (a USB stick)
  and delete it from the PC: it's for signing the release that brings a new main key if this PC
  or its Windows account is ever lost.

The Mod Menu and the setup app trust both public keys (signing.PUBLIC_KEYS in
mods/bl4_mod_menu/signing.py and its copy installer/signing.py).

    python tools/release_key.py create [--backup FILE]   # once: both keys, prints the public keys
    python tools/release_key.py public                     # the main key's public key
    python tools/release_key.py sign FILE [--backup FILE]  # writes FILE.sig (main key, or the backup)
    python tools/release_key.py check                      # signs and verifies a test string

tools/release.py signs manifest.json with sign_file().
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import sys
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "mods" / "bl4_mod_menu"))
import signing  # noqa: E402  # the verifier the Mod Menu uses

KEY_NAME = "BL4 Fix release signing"
PROVIDER = "Microsoft Software Key Storage Provider"
NTE_BAD_KEYSET = 0x80090016
handle, ulong = ctypes.c_void_p, wintypes.ULONG


def _libs() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    nc, bc = ctypes.WinDLL("ncrypt"), ctypes.WinDLL("bcrypt")
    nc.NCryptOpenStorageProvider.argtypes = (ctypes.POINTER(handle), wintypes.LPCWSTR, ulong)
    nc.NCryptOpenKey.argtypes = (handle, ctypes.POINTER(handle), wintypes.LPCWSTR, ulong, ulong)
    nc.NCryptCreatePersistedKey.argtypes = (handle, ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong, ulong)
    nc.NCryptFinalizeKey.argtypes = (handle, ulong)
    nc.NCryptSignHash.argtypes = (handle, ctypes.c_void_p, ctypes.c_char_p, ulong, ctypes.c_char_p, ulong, ctypes.POINTER(ulong), ulong)
    nc.NCryptExportKey.argtypes = (handle, handle, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_char_p, ulong, ctypes.POINTER(ulong), ulong)
    nc.NCryptFreeObject.argtypes = (handle,)
    bc.BCryptOpenAlgorithmProvider.argtypes = (ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong)
    bc.BCryptGenerateKeyPair.argtypes = (handle, ctypes.POINTER(handle), ulong, ulong)
    bc.BCryptFinalizeKeyPair.argtypes = (handle, ulong)
    bc.BCryptExportKey.argtypes = (handle, handle, wintypes.LPCWSTR, ctypes.c_char_p, ulong, ctypes.POINTER(ulong), ulong)
    bc.BCryptImportKeyPair.argtypes = (handle, handle, wintypes.LPCWSTR, ctypes.POINTER(handle), ctypes.c_char_p, ulong, ulong)
    bc.BCryptSignHash.argtypes = (handle, ctypes.c_void_p, ctypes.c_char_p, ulong, ctypes.c_char_p, ulong, ctypes.POINTER(ulong), ulong)
    bc.BCryptDestroyKey.argtypes = (handle,)
    bc.BCryptCloseAlgorithmProvider.argtypes = (handle, ulong)
    for fn in (nc.NCryptOpenStorageProvider, nc.NCryptOpenKey, nc.NCryptCreatePersistedKey, nc.NCryptFinalizeKey,
               nc.NCryptSignHash, nc.NCryptExportKey, nc.NCryptFreeObject, bc.BCryptOpenAlgorithmProvider,
               bc.BCryptGenerateKeyPair, bc.BCryptFinalizeKeyPair, bc.BCryptExportKey, bc.BCryptImportKeyPair,
               bc.BCryptSignHash, bc.BCryptDestroyKey, bc.BCryptCloseAlgorithmProvider):  # fmt: skip
        fn.restype = ctypes.c_long
    return nc, bc


def _ok(status: int, what: str) -> None:
    if status & 0xFFFFFFFF:
        raise RuntimeError(f"{what} failed (0x{status & 0xFFFFFFFF:08X})")


def _provider(nc: ctypes.WinDLL) -> ctypes.c_void_p:
    prov = handle()
    _ok(nc.NCryptOpenStorageProvider(ctypes.byref(prov), PROVIDER, 0), "opening Windows' key storage")
    return prov


def _main_key(nc: ctypes.WinDLL, prov: ctypes.c_void_p) -> ctypes.c_void_p | None:
    key = handle()
    status = nc.NCryptOpenKey(prov, ctypes.byref(key), KEY_NAME, 0, 0) & 0xFFFFFFFF
    if status == NTE_BAD_KEYSET:
        return None
    _ok(status, "opening the release key")
    return key


def _export_public_nc(nc: ctypes.WinDLL, key: ctypes.c_void_p) -> bytes:
    size = ulong()
    _ok(nc.NCryptExportKey(key, None, "ECCPUBLICBLOB", None, None, 0, ctypes.byref(size), 0), "reading the public key")
    out = ctypes.create_string_buffer(size.value)
    _ok(nc.NCryptExportKey(key, None, "ECCPUBLICBLOB", None, out, size, ctypes.byref(size), 0), "reading the public key")
    return out.raw[: size.value]


def main_public() -> bytes:
    nc, _ = _libs()
    prov = _provider(nc)
    try:
        key = _main_key(nc, prov)
        if key is None:
            raise RuntimeError(f"no release key on this PC for this Windows user: run `python {Path(__file__).name} create` once")
        try:
            return _export_public_nc(nc, key)
        finally:
            nc.NCryptFreeObject(key)
    finally:
        nc.NCryptFreeObject(prov)


def _sign_main(digest: bytes) -> bytes:
    nc, _ = _libs()
    prov = _provider(nc)
    try:
        key = _main_key(nc, prov)
        if key is None:
            raise RuntimeError(f"no release key on this PC for this Windows user: run `python {Path(__file__).name} create` once")
        try:
            sig, size = ctypes.create_string_buffer(64), ulong()
            _ok(nc.NCryptSignHash(key, None, digest, len(digest), sig, 64, ctypes.byref(size), 0), "signing")
            return sig.raw[: size.value]
        finally:
            nc.NCryptFreeObject(key)
    finally:
        nc.NCryptFreeObject(prov)


def _with_backup(blob: bytes, fn):  # noqa: ANN001, ANN202
    _, bc = _libs()
    alg, key = handle(), handle()
    _ok(bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "ECDSA_P256", None, 0), "opening ECDSA")
    try:
        _ok(bc.BCryptImportKeyPair(alg, None, "ECCPRIVATEBLOB", ctypes.byref(key), blob, len(blob), 0), "reading the backup key")
        try:
            return fn(bc, key)
        finally:
            bc.BCryptDestroyKey(key)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)


def _export_bc(bc: ctypes.WinDLL, key: ctypes.c_void_p, kind: str) -> bytes:
    size = ulong()
    _ok(bc.BCryptExportKey(key, None, kind, None, 0, ctypes.byref(size), 0), "exporting a key")
    out = ctypes.create_string_buffer(size.value)
    _ok(bc.BCryptExportKey(key, None, kind, out, size, ctypes.byref(size), 0), "exporting a key")
    return out.raw[: size.value]


def _sign_backup(blob: bytes, digest: bytes) -> bytes:
    def sign(bc: ctypes.WinDLL, key: ctypes.c_void_p) -> bytes:
        sig, size = ctypes.create_string_buffer(64), ulong()
        _ok(bc.BCryptSignHash(key, None, digest, len(digest), sig, 64, ctypes.byref(size), 0), "signing")
        return sig.raw[: size.value]

    return _with_backup(blob, sign)


def backup_public(blob: bytes) -> bytes:
    return _with_backup(blob, lambda bc, key: _export_bc(bc, key, "ECCPUBLICBLOB"))


def sign_bytes(data: bytes, backup: Path | None = None) -> bytes:
    """The .sig file's contents (base64 + newline) for data."""
    digest = hashlib.sha256(data).digest()
    raw = _sign_backup(backup.read_bytes(), digest) if backup else _sign_main(digest)
    public = backup_public(backup.read_bytes()) if backup else main_public()
    if not signing.verify_with(public, data, raw):
        raise RuntimeError("the new signature doesn't verify: nothing written")
    trusted = [base64.b64decode(k) for k in signing.PUBLIC_KEYS]
    if public not in trusted:
        raise RuntimeError(
            "this key isn't in signing.PUBLIC_KEYS (mods/bl4_mod_menu/signing.py): players' Mod Menus would refuse the"
            " release. Add its public key (`release_key.py public`) there and in installer/signing.py first."
        )
    return base64.b64encode(raw) + b"\n"


def sign_file(path: Path, backup: Path | None = None) -> Path:
    out = path.with_name(path.name + signing.SIGNATURE_SUFFIX)
    out.write_bytes(sign_bytes(path.read_bytes(), backup))
    return out


def create(backup_path: Path) -> None:
    nc, bc = _libs()
    prov = _provider(nc)
    try:
        existing = _main_key(nc, prov)
        if existing is not None:
            nc.NCryptFreeObject(existing)
            raise RuntimeError("a release key exists already on this PC (delete it in Windows' key storage first if you really mean to replace it)")
        key = handle()
        _ok(nc.NCryptCreatePersistedKey(prov, ctypes.byref(key), "ECDSA_P256", KEY_NAME, 0, 0), "creating the release key")
        try:
            # No export policy set: the private key can't be exported
            _ok(nc.NCryptFinalizeKey(key, 0), "creating the release key")
            main = _export_public_nc(nc, key)
        finally:
            nc.NCryptFreeObject(key)
    finally:
        nc.NCryptFreeObject(prov)
    if backup_path.exists():
        raise RuntimeError(f"{backup_path} exists already: not overwritten")
    alg, bkey = handle(), handle()
    _ok(bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "ECDSA_P256", None, 0), "opening ECDSA")
    try:
        _ok(bc.BCryptGenerateKeyPair(alg, ctypes.byref(bkey), 256, 0), "making the backup key")
        _ok(bc.BCryptFinalizeKeyPair(bkey, 0), "making the backup key")
        try:
            private, backup = _export_bc(bc, bkey, "ECCPRIVATEBLOB"), _export_bc(bc, bkey, "ECCPUBLICBLOB")
        finally:
            bc.BCryptDestroyKey(bkey)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_bytes(private)
    print("Main key (in Windows' key storage, not exportable):")
    print(f'    "{base64.b64encode(main).decode()}",')
    print(f"Backup key (private part in {backup_path}: move it offline and delete it here):")
    print(f'    "{base64.b64encode(backup).decode()}",')
    print("Put both lines in PUBLIC_KEYS in mods/bl4_mod_menu/signing.py and installer/signing.py.")


def check() -> None:
    data = b"BL4 Fix release key check"
    raw = base64.b64decode(sign_bytes(data))
    print("signed and verified with the main key:", signing.verify_with(main_public(), data, raw))


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--backup", type=Path, default=Path.home() / "Documents" / "BL4Fix-release-backup-key.bin")
    sub.add_parser("public")
    s = sub.add_parser("sign")
    s.add_argument("file", type=Path)
    s.add_argument("--backup", type=Path)
    sub.add_parser("check")
    args = parser.parse_args()
    try:
        if args.cmd == "create":
            create(args.backup)
        elif args.cmd == "public":
            print(base64.b64encode(main_public()).decode())
        elif args.cmd == "sign":
            print(sign_file(args.file, args.backup))
        elif args.cmd == "check":
            check()
    except RuntimeError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
