"""apksign.py: zip alignment, v1 + v2 signing and keystores in pure Python. The SDK's zipalign / apksigner and the
JDK's keytool, when installed, cross-check the results; the pure-Python checks always run."""

import os
import re
import shutil
import struct
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import APK, needs_apk
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from arcane_patcher import apksign

START = datetime(2020, 1, 1, tzinfo=UTC)  # fixed validity start: nothing depends on today's date
PASSWORD = "test-pass"


def _sdk_tool(name: str) -> Path | None:
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             str(Path.home() / "Library/Android/sdk"), str(Path.home() / "Android/Sdk")]
    for root in filter(None, roots):
        bt = Path(root) / "build-tools"
        if bt.is_dir():
            versions = sorted((v for v in bt.iterdir() if (v / name).exists()),
                              key=lambda v: [int(p) if p.isdigit() else 0 for p in v.name.split(".")])
            if versions:
                return versions[-1] / name
    return None


APKSIGNER = _sdk_tool("apksigner")
ZIPALIGN = _sdk_tool("zipalign")
KEYTOOL = shutil.which("keytool")
needs_apksigner = pytest.mark.skipif(APKSIGNER is None or KEYTOOL is None, reason="apksigner (and a JDK) not found")
needs_zipalign = pytest.mark.skipif(ZIPALIGN is None, reason="zipalign not found")
needs_keytool = pytest.mark.skipif(KEYTOOL is None, reason="keytool not found")


def _cert(key, cn: str) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(12345).not_valid_before(START).not_valid_after(START + timedelta(days=3650))
            .sign(key, hashes.SHA256()))


@pytest.fixture(scope="module")
def rsa_key() -> apksign.SigningKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return apksign.SigningKey(key, (_cert(key, "Test RSA"),), "TESTRSA")


@pytest.fixture(scope="module")
def ec_key() -> apksign.SigningKey:
    key = ec.generate_private_key(ec.SECP256R1())
    return apksign.SigningKey(key, (_cert(key, "Test EC"),), "TESTEC")


def _make_apk(path: Path) -> Path:
    """A minimal APK-like zip: deflated and stored entries, a stored .so, a directory and an old v1 signature."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00" + bytes(2000), zipfile.ZIP_DEFLATED)
        z.writestr("classes.dex", b"dex\n035\x00" + bytes(range(256)) * 20, zipfile.ZIP_DEFLATED)
        z.writestr("resources.arsc", b"\x02\x00\x0c\x00" + bytes(123), zipfile.ZIP_STORED)
        z.writestr("assets/", b"", zipfile.ZIP_STORED)
        z.writestr("assets/odd.bin", b"abc", zipfile.ZIP_STORED)
        z.writestr("lib/arm64-v8a/libgame.so", b"\x7fELF" + bytes(9000), zipfile.ZIP_STORED)
        z.writestr("res/raw/" + "long_name_" * 12 + ".txt", b"wrapped manifest line", zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/services/x", b"kept", zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/MANIFEST.MF", b"old")
        z.writestr("META-INF/OLD.SF", b"old")
        z.writestr("META-INF/OLD.RSA", b"old")
        z.writestr("stamp-cert-sha256", bytes(32))
        z.comment = b"comment kept"
    return path


def _check_aligned(apk: Path) -> None:
    """Every stored entry's data starts on 4 bytes, a stored .so on 4096."""
    with zipfile.ZipFile(apk) as z, open(apk, "rb") as f:
        for info in z.infolist():
            f.seek(info.header_offset)
            nlen, xlen = struct.unpack("<HH", f.read(30)[26:30])
            start = info.header_offset + 30 + nlen + xlen
            if info.compress_type == zipfile.ZIP_STORED:
                align = 4096 if info.filename.endswith(".so") else 4
                assert start % align == 0, (info.filename, start)


def _contents(apk: Path) -> dict[str, tuple[int, bytes]]:
    with zipfile.ZipFile(apk) as z:
        return {i.filename: (i.compress_type, z.read(i)) for i in z.infolist()}


# what newer JDKs print about the tool itself (native access of its TLS library), not about the APK
JVM_NOTICE = re.compile(r"WARNING: (A restricted method in |java\.lang\.System::|Use --enable-native-access|"
                        r"Restricted methods will be blocked)")


