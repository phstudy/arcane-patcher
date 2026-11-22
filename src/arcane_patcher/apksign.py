"""Zip alignment and APK signing in pure Python: no Android SDK, JDK, zipalign or apksigner needed.

- zipalign: same layout as `zipalign -f -p 4`.
- sign_apk: APK Signature Scheme v2, plus a v1 (JAR) signature when the APK still runs on Android below 7.0
  (min_sdk < 24). Output passes `apksigner verify --min-sdk-version <min_sdk>`.
- load_keystore: PKCS12 (keytool's default, Android Studio's debug keystore) and JKS keystores.
- create_keystore: a new PKCS12 keystore with an RSA-2048 self-signed key, for users who have none.

Signing is deterministic: the same APK and key give the same bytes (RSA PKCS#1 v1.5, RFC 6979 ECDSA, fixed entry
times for the files it adds).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


class ApkSignError(ValueError):
    """The APK or keystore cannot be used; the message says why."""


# --- zip layout ---------------------------------------------------------------------------------------------------

_LOCAL = struct.Struct("<IHHHHHIIIHH")  # local file header, 30 bytes
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")  # central directory record, 46 bytes
_EOCD = struct.Struct("<IHHHHIIH")  # end of central directory, 22 bytes
_LOCAL_SIG, _CENTRAL_SIG, _EOCD_SIG, _DESCRIPTOR_SIG = 0x04034B50, 0x02014B50, 0x06054B50, 0x08074B50

ALIGNMENT = 4  # every stored entry's data
# `zipalign -p` page-aligns stored .so files to 4 KiB: build-tools 36.1.0's zipalign says "-p: 4kb page-align
# uncompressed .so files" (its separate -P option picks 16 or 64 KiB instead).
SO_PAGE_ALIGNMENT = 4096
_ALIGNMENT_EXTRA_ID = 0xD935  # the extra field apksigner pads with: id, size, u16 alignment, zero bytes
_FIXED_DOS_TIME, _FIXED_DOS_DATE = 0, (1 << 9) | (1 << 5) | 1  # 1981-01-01 00:00 for the entries signing adds


@dataclass
class _Entry:
    name: str
    local_offset: int
    central: bytes  # its raw central directory record
    method: int
    compressed_size: int


def _read_entries(f: BinaryIO) -> tuple[list[_Entry], bytes]:
    """The zip's entries in file order and its archive comment."""
    f.seek(0, os.SEEK_END)
    size = f.tell()
    tail_len = min(size, _EOCD.size + 0xFFFF)
    f.seek(size - tail_len)
    tail = f.read(tail_len)
    pos = len(tail) - _EOCD.size
    while pos >= 0:
        if tail[pos:pos + 4] == b"PK\x05\x06":
            comment_len = _EOCD.unpack_from(tail, pos)[7]
            if pos + _EOCD.size + comment_len == len(tail):
                break
        pos -= 1
    else:
        raise ApkSignError("not a zip file (no end of central directory)")
    _, disk, cd_disk, n_disk, n_total, cd_size, cd_offset, comment_len = _EOCD.unpack_from(tail, pos)
    if disk or cd_disk or n_disk != n_total:
        raise ApkSignError("multi-disk zip files are not supported")
    if n_total == 0xFFFF or cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        raise ApkSignError("zip64 archives are not supported")
    comment = tail[pos + _EOCD.size:]
    f.seek(cd_offset)
    cd = f.read(cd_size)
    entries: list[_Entry] = []
    p = 0
    for _ in range(n_total):
        rec = _CENTRAL.unpack_from(cd, p)
        if rec[0] != _CENTRAL_SIG:
            raise ApkSignError("damaged zip central directory")
        method, csize, usize, nlen, xlen, clen, local_offset = rec[4], rec[8], rec[9], rec[10], rec[11], rec[12], rec[16]
        if 0xFFFFFFFF in (csize, usize, local_offset):
            raise ApkSignError("zip64 archives are not supported")
        end = p + _CENTRAL.size + nlen + xlen + clen
        raw_name = cd[p + _CENTRAL.size:p + _CENTRAL.size + nlen]
        name = raw_name.decode("utf-8" if rec[3] & 0x800 else "cp437")
        entries.append(_Entry(name, local_offset, cd[p:end], method, csize))
        p = end
    entries.sort(key=lambda e: e.local_offset)
    return entries, comment


