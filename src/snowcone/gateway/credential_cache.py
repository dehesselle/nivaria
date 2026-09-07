"""Swap in a DPAPI-encrypted file for Windows' Credential Manager keyring.

The driver's own OAuth/SSO/MFA token cache (`ConnectParams.to_connect_kwargs`
sets `client_store_temporary_credential` and, for OAuth,
`oauth_enable_refresh_tokens`) already does exactly what a connection pool
needs: authenticate once, then let every later connection -- the rest of the
pool, or a later session -- reuse the cached credential instead of opening a
fresh browser prompt. On Windows that cache is backed by `keyring`'s
`WinVaultKeyring`, which stores each token as a `CRED_TYPE_GENERIC` Windows
Credential -- capped by the OS at 2560 bytes regardless of persistence type.
A Snowflake ID token or MFA token is a short opaque server-side reference and
fits; an OAuth access or refresh token is a real JWT and routinely does not.
`CredWrite` then fails with a raw Win32 error (1783, "the stub received bad
data" / RPC_X_BAD_STUB_DATA) that neither `keyring`'s Windows backend nor the
Snowflake driver's `KeyringTokenCache` catches -- both only trap
`keyring.errors.KeyringError` -- so it propagates out of `connect()` and the
connect dialog reports it as a failed connection, the opposite of what
enabling the cache was for.

A DPAPI-encrypted flat file has no comparable size limit, and DPAPI ties the
ciphertext to the current Windows user profile the same way Credential
Manager does, so this is not a step down in secrecy, only a different
container. macOS Keychain has no equivalent restriction and Linux already
uses a permissioned file (the driver's own `FileTokenCache`), so neither
platform needs this.
"""

from __future__ import annotations

import base64
import contextlib
import sys

import keyring
import keyring.backend
import keyring.errors

from snowcone.paths import credential_cache_file, read_json, write_json_atomic

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from pathlib import Path

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def _to_blob(data: bytes) -> _DataBlob:
        # The buffer backing pbData must outlive the call, which it does here
        # since it is only ever read by the immediately-following CryptoAPI call.
        buffer = ctypes.create_string_buffer(data, len(data))
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: _DataBlob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            # CryptProtectData/CryptUnprotectData allocate pbData with
            # LocalAlloc; only LocalFree, not our own allocator, may release it.
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def _protect(data: bytes) -> bytes:
        """Encrypt for the current Windows user via DPAPI (no extra secret
        to manage -- the OS ties the ciphertext to the logged-in profile)."""
        blob_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(_to_blob(data)),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        return _from_blob(blob_out)

    def _unprotect(data: bytes) -> bytes:
        blob_out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_to_blob(data)),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise ctypes.WinError()
        return _from_blob(blob_out)

    class DpapiFileKeyring(keyring.backend.KeyringBackend):
        """One JSON file of DPAPI-encrypted secrets, keyed like Credential
        Manager is (service, username), but with no 2560-byte ceiling."""

        priority = 10  # only ever installed explicitly via set_keyring(); never auto-discovered

        def __init__(self, path: Path | None = None) -> None:
            super().__init__()
            self._path = path

        @property
        def _cache_file(self) -> Path:
            # Resolved lazily, like ProfileStore.path, so a test that
            # constructs this before pointing paths elsewhere still sees it.
            return self._path if self._path is not None else credential_cache_file()

        @staticmethod
        def _key(service: str, username: str) -> str:
            # \x1f (unit separator) cannot appear in a keyring service or
            # username, so this can never collide two distinct pairs.
            return f"{service}\x1f{username}"

        def get_password(self, service: str, username: str) -> str | None:
            entries = read_json(self._cache_file, {})
            encoded = entries.get(self._key(service, username))
            if encoded is None:
                return None
            try:
                return _unprotect(base64.b64decode(encoded)).decode("utf-8")
            except (ValueError, OSError):
                # Undecodable (moved to a different Windows profile, a
                # corrupt file) is the same as "not cached", not a crash.
                return None

        def set_password(self, service: str, username: str, password: str) -> None:
            entries = read_json(self._cache_file, {})
            entries[self._key(service, username)] = base64.b64encode(
                _protect(password.encode("utf-8"))
            ).decode("ascii")
            write_json_atomic(self._cache_file, entries)

        def delete_password(self, service: str, username: str) -> None:
            entries = read_json(self._cache_file, {})
            key = self._key(service, username)
            if key not in entries:
                raise keyring.errors.PasswordDeleteError(key)
            del entries[key]
            write_json_atomic(self._cache_file, entries)


def install() -> None:
    """Make `DpapiFileKeyring` the active `keyring` backend, once per process.

    Windows-only and never raises: any failure here (an already-customised
    keyring, a DPAPI call that is refused) just leaves whatever backend the
    driver would otherwise have picked. The token cache is always an
    optimisation on top of a working connection, never a dependency for
    making one.
    """
    if sys.platform != "win32":
        return
    if isinstance(keyring.get_keyring(), DpapiFileKeyring):
        return
    with contextlib.suppress(Exception):
        keyring.set_keyring(DpapiFileKeyring())