def _verify(apk: Path, min_sdk: int) -> str:
    r = subprocess.run([str(APKSIGNER), "verify", "--verbose", "--min-sdk-version", str(min_sdk), str(apk)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    # apksigner warns about every META-INF/ entry left by the build (old Android's v1 check skips that folder);
    # it says the same about the APKs it signs itself. Any other warning is a failure.
    warnings = [line for line in (r.stdout + r.stderr).splitlines() if "WARNING" in line
                and not re.fullmatch(r"WARNING: META-INF/\S+ not protected by signature\..*", line)
                and not JVM_NOTICE.match(line)]
    assert not warnings, r.stdout + r.stderr
    return r.stdout


def test_zipalign_aligns_and_keeps_entries(tmp_path):
    src = _make_apk(tmp_path / "in.apk")
    # misalign on purpose: a prefix of odd length shifts every entry
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("zz/stored", b"12345", zipfile.ZIP_STORED)
    out = tmp_path / "out.apk"
    apksign.zipalign(src, out)
    _check_aligned(out)
    assert _contents(out) == _contents(src)
    with zipfile.ZipFile(out) as z:
        assert z.comment == b"comment kept"
        assert z.testzip() is None
    again = tmp_path / "again.apk"
    apksign.zipalign(out, again)  # aligning twice replaces the padding instead of stacking it
    assert again.read_bytes() == out.read_bytes()


@needs_zipalign
def test_zipalign_passes_sdk_check(tmp_path):
    out = tmp_path / "out.apk"
    apksign.zipalign(_make_apk(tmp_path / "in.apk"), out)
    r = subprocess.run([str(ZIPALIGN), "-c", "-p", "-v", "4", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("min_sdk", [21, 24])
@pytest.mark.parametrize("which", ["rsa_key", "ec_key"])
def test_sign_is_deterministic_and_complete(tmp_path, request, which, min_sdk):
    key = request.getfixturevalue(which)
    src = _make_apk(tmp_path / "in.apk")
    a, b = tmp_path / "a.apk", tmp_path / "b.apk"
    for p in (a, b):
        apksign.zipalign(src, p)
        apksign.sign_apk(p, key, min_sdk)
    assert a.read_bytes() == b.read_bytes()
    _check_aligned(a)
    data = a.read_bytes()
    assert b"APK Sig Block 42" in data
    contents = _contents(a)
    assert not {"META-INF/OLD.SF", "META-INF/OLD.RSA", "stamp-cert-sha256"} & set(contents)
    original = {n: v for n, v in _contents(src).items()
                if not n.startswith(("META-INF/MANIFEST", "META-INF/OLD", "stamp-cert"))}
    assert {n: v for n, v in contents.items() if n in original} == original
    ext = "RSA" if which == "rsa_key" else "EC"
    v1 = {"META-INF/MANIFEST.MF", f"META-INF/{key.name}.SF", f"META-INF/{key.name}.{ext}"}
    assert v1 <= set(contents) if min_sdk < 24 else not v1 & set(contents)
    # signing again replaces the signature rather than adding one
    apksign.sign_apk(a, key, min_sdk)
    assert a.read_bytes() == b.read_bytes()


@needs_apksigner
@pytest.mark.parametrize("min_sdk", [21, 24])
@pytest.mark.parametrize("which", ["rsa_key", "ec_key"])
def test_sign_passes_apksigner(tmp_path, request, which, min_sdk):
    apk = tmp_path / "s.apk"
    apksign.zipalign(_make_apk(tmp_path / "in.apk"), apk)
    apksign.sign_apk(apk, request.getfixturevalue(which), min_sdk)
    out = _verify(apk, min_sdk)
    assert f"Verified using v1 scheme (JAR signing): {'true' if min_sdk < 24 else 'false'}" in out
    assert "Verified using v2 scheme (APK Signature Scheme v2): true" in out
    if ZIPALIGN:
        assert subprocess.run([str(ZIPALIGN), "-c", "-p", "4", str(apk)], capture_output=True).returncode == 0


def test_sign_refuses_too_old_min_sdk(tmp_path, rsa_key):
    with pytest.raises(apksign.ApkSignError, match="minSdkVersion 17"):
        apksign.sign_apk(_make_apk(tmp_path / "in.apk"), rsa_key, 17)


def test_signing_key_checks_its_certificate(rsa_key, ec_key):
    with pytest.raises(apksign.ApkSignError, match="does not belong"):
        apksign.SigningKey(rsa_key.private_key, ec_key.certificates)
    assert apksign.signer_name("androiddebugkey") == "ANDROIDD"
    assert apksign.signer_name("my key.1") == "MY_KEY_1"


def test_create_keystore_round_trip(tmp_path):
    ks = tmp_path / "new.keystore"
    apksign.create_keystore(ks, "upload", PASSWORD, common_name="Me", years=30, not_before=START)
    key = apksign.load_keystore(ks, "Upload", PASSWORD)  # aliases match without regard to case
    assert isinstance(key.private_key, rsa.RSAPrivateKey) and key.private_key.key_size == 2048
    cert = key.certificates[0]
    assert cert.subject == cert.issuer and cert.subject.rfc4514_string() == "CN=Me"
    assert cert.not_valid_before_utc == START and cert.not_valid_after_utc.year == 2050
    assert key.name == "UPLOAD"
    with pytest.raises(apksign.ApkSignError, match="already exists"):
        apksign.create_keystore(ks, "upload", PASSWORD)
    with pytest.raises(apksign.ApkSignError, match="wrong password"):
        apksign.load_keystore(ks, "upload", "not-the-password")
    with pytest.raises(apksign.ApkSignError, match=r"no key with alias 'other' \(its key aliases: upload\)"):
        apksign.load_keystore(ks, "other", PASSWORD)


def test_unknown_keystore_format(tmp_path):
    ks = tmp_path / "x.keystore"
    ks.write_bytes(b"not a keystore")
    with pytest.raises(apksign.ApkSignError, match="unknown format"):
        apksign.load_keystore(ks, "a", PASSWORD)
    with pytest.raises(apksign.ApkSignError, match="No such file"):
        apksign.load_keystore(tmp_path / "missing", "a", PASSWORD)


@needs_keytool
def test_keytool_reads_created_keystore(tmp_path):
    ks = tmp_path / "new.keystore"
    apksign.create_keystore(ks, "upload", PASSWORD, not_before=START)
    r = subprocess.run([KEYTOOL, "-list", "-keystore", str(ks), "-storepass", PASSWORD], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "upload," in r.stdout and "PrivateKeyEntry" in r.stdout


def _keytool_key(ks: Path, alias: str, storetype: str, keyalg: str = "RSA") -> None:
    subprocess.run([KEYTOOL, "-genkeypair", "-storetype", storetype, "-keystore", str(ks), "-storepass", PASSWORD,
                    "-keypass", PASSWORD, "-alias", alias, "-keyalg", keyalg, "-dname", f"CN={alias}",
                    "-startdate", "2020/01/01 00:00:00", "-validity", "3650"], check=True, capture_output=True)


@needs_keytool
def test_jks_keystore(tmp_path):
    ks = tmp_path / "old.jks"
    _keytool_key(ks, "first", "JKS")
    _keytool_key(ks, "second", "JKS", "EC")
    first = apksign.load_keystore(ks, "first", PASSWORD)
    second = apksign.load_keystore(ks, "SECOND", PASSWORD)
    assert isinstance(first.private_key, rsa.RSAPrivateKey)
    assert isinstance(second.private_key, ec.EllipticCurvePrivateKey)
    assert second.certificates[0].subject.rfc4514_string() == "CN=second"
    with pytest.raises(apksign.ApkSignError, match="wrong password"):
        apksign.load_keystore(ks, "first", "not-the-password")
    with pytest.raises(apksign.ApkSignError, match=r"its key aliases: first, second"):
        apksign.load_keystore(ks, "third", PASSWORD)


@needs_keytool
def test_pkcs12_with_several_keys(tmp_path):
    """cryptography alone returns only a PKCS12 store's first key; every alias must be reachable."""
    ks = tmp_path / "multi.p12"
    for alias, alg in (("one", "RSA"), ("two", "EC"), ("three", "RSA")):
        _keytool_key(ks, alias, "PKCS12", alg)
    for alias in ("one", "two", "three"):
        assert apksign.load_keystore(ks, alias, PASSWORD).certificates[0].subject.rfc4514_string() == f"CN={alias}"
    with pytest.raises(apksign.ApkSignError, match=r"its key aliases: one, three, two"):
        apksign.load_keystore(ks, "four", PASSWORD)


@needs_apksigner
def test_jks_key_signs(tmp_path):
    ks = tmp_path / "old.jks"
    _keytool_key(ks, "mykey", "JKS")
    apk = tmp_path / "s.apk"
    apksign.zipalign(_make_apk(tmp_path / "in.apk"), apk)
    apksign.sign_apk(apk, apksign.load_keystore(ks, "mykey", PASSWORD), 21)
    _verify(apk, 21)


@needs_apk
@needs_apksigner
def test_real_apk_signs(tmp_path, rsa_key):
    apk = tmp_path / "real.apk"
    apksign.zipalign(APK, apk)
    apksign.sign_apk(apk, rsa_key, 21)
    out = _verify(apk, 21)
    assert "Verified using v1 scheme (JAR signing): true" in out
    assert "Verified using v2 scheme (APK Signature Scheme v2): true" in out
    if ZIPALIGN:
        r = subprocess.run([str(ZIPALIGN), "-c", "-p", "4", str(apk)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