def _extra_records(extra: bytes) -> list[tuple[int, bytes]]:
    """The extra field's records, without alignment padding (apksigner's record, or zipalign's bare zero bytes)."""
    out = []
    p = 0
    while p + 4 <= len(extra):
        hid, hlen = struct.unpack_from("<HH", extra, p)
        if p + 4 + hlen > len(extra):
            break
        if hid not in (_ALIGNMENT_EXTRA_ID, 0):
            out.append((hid, extra[p + 4:p + 4 + hlen]))
        p += 4 + hlen
    return out


def _alignment(name: str, method: int) -> int:
    if method != 0:
        return 1
    return SO_PAGE_ALIGNMENT if name.endswith(".so") else ALIGNMENT


def _padded_extra(extra: bytes, data_start_without_extra: int, align: int) -> bytes:
    kept = b"".join(struct.pack("<HH", hid, len(v)) + v for hid, v in _extra_records(extra))
    if align == 1:
        return kept
    start = data_start_without_extra + len(kept) + 6
    pad = -start % align
    return kept + struct.pack("<HHH", _ALIGNMENT_EXTRA_ID, 2 + pad, align) + bytes(pad)


class _ZipWriter:
    """Writes entries (copied raw or new) with aligned data; keeps the central directory in memory."""

    def __init__(self, out: BinaryIO):
        self.out = out
        self.central: list[bytes] = []

    def copy(self, f: BinaryIO, e: _Entry, on_data: Callable[[bytes], None] | None = None) -> None:
        f.seek(e.local_offset)
        head = f.read(_LOCAL.size)
        rec = _LOCAL.unpack(head)
        if rec[0] != _LOCAL_SIG:
            raise ApkSignError(f"{e.name}: damaged local header")
        flags, nlen, xlen = rec[2], rec[9], rec[10]
        name = f.read(nlen)
        extra = f.read(xlen)
        data = f.read(e.compressed_size)
        if flags & 0x08:  # a data descriptor follows the data
            sig = f.read(4)
            data += sig + f.read(12 if struct.unpack("<I", sig)[0] == _DESCRIPTOR_SIG else 8)
        if on_data is not None:
            on_data(data[:e.compressed_size])
        offset = self.out.tell()
        extra = _padded_extra(extra, offset + _LOCAL.size + nlen, _alignment(e.name, e.method))
        self.out.write(_LOCAL.pack(*rec[:10], len(extra)) + name + extra + data)
        self.central.append(e.central[:42] + struct.pack("<I", offset) + e.central[46:])

    def add(self, name: str, data: bytes) -> None:
        """A new deflated entry with a fixed time."""
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = co.compress(data) + co.flush()
        raw_name = name.encode()
        crc = zlib.crc32(data)
        offset = self.out.tell()
        self.out.write(_LOCAL.pack(_LOCAL_SIG, 20, 0x800, 8, _FIXED_DOS_TIME, _FIXED_DOS_DATE, crc, len(packed),
                                   len(data), len(raw_name), 0) + raw_name + packed)
        self.central.append(_CENTRAL.pack(_CENTRAL_SIG, 20, 20, 0x800, 8, _FIXED_DOS_TIME, _FIXED_DOS_DATE, crc,
                                          len(packed), len(data), len(raw_name), 0, 0, 0, 0, 0, offset) + raw_name)

    def central_directory(self) -> tuple[bytes, bytes]:
        """(central directory, end of central directory without comment) for a directory written right here."""
        cd = b"".join(self.central)
        n = len(self.central)
        if n >= 0xFFFF or self.out.tell() + len(cd) >= 0xFFFFFFFF:
            raise ApkSignError("the APK is too large for a non-zip64 archive")
        return cd, _EOCD.pack(_EOCD_SIG, 0, 0, n, n, len(cd), self.out.tell(), 0)


def _with_comment(eocd: bytes, comment: bytes) -> bytes:
    return eocd[:-2] + struct.pack("<H", len(comment)) + comment


