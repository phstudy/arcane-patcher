"""signing.py: the user's keystore signs every build; its password comes from the environment, never a command
line; keystore problems are clear errors before any build work."""

import zipfile
from datetime import UTC, datetime

import pytest

from arcane_patcher import apksign, signing

SECRET = "s3cret-Pass_word"
START = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def keystore(tmp_path_factory):
    path = tmp_path_factory.mktemp("ks") / "my.keystore"
    apksign.create_keystore(path, "me", SECRET, not_before=START)
    return path


def test_missing_password_is_a_clear_error(tmp_path):
    keystore = tmp_path / "my.keystore"
    keystore.write_bytes(b"ks")
    with pytest.raises(signing.SigningError, match="set ARCANE_KS_PASS"):
        signing.key_from_env(keystore, "me", {})
    assert signing.key_from_env(keystore, signing.DEBUG_ALIAS, {}).password == "android"  # Android's debug key
    assert signing.key_from_env(keystore, "me", {signing.PASSWORD_ENV: SECRET}).password == SECRET
    with pytest.raises(signing.SigningError, match="not found"):
        signing.key_from_env(tmp_path / "missing.keystore", "me", {signing.PASSWORD_ENV: SECRET})


def test_keystore_problems_stop_before_signing(keystore):
    with pytest.raises(signing.SigningError, match="wrong password"):
        signing.KeystoreSigner(signing.key_from_env(keystore, "me", {signing.PASSWORD_ENV: "wrong-one"}))
    with pytest.raises(signing.SigningError, match=r"no key with alias 'you' \(its key aliases: me\)"):
        signing.KeystoreSigner(signing.key_from_env(keystore, "you", {signing.PASSWORD_ENV: SECRET}))


@pytest.mark.parametrize("min_sdk", [21, 24])
def test_signs_with_the_users_key(tmp_path, keystore, min_sdk):
    apk = tmp_path / "x.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", b"m")
        z.writestr("classes.dex", b"dex\n", zipfile.ZIP_DEFLATED)
    signer = signing.KeystoreSigner(signing.key_from_env(keystore, "me", {signing.PASSWORD_ENV: SECRET}))
    signer.sign(apk, min_sdk)
    assert b"APK Sig Block 42" in apk.read_bytes()
    with zipfile.ZipFile(apk) as z:
        assert ("META-INF/ME.RSA" in z.namelist()) == (min_sdk < 24)


def test_sign_error_is_a_signing_error(tmp_path, keystore):
    bad = tmp_path / "bad.apk"
    bad.write_bytes(b"not a zip")
    signer = signing.KeystoreSigner(signing.key_from_env(keystore, "me", {signing.PASSWORD_ENV: SECRET}))
    with pytest.raises(signing.SigningError, match="not a zip"):
        signer.sign(bad, 24)
