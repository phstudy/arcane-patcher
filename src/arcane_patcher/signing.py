"""Signing with the user's own keystore (PKCS12 or JKS), in pure Python (apksign.py). The password comes from an
environment variable, never a command line. No key is shipped: every build is signed with the keystore the user
passes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from arcane_patcher import apksign

PASSWORD_ENV = "ARCANE_KS_PASS"
DEBUG_ALIAS = "androiddebugkey"  # Android's debug keystore: password "android"


class SigningError(SystemExit):
    """A signing setup problem, reported as a clear message."""


@dataclass(frozen=True)
class Key:
    keystore: Path
    alias: str
    password: str


def key_from_env(keystore: Path, alias: str, env: dict[str, str] | None = None) -> Key:
    """The key `alias` of `keystore`, its password from ARCANE_KS_PASS ("android" for the debug alias when unset)."""
    env = os.environ if env is None else env
    password = env.get(PASSWORD_ENV) or ("android" if alias == DEBUG_ALIAS else None)
    if password is None:
        raise SigningError(f"--keystore: set {PASSWORD_ENV} to the keystore's password")
    if not Path(keystore).is_file():
        raise SigningError(f"keystore {keystore}: not found")
    return Key(Path(keystore), alias, password)


class KeystoreSigner:
    """Signs APKs in place with one key. The keystore is read (and its password checked) when the signer is made, so
    a wrong password or alias stops a build before any work."""

    def __init__(self, key: Key):
        if not key.keystore.is_file():
            raise SigningError(f"keystore {key.keystore}: not found")
        self.key = key
        try:
            self.signing_key = apksign.load_keystore(key.keystore, key.alias, key.password)
        except apksign.ApkSignError as e:
            raise SigningError(str(e)) from None

    def sign(self, apk: Path, min_sdk: int) -> None:
        """Sign `apk` in place: APK Signature Scheme v2, plus v1 when `min_sdk` < 24."""
        try:
            apksign.sign_apk(apk, self.signing_key, min_sdk)
        except apksign.ApkSignError as e:
            raise SigningError(f"signing {apk}: {e}") from None


def load_key(key: Key) -> None:
    """Read the key (checks the password and alias); SigningError when it cannot be used."""
    KeystoreSigner(key)