def zipalign(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` like `zipalign -f -p 4`: stored entries' data 4-byte aligned, stored .so files on 4 KiB
    pages; entry data and compression are unchanged, the padding goes in the local header's extra field."""
    src, dst = Path(src), Path(dst)
    tmp = dst.with_name(dst.name + ".aligning")
    try:
        with open(src, "rb") as f, open(tmp, "wb") as out:
            entries, comment = _read_entries(f)
            w = _ZipWriter(out)
            for e in entries:
                w.copy(f, e)
            cd, eocd = w.central_directory()
            out.write(cd + _with_comment(eocd, comment))
        os.replace(tmp, dst)  # after both are closed: Windows cannot replace an open file
    finally:
        tmp.unlink(missing_ok=True)


# --- keys ---------------------------------------------------------------------------------------------------------

PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey


@dataclass(frozen=True)
class SigningKey:
    """A private key and its certificate chain (signer's certificate first). `name`: the v1 signature's file name
    (META-INF/<name>.RSA / .EC), apksigner's rule applied to the keystore alias."""
    private_key: PrivateKey
    certificates: tuple[x509.Certificate, ...]
    name: str = field(default="CERT")

    def __post_init__(self):
        if not isinstance(self.private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
            raise ApkSignError(f"unsupported key type {type(self.private_key).__name__} (RSA or EC keys only)")
        if not self.certificates:
            raise ApkSignError("the key has no certificate")
        pub = serialization.PublicFormat.SubjectPublicKeyInfo
        if (self.certificates[0].public_key().public_bytes(serialization.Encoding.DER, pub)
                != self.private_key.public_key().public_bytes(serialization.Encoding.DER, pub)):
            raise ApkSignError("the certificate does not belong to the private key")


def signer_name(alias: str) -> str:
    """apksigner's v1 signer name for a key alias: its first 8 characters, upper case, others than A-Z 0-9 _ - as _."""
    name = "".join(c if c.isascii() and (c.isalnum() or c in "_-") else "_" for c in alias[:8].upper())
    return name or "CERT"


_JKS_MAGIC, _JCEKS_MAGIC = 0xFEEDFEED, 0xCECECECE


def load_keystore(path: Path, alias: str, password: str) -> SigningKey:
    """The key `alias` of the PKCS12 or JKS keystore `path` (key password = keystore password, as keytool makes
    them). Aliases match without regard to case, as in keytool."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ApkSignError(f"keystore {path}: {e.strerror}") from None
    magic = struct.unpack(">I", data[:4])[0] if len(data) >= 4 else None
    if magic == _JKS_MAGIC:
        key = _load_jks(data, alias, password, path)
    elif magic == _JCEKS_MAGIC:
        raise ApkSignError(f"keystore {path}: JCEKS keystores are not supported; convert it to PKCS12 with "
                           f"keytool -importkeystore -srckeystore OLD -destkeystore NEW -deststoretype PKCS12")
    elif data[:1] == b"\x30":
        key = _load_pkcs12(data, alias, password, path)
    else:
        raise ApkSignError(f"keystore {path}: unknown format (expected a PKCS12 or JKS keystore)")
    return SigningKey(key[0], key[1], signer_name(alias))


def _alias_missing(path: Path, alias: str, aliases: list[str]) -> ApkSignError:
    listed = ", ".join(sorted(aliases)) if aliases else "none"
    return ApkSignError(f"keystore {path}: no key with alias {alias!r} (its key aliases: {listed})")


def _chain(leaf: x509.Certificate, certs: list[x509.Certificate]) -> tuple[x509.Certificate, ...]:
    chain = [leaf]
    while chain[-1].issuer != chain[-1].subject:
        nxt = next((c for c in certs if c.subject == chain[-1].issuer and c not in chain), None)
        if nxt is None:
            break
        chain.append(nxt)
    return tuple(chain)


def _spki(key) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


# PKCS12: cryptography reads the store (and checks the password) but returns only its first key, so the key bags
# are found here and each decrypted on its own.

_OID_DATA = bytes.fromhex("2a864886f70d010701")  # 1.2.840.113549.1.7.1
_OID_SHROUDED_KEY_BAG = bytes.fromhex("2a864886f70d010c0a0102")  # 1.2.840.113549.1.12.10.1.2
_OID_FRIENDLY_NAME = bytes.fromhex("2a864886f70d010914")  # 1.2.840.113549.1.9.20


def _der(data: bytes, pos: int) -> tuple[int, int, int]:
    """(tag, content start, content end) of the DER element at `pos`."""
    tag = data[pos]
    n = data[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(data[pos:pos + k], "big")
        pos += k
    if pos + n > len(data):
        raise ValueError("truncated DER")
    return tag, pos, pos + n


def _der_children(data: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    """(tag, content start, content end, element start) of each element in data[start:end]."""
    out = []
    while start < end:
        tag, s, e = _der(data, start)
        out.append((tag, s, e, start))
        start = e
    return out


def _pkcs12_key_bags(data: bytes) -> list[tuple[str, bytes]]:
    """(friendly name, EncryptedPrivateKeyInfo DER) of each key bag outside an encrypted content."""
    bags = []
    _, s, e = _der(data, 0)
    pfx = _der_children(data, s, e)
    _, s, e, _ = pfx[1]  # authSafe ContentInfo
    ci = _der_children(data, s, e)
    if data[ci[0][1]:ci[0][2]] != _OID_DATA:
        return []
    _, s, e = _der(data, ci[1][1])  # [0] EXPLICIT OCTET STRING
    _, s, e = _der(data, s)  # AuthenticatedSafe SEQUENCE
    for _, cs, ce, _ in _der_children(data, s, e):
        parts = _der_children(data, cs, ce)
        if data[parts[0][1]:parts[0][2]] != _OID_DATA or len(parts) < 2:
            continue  # encryptedData: certificates in keytool's stores
        _, os_s, _ = _der(data, parts[1][1])
        _, ss, se = _der(data, os_s)  # SafeContents
        for _, bs, be, _ in _der_children(data, ss, se):
            bag = _der_children(data, bs, be)
            if data[bag[0][1]:bag[0][2]] != _OID_SHROUDED_KEY_BAG:
                continue
            value = data[_value_element(data, bag[1][1])]  # [0] EXPLICIT EncryptedPrivateKeyInfo
            name = ""
            if len(bag) > 2:
                for _, as_, ae, _ in _der_children(data, bag[2][1], bag[2][2]):
                    attr = _der_children(data, as_, ae)
                    if data[attr[0][1]:attr[0][2]] == _OID_FRIENDLY_NAME:
                        _, ns, ne = _der(data, attr[1][1])
                        name = data[ns:ne].decode("utf-16-be")
            bags.append((name, value))
    return bags


def _value_element(data: bytes, start: int) -> slice:
    """The whole DER element at `start`."""
    _, _, end = _der(data, start)
    return slice(start, end)


def _load_pkcs12(data: bytes, alias: str, password: str, path: Path) -> tuple[PrivateKey, tuple]:
    try:
        store = pkcs12.load_pkcs12(data, password.encode())
    except ValueError:
        raise ApkSignError(f"keystore {path}: wrong password, or not a keystore") from None
    certs = [c.certificate for c in ([store.cert] if store.cert else []) + list(store.additional_certs)]
    try:
        bags = _pkcs12_key_bags(data)
    except (ValueError, IndexError):
        bags = []
    if not bags:  # keys inside an encrypted content: only the store's first key is reachable
        name = store.cert.friendly_name.decode() if store.cert and store.cert.friendly_name else ""
        if store.key is None or name.lower() != alias.lower():
            raise _alias_missing(path, alias, [name] if store.key is not None and name else [])
        return store.key, _chain(store.cert.certificate, certs)
    for name, encrypted in bags:
        if name.lower() != alias.lower():
            continue
        try:
            key = serialization.load_der_private_key(encrypted, password.encode())
        except (ValueError, TypeError):
            raise ApkSignError(f"keystore {path}: the key {alias!r} has another password than the keystore") from None
        leaf = next((c for c in certs if _spki(c.public_key()) == _spki(key.public_key())), None)
        if leaf is None:
            raise ApkSignError(f"keystore {path}: no certificate for the key {alias!r}")
        return key, _chain(leaf, certs)
    raise _alias_missing(path, alias, [n for n, _ in bags])


# JKS: the Java keystore format. Keys are protected with a SHA-1 keystream (the "JKS key protector"), and the file
# ends with SHA-1(password as UTF-16BE + "Mighty Aphrodite" + everything before it).

_OID_JKS_KEY_PROTECTOR = "1.3.6.1.4.1.42.2.17.1.1"


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ApkSignError("truncated keystore")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def utf(self) -> str:
        return self.take(self.u16()).decode("utf-8", "replace")


def _load_jks(data: bytes, alias: str, password: str, path: Path) -> tuple[PrivateKey, tuple]:
    pw = password.encode("utf-16-be")
    if len(data) < 32 or hashlib.sha1(pw + b"Mighty Aphrodite" + data[:-20]).digest() != data[-20:]:
        raise ApkSignError(f"keystore {path}: wrong password, or a damaged keystore")
    r = _Reader(data[:-20])
    r.take(4)
    if r.u32() not in (1, 2):
        raise ApkSignError(f"keystore {path}: unsupported JKS version")
    version = struct.unpack(">I", data[4:8])[0]
    keys: dict[str, tuple[bytes, list[bytes]]] = {}
    for _ in range(r.u32()):
        tag = r.u32()
        name = r.utf()
        r.take(8)  # creation time
        if tag == 1:  # private key entry
            protected = r.take(r.u32())
            chain = []
            for _ in range(r.u32()):
                if version == 2:
                    r.utf()  # certificate type
                chain.append(r.take(r.u32()))
            keys[name] = (protected, chain)
        elif tag == 2:  # trusted certificate entry
            if version == 2:
                r.utf()
            r.take(r.u32())
        else:
            raise ApkSignError(f"keystore {path}: unsupported JKS entry (tag {tag})")
    found = next((n for n in keys if n.lower() == alias.lower()), None)
    if found is None:
        raise _alias_missing(path, alias, list(keys))
    protected, chain_der = keys[found]
    # EncryptedPrivateKeyInfo { AlgorithmIdentifier { key protector OID }, OCTET STRING }
    _, s, e = _der(protected, 0)
    (_, as_, _, _), (_, os_s, os_e, _) = _der_children(protected, s, e)[:2]
    oid = x509.ObjectIdentifier(_oid_text(protected[_der(protected, as_)[1]:_der(protected, as_)[2]]))
    if oid.dotted_string != _OID_JKS_KEY_PROTECTOR:
        raise ApkSignError(f"keystore {path}: unsupported key protection {oid.dotted_string}")
    blob = protected[os_s:os_e]
    salt, encrypted, check = blob[:20], blob[20:-20], blob[-20:]
    stream = bytearray()
    digest = salt
    while len(stream) < len(encrypted):
        digest = hashlib.sha1(pw + digest).digest()
        stream += digest
    plain = bytes(a ^ b for a, b in zip(encrypted, stream))
    if hashlib.sha1(pw + plain).digest() != check:
        raise ApkSignError(f"keystore {path}: the key {found!r} has another password than the keystore")
    key = serialization.load_der_private_key(plain, None)
    certs = [x509.load_der_x509_certificate(c) for c in chain_der]
    return key, tuple(certs)


def _oid_text(content: bytes) -> str:
    first = content[0]
    parts = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    n = 0
    for b in content[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def _add_years(t: datetime, years: int) -> datetime:
    try:
        return t.replace(year=t.year + years)
    except ValueError:  # 29 February
        return t.replace(year=t.year + years, day=28)


def create_keystore(path: Path, alias: str, password: str, common_name: str = "Android", years: int = 30,
                    not_before: datetime | None = None) -> None:
    """Write a new PKCS12 keystore holding one RSA-2048 key under `alias` with a self-signed certificate valid for
    `years` from `not_before` (default: now). keytool and load_keystore read it with `password`."""
    path = Path(path)
    if path.exists():
        raise ApkSignError(f"keystore {path}: already exists")
    if len(password) < 6:
        raise ApkSignError("the keystore password must have at least 6 characters (keytool's rule)")
    start = (not_before or datetime.now(UTC)).replace(microsecond=0)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start)
            .not_valid_after(_add_years(start, years))
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    encryption = (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(10000)
                  .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC).hmac_hash(hashes.SHA256())
                  .build(password.encode()))
    data = pkcs12.serialize_key_and_certificates(alias.encode(), key, cert, None, encryption)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)


# --- signing ------------------------------------------------------------------------------------------------------

_V2_BLOCK_ID = 0x7109871A
_SIG_RSA_PKCS1_SHA256 = 0x0103
_SIG_ECDSA_SHA256 = 0x0201
_BLOCK_MAGIC = b"APK Sig Block 42"
# Names the certificate of a source stamp in the old signing block; the stamp is gone once the APK is re-signed.
_SOURCE_STAMP_ENTRY = "stamp-cert-sha256"
_CHUNK = 1 << 20
_V1_MIN_SDK = 24  # Android 7.0 verifies v2; below it only v1
_SHA256_MIN_SDK = 18  # v1 signatures with SHA-256 digests


def _lp(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def _sign(key: PrivateKey, data: bytes) -> bytes:
    if isinstance(key, rsa.RSAPrivateKey):
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return key.sign(data, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))


def _algorithm_id(key: PrivateKey) -> int:
    return _SIG_RSA_PKCS1_SHA256 if isinstance(key, rsa.RSAPrivateKey) else _SIG_ECDSA_SHA256


_V1_SIGNATURE_FILE = re.compile(r"META-INF/(MANIFEST\.MF|[^/]+\.(SF|RSA|EC|[D]SA)|SIG-[^/]*)", re.IGNORECASE)


def _is_v1_signature_file(name: str) -> bool:
    """Files of a v1 signature (as apksigner): META-INF/MANIFEST.MF, *.SF, signature blocks, SIG-*."""
    return _V1_SIGNATURE_FILE.fullmatch(name) is not None


def _manifest_attribute(name: str, value: str) -> bytes:
    """`name: value` wrapped at 70 bytes per line, continuation lines starting with one space (as apksigner)."""
    line = f"{name}: {value}".encode()
    out = [line[:70]]
    line = line[70:]
    while line:
        out.append(b" " + line[:69])
        line = line[69:]
    return b"".join(o + b"\r\n" for o in out)


def _v1_files(key: SigningKey, digests: dict[str, bytes]) -> dict[str, bytes]:
    """META-INF/MANIFEST.MF, <name>.SF and the PKCS#7 signature block for entries' SHA-256 digests."""
    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()

    manifest = _manifest_attribute("Manifest-Version", "1.0") + _manifest_attribute("Created-By", "1.0 (Android)")
    manifest += b"\r\n"
    sections = []
    for name in sorted(digests):
        section = _manifest_attribute("Name", name) + _manifest_attribute("SHA-256-Digest", b64(digests[name])) + b"\r\n"
        sections.append((name, section))
        manifest += section
    sf = (_manifest_attribute("Signature-Version", "1.0") + _manifest_attribute("Created-By", "1.0 (Android)")
          + _manifest_attribute("SHA-256-Digest-Manifest", b64(hashlib.sha256(manifest).digest()))
          + _manifest_attribute("X-Android-APK-Signed", "2") + b"\r\n")  # a v2 signature is present too
    for name, section in sections:
        sf += _manifest_attribute("Name", name) + _manifest_attribute("SHA-256-Digest", b64(
            hashlib.sha256(section).digest())) + b"\r\n"
    ext = "RSA" if isinstance(key.private_key, rsa.RSAPrivateKey) else "EC"
    return {"META-INF/MANIFEST.MF": manifest, f"META-INF/{key.name}.SF": sf,
            f"META-INF/{key.name}.{ext}": _pkcs7(key, _sign(key.private_key, sf))}


def _tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        return bytes([tag, n]) + content
    nb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(nb)]) + nb + content


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


def _integer(n: int) -> bytes:
    return _tlv(0x02, n.to_bytes(n.bit_length() // 8 + 1, "big", signed=True))


_NULL = b"\x05\x00"


def _pkcs7(key: SigningKey, signature: bytes) -> bytes:
    """A detached PKCS#7 SignedData without signed attributes, as the v1 scheme uses."""
    def seq(*items: bytes) -> bytes:
        return _tlv(0x30, b"".join(items))

    sha256 = seq(_oid("2.16.840.1.101.3.4.2.1"), _NULL)
    if isinstance(key.private_key, rsa.RSAPrivateKey):
        sig_alg = seq(_oid("1.2.840.113549.1.1.1"), _NULL)  # rsaEncryption
    else:
        sig_alg = seq(_oid("1.2.840.10045.2.1"))  # ecPublicKey, as apksigner writes it
    leaf = key.certificates[0]
    der = serialization.Encoding.DER
    signer_info = seq(_integer(1), seq(leaf.issuer.public_bytes(), _integer(leaf.serial_number)), sha256, sig_alg,
                      _tlv(0x04, signature))
    signed_data = seq(_integer(1), _tlv(0x31, sha256), seq(_oid("1.2.840.113549.1.7.1")),
                      _tlv(0xA0, b"".join(c.public_bytes(der) for c in key.certificates)), _tlv(0x31, signer_info))
    return seq(_oid("1.2.840.113549.1.7.2"), _tlv(0xA0, signed_data))


def _chunked_digest(sections: list[Callable[[], object]]) -> bytes:
    """APK Signature Scheme v2's SHA-256 content digest: 1 MiB chunks of each section, then their digests."""
    chunk_digests = []
    for section in sections:
        for chunk in section():
            chunk_digests.append(hashlib.sha256(b"\xa5" + struct.pack("<I", len(chunk)) + chunk).digest())
    return hashlib.sha256(b"\x5a" + struct.pack("<I", len(chunk_digests)) + b"".join(chunk_digests)).digest()


def _file_chunks(f: BinaryIO, end: int):
    f.seek(0)
    pos = 0
    while pos < end:
        chunk = f.read(min(_CHUNK, end - pos))
        pos += len(chunk)
        yield chunk


def _bytes_chunks(b: bytes):
    for i in range(0, len(b), _CHUNK):
        yield b[i:i + _CHUNK]


def _v2_block(key: SigningKey, digest: bytes) -> bytes:
    der = serialization.Encoding.DER
    algorithm = _algorithm_id(key.private_key)
    signed_data = (_lp(_lp(struct.pack("<I", algorithm) + _lp(digest)))
                   + _lp(b"".join(_lp(c.public_bytes(der)) for c in key.certificates))
                   + _lp(b""))  # no additional attributes
    signatures = _lp(_lp(struct.pack("<I", algorithm) + _lp(_sign(key.private_key, signed_data))))
    signer = _lp(signed_data) + signatures + _lp(_spki(key.private_key.public_key()))
    value = _lp(_lp(signer))
    pairs = struct.pack("<Q", 4 + len(value)) + struct.pack("<I", _V2_BLOCK_ID) + value
    size = struct.pack("<Q", len(pairs) + 8 + len(_BLOCK_MAGIC))
    return size + pairs + size + _BLOCK_MAGIC


def sign_apk(apk: Path, key: SigningKey, min_sdk: int) -> None:
    """Sign `apk` in place with APK Signature Scheme v2, and a v1 (JAR) signature too when `min_sdk` < 24. An earlier
    signature (and source stamp) is replaced; stored entries are (re)aligned like zipalign."""
    apk = Path(apk)
    if min_sdk < _SHA256_MIN_SDK:
        raise ApkSignError(f"minSdkVersion {min_sdk} is not supported (SHA-256 v1 signatures need {_SHA256_MIN_SDK}+)")
    tmp = apk.with_name(apk.name + ".signing")
    try:
        with open(apk, "rb") as f, open(tmp, "w+b") as out:
            entries, comment = _read_entries(f)
            w = _ZipWriter(out)
            digests: dict[str, bytes] = {}
            for e in entries:
                if _is_v1_signature_file(e.name) or e.name == _SOURCE_STAMP_ENTRY:
                    continue
                needs_digest = min_sdk < _V1_MIN_SDK and not e.name.endswith("/")
                on_data = None
                if needs_digest:
                    def on_data(data: bytes, e: _Entry = e) -> None:
                        if e.method == 8:
                            data = zlib.decompress(data, -15)
                        elif e.method != 0:
                            raise ApkSignError(f"{e.name}: unsupported compression method {e.method}")
                        digests[e.name] = hashlib.sha256(data).digest()
                w.copy(f, e, on_data)
            if min_sdk < _V1_MIN_SDK:
                for name, data in _v1_files(key, digests).items():
                    w.add(name, data)
            entries_end = out.tell()
            cd, eocd = w.central_directory()
            eocd = _with_comment(eocd, comment)
            digest = _chunked_digest([lambda: _file_chunks(out, entries_end), lambda: _bytes_chunks(cd),
                                      lambda: _bytes_chunks(eocd)])
            block = _v2_block(key, digest)
            out.seek(entries_end)
            out.write(block + cd + eocd[:16] + struct.pack("<I", entries_end + len(block)) + eocd[20:])
        os.replace(tmp, apk)
    finally:
        tmp.unlink(missing_ok=True)
