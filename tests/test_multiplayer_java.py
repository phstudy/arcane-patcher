"""The multiplayer variant's Java side on a desktop JVM (android/multiplayer/java: ServerUrl, LoopbackProxy, JoinLink
and QrDecoder use no Android classes): the server address the player types, scans or opens as a connect link, and the
loopback proxy the game talks to (Host rewritten to the chosen server; a websocket upgrade passes through). Needs a
JDK; the QR decoding test also needs ZXing in the cache (no download here)."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

from conftest import FRAGMENTS

from arcane_patcher import kit, offline

ROOT = Path(__file__).resolve().parents[1]
JAVA_SRC = kit.ANDROID_DIR / "multiplayer" / "java" / "arcane" / "multiplayer"
CONTENT_SRC = kit.ANDROID_DIR / "common" / "java" / "arcane" / "content"
PLAIN_CONTENT = ("Files", "GamedataDelta", "LocalCdn")  # Android-free shared classes
TEST_JAVA = ROOT / "tests" / "java" / "arcane" / "multiplayer"
PLAIN_JAVA = ("ServerUrl", "LoopbackProxy", "JoinLink", "MetaRewrite")  # Android-free, and no library needed
pytestmark = pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")), reason="no JDK")


def link(url: str) -> str:
    """The connect link a server's join page shows (arcane://connect?url=<address>)."""
    scheme, host = re.search(r'<data android:scheme="(\w+)" android:host="(\w+)"/>', FRAGMENTS["multiplayer"]).groups()
    return f"{scheme}://{host}?url={quote(url, safe='')}"


@pytest.fixture(scope="module")
def classes(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("classes")
    subprocess.run(["javac", "-nowarn", "--release", "11", "-d", str(out),
                    *(str(JAVA_SRC / f"{c}.java") for c in PLAIN_JAVA),
                    *(str(CONTENT_SRC / f"{c}.java") for c in PLAIN_CONTENT)], check=True)
    return out


def java(cp: str, main: str, *args: str) -> list[str]:
    return subprocess.run(["java", "-cp", cp, main, *args], check=True, capture_output=True, text=True).stdout.splitlines()


def test_server_url_parsing(classes):
    inputs = ["203.0.113.5:18490", " http://Game.Example.com:18490/ ", "https://game.example.com",
              "https://game.example.com:443", "http://game.example.com:80", "http://[2001:db8::1]:18490", "", "ftp://x",
              "http://x/path", "http://u@x", "http://x:99999", "http://x?a=1", "not a url"]
    out = java(str(classes), "arcane.multiplayer.ServerUrl", *inputs)
    assert out[:6] == ["http://203.0.113.5:18490", "http://game.example.com:18490", "https://game.example.com",
                       "https://game.example.com", "http://game.example.com", "http://[2001:db8::1]:18490"]
    assert all(line.startswith("error: ") for line in out[6:]), out[6:]
    assert "Enter the server address" in out[6] and "http:// or https://" in out[7] and "without a path" in out[8]


def test_connect_links_and_scanned_codes(classes):
    """What a join page's connect link / QR code holds becomes the address, normalized like a typed one."""
    links = ["link:" + link("http://203.0.113.5:18490"), "link:" + link("https://Game.example.org"),
             "link:" + link("http://[2001:db8::1]:18490"), "link:ARCANE://Connect?x=1&url=game.example.org%3A18490",
             "link:arcane://connect?url=", "link:arcane://other?url=http%3A%2F%2Fx", "link:https://x",
             "link:" + link("http://x/path"), "link:arcane://connect?url=%zz"]
    out = java(str(classes), "arcane.multiplayer.JoinLink", *links)
    assert out[:4] == ["http://203.0.113.5:18490", "https://game.example.org", "http://[2001:db8::1]:18490",
                       "http://game.example.org:18490"]
    assert out[4] == "error: The link names no server address."
    assert out[5] == out[6] == "error: This is not a game server code."
    assert "without a path" in out[7] and out[8] == "error: This is not a game server code."  # bad escape
    scanned = [link("http://203.0.113.5:18490"), "http://game.example.org:18490", "https://game.example.org/join",
               "http://game.example.org:18490/JOIN/", "hello", "WIFI:S:x;;", "http://x/news"]
    out = java(str(classes), "arcane.multiplayer.JoinLink", *scanned)
    assert out[:4] == ["http://203.0.113.5:18490", "http://game.example.org:18490", "https://game.example.org",
                       "http://game.example.org:18490"]
    assert out[4] == out[5] == "error: This is not a game server code." and "without a path" in out[6]


def _png(path: Path, rows: list[list[int]], scale: int = 8, border: int = 4) -> None:
    """A grayscale PNG of a module matrix (1 = dark), drawn without any imaging library."""
    n = len(rows) + 2 * border
    px = []
    for y in range(n * scale):
        r = y // scale - border
        line = bytearray([0])
        for x in range(n * scale):
            c = x // scale - border
            dark = 0 <= r < len(rows) and 0 <= c < len(rows) and rows[r][c]
            line.append(0 if dark else 255)
        px.append(bytes(line))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n * scale, n * scale, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(px))) + chunk(b"IEND", b""))


def test_qr_code_decodes_in_the_app(tmp_path):
    """A join page's QR code, decoded with the app's own decoder (QrDecoder + ZXing) and read like the scanner does;
    a blank image has no code."""
    segno = pytest.importorskip("segno")
    zxing = offline.cached(*kit.ZXING_CORE)
    if zxing is None:
        pytest.skip("ZXing not in the cache (a multi build downloads it)")
    classes = tmp_path / "classes"
    subprocess.run(["javac", "-nowarn", "--release", "11", "-cp", str(zxing), "-d", str(classes),
                    *(str(JAVA_SRC / f"{c}.java") for c in (*PLAIN_JAVA, "QrDecoder")),
                    *(str(CONTENT_SRC / f"{c}.java") for c in PLAIN_CONTENT),
                    str(TEST_JAVA / "DecodePng.java")], check=True)
    matrix = [list(row) for row in segno.make(link("http://game.example.com:18490"), error="m", micro=False).matrix]
    _png(tmp_path / "qr.png", matrix)
    _png(tmp_path / "blank.png", [[0] * 21 for _ in range(21)])
    out = java(f"{classes}{os.pathsep}{zxing}", "arcane.multiplayer.DecodePng",
               str(tmp_path / "qr.png"), str(tmp_path / "blank.png"))
    assert out == ["http://game.example.com:18490", "no code"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream:
    """A tiny server on 127.0.0.1: answers a plain request with the Host it saw; a websocket upgrade gets 101 and then
    echoes every byte back."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.heads: list[str] = []
        self.routes: dict[str, tuple[bytes, bytes]] = {}  # path -> (status, body)
        self.bodies: list[bytes] = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c: socket.socket):
        with c:
            head = b""
            while b"\r\n\r\n" not in head:
                part = c.recv(4096)
                if not part:
                    return
                head += part
            text = head.decode("latin-1")
            self.heads.append(text)
            host = next(line.split(":", 1)[1].strip() for line in text.split("\r\n") if line.lower().startswith("host:"))
            if "upgrade: websocket" in text.lower():
                c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                while data := c.recv(4096):
                    c.sendall(data)
                return
            path = text.split(" ", 2)[1]
            if path in self.routes:
                length = next((int(line.split(":", 1)[1]) for line in text.split("\r\n")
                               if line.lower().startswith("content-length:")), 0)
                body_in = head.split(b"\r\n\r\n", 1)[1]
                while len(body_in) < length:
                    body_in += c.recv(4096)
                self.bodies.append(body_in)
                status, body = self.routes[path]
                c.sendall(b"HTTP/1.1 %s\r\nContent-Type: application/octet-stream\r\nContent-Length: %d\r\n"
                          b"Connection: close\r\n\r\n%s" % (status, len(body), body))
                return
            body = host.encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))


def _start_proxy(classes: Path, port: int, server: str, content: Path | None = None) -> subprocess.Popen:
    proc = subprocess.Popen(["java", "-cp", str(classes), "arcane.multiplayer.LoopbackProxy", str(port), server,
                             *([str(content)] if content else [])],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _request(port: int, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(raw)
        out = b""
        while part := s.recv(4096):
            out += part
    return out


def test_proxy_forwards_http_and_websocket_with_the_chosen_host(classes):
    up = Upstream()
    proxy_port = _free_port()
    chosen = f"localhost:{up.port}"  # the player typed this: the server must see it, not the proxy's address
    proc = _start_proxy(classes, proxy_port, chosen)
    try:
        for _ in range(3):  # one connection per request: the proxy closes after each
            res = _request(proxy_port, f"GET /status/maintenance.json HTTP/1.1\r\nHost: 127.0.0.1:{proxy_port}\r\n"
                                       "Connection: keep-alive\r\n\r\n".encode())
            assert res.startswith(b"HTTP/1.1 200") and res.endswith(chosen.encode())
        assert all("connection: close" in h.lower() and "keep-alive" not in h.lower() for h in up.heads)
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=10) as s:
            s.sendall(f"GET /?auth_token=t HTTP/1.1\r\nHost: 127.0.0.1:{proxy_port}\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\n\r\n".encode())
            got = b""
            while b"\r\n\r\n" not in got:
                got += s.recv(4096)
            assert got.startswith(b"HTTP/1.1 101")
            for msg in (b"frame-1", b"frame-2"):
                s.sendall(msg)
                assert s.recv(4096) == msg
        ws_head = up.heads[-1]
        assert f"Host: {chosen}" in ws_head and "Upgrade: websocket" in ws_head and "Connection: Upgrade" in ws_head
    finally:
        proc.kill()
        proc.wait()
        up.sock.close()


def test_proxy_reports_an_unreachable_server(classes):
    proxy_port, dead = _free_port(), _free_port()
    proc = _start_proxy(classes, proxy_port, f"http://127.0.0.1:{dead}")
    try:
        res = _request(proxy_port, b"GET /meta HTTP/1.1\r\nHost: x\r\n\r\n")
        assert res.startswith(b"HTTP/1.1 502") and f"127.0.0.1:{dead}".encode() in res
    finally:
        proc.kill()
        proc.wait()


# --- the client's files from the imported content set --------------------------------------------------------------


def _varint(v: int) -> bytes:
    out = b""
    while v > 0x7F:
        out += bytes([(v & 0x7F) | 0x80])
        v >>= 7
    return out + bytes([v])


def _field(number: int, data: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def _fields(msg: bytes) -> list[tuple[int, int, bytes | int]]:
    out, i = [], 0
    while i < len(msg):
        key, i = _read_varint(msg, i)
        number, wire = key >> 3, key & 7
        if wire == 2:
            n, i = _read_varint(msg, i)
            out.append((number, wire, msg[i:i + n]))
            i += n
        else:
            v, i = _read_varint(msg, i)
            out.append((number, wire, v))
    return out


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        shift += 7
        if x < 0x80:
            return v, i


def _delta(original: bytes, served: bytes) -> bytes:
    """A stand-in for the server's delta (the format GamedataDelta reads); test_delta_matches_the_server checks it and
    the app against the server's own implementation when that repository is next to this one."""
    import hashlib

    runs, i, n = [], 0, len(original)
    while i < n:
        if original[i] != served[i]:
            j = i
            while j < n and original[j] != served[j]:
                j += 1
            runs.append((i, served[i:j]))
            i = j
        else:
            i += 1
    if len(served) > n:
        runs.append((n, served[n:]))
    out = b"AGD1" + hashlib.sha256(original).digest() + struct.pack("<II", len(served), len(runs))
    for off, data in runs:
        out += struct.pack("<II", off, len(data)) + data
    return out


def _content_set(root: Path) -> tuple[Path, bytes]:
    content = root / "content" / "v29"
    (content / "assets").mkdir(parents=True)
    (content / "assets" / "bundle_a").write_bytes(b"bundle-a-bytes" * 1000)
    (content / "assets" / "store sale bundle").write_bytes(b"spaced")  # some bundle names have spaces
    (content / "assets" / "unlisted").write_bytes(b"not in the list")
    (content / "asset_bundle_metadata.json").write_text(json.dumps({"bundleInfo": [
        {"bundleName": "bundle_a", "crc": 1}, {"bundleName": "store sale bundle", "crc": 2},
        {"bundleName": "missing_file", "crc": 3}]}, indent=2))
    (content / "localization").mkdir()
    (content / "localization" / "Loc_English.txt").write_text("english")
    (content / "localization" / "Loc_French.txt").write_text("french")
    (content / "gamedata").mkdir()
    original = bytes(range(256)) * 40
    (content / "gamedata" / "gamedata-static.bin").write_bytes(original)
    return content, original


def _body(res: bytes) -> bytes:
    head, body = res.split(b"\r\n\r\n", 1)
    return body


def test_proxy_serves_the_clients_files_from_the_imported_data(classes, tmp_path):
    """Bundles, localization (English for a language the set lacks) and the gamedata (the imported one plus the
    server's delta) come from the phone; the bundle list and everything else from the server; /meta's download
    addresses point at the proxy."""
    content, original = _content_set(tmp_path)
    served = bytearray(original)
    served[10:14] = b"\xff\xfe\xfd\xfc"
    served += b"appended-strings"
    up = Upstream()
    up.routes["/gamedata/v7/gamedata-static.delta"] = (b"200 OK", _delta(original, bytes(served)))
    up.routes["/gamedata/v8/gamedata-static.delta"] = (b"200 OK", _delta(b"another gamedata", b"another gamedata!"))
    up.routes["/assets/v1-Android/metadata"] = (b"200 OK", b"the server's bundle list")
    meta = _field(1, _field(1, b"bundle-version") + _field(2, b"gamedata-version") + _field(3, b"http://server:1")
                  + _field(4, b"loc-version") + _field(5, b"http://server:1") + _field(6, b"http://server:1")) \
        + _varint(6 << 3) + _varint(42)
    up.routes["/meta"] = (b"200 OK", meta)
    proxy_port = _free_port()
    proc = _start_proxy(classes, proxy_port, f"localhost:{up.port}", content)
    get = lambda path: _request(proxy_port, f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())  # noqa: E731
    try:
        res = get("/assets/v1-Android/bundle_a")
        assert res.startswith(b"HTTP/1.1 200") and _body(res) == b"bundle-a-bytes" * 1000
        assert _body(get("/assets/v1-Android/store%20sale%20bundle")) == b"spaced"  # percent-decoded
        for bad in ("missing", "missing_file", "unlisted", "..%2Fx", "%2E%2E%2Fasset_bundle_metadata.json",
                    "bundle_a%00", "%zz", "store%5Csale"):
            assert get(f"/assets/v1-Android/{bad}").startswith(b"HTTP/1.1 404"), bad
        assert _body(get("/assets/v1-Android/metadata")) == b"the server's bundle list"  # forwarded
        assert _body(get("/localization/l1/Loc_French.txt")) == b"french"
        assert _body(get("/localization/l1/Loc_German.txt")) == b"english"  # not in the set: English
        res = get("/gamedata/v7/gamedata-static.bin")
        assert res.startswith(b"HTTP/1.1 200") and _body(res) == bytes(served)
        res = get("/gamedata/v8/gamedata-static.bin")  # a delta for another gamedata: refused
        assert res.startswith(b"HTTP/1.1 502") and b"another gamedata" in res
        req = b"\x0a\x03abc"
        res = _request(proxy_port, b"POST /meta HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s" % (len(req), req))
        assert res.startswith(b"HTTP/1.1 200") and up.bodies[-1] == req
        envelope = _fields(_body(res))
        assert envelope[1:] == [(6, 0, 42)]  # the rest of the envelope as sent
        fields = {n: v for n, _, v in _fields(envelope[0][2])}
        local = f"http://127.0.0.1:{proxy_port}".encode()
        assert fields == {1: b"bundle-version", 2: b"gamedata-version", 3: local, 4: b"loc-version", 5: local, 6: local}
        assert not any("/assets/v1-Android/bundle_a" in h or "Loc_" in h or "gamedata-static.bin" in h for h in up.heads)
    finally:
        proc.kill()
        proc.wait()
        up.sock.close()


def test_proxy_without_imported_data_serves_no_client_files(classes):
    up = Upstream()
    proxy_port = _free_port()
    proc = _start_proxy(classes, proxy_port, f"localhost:{up.port}")
    try:
        res = _request(proxy_port, b"GET /assets/v1/bundle_a HTTP/1.1\r\nHost: x\r\n\r\n")
        assert res.startswith(b"HTTP/1.1 404") and b"not imported" in res and not up.heads
    finally:
        proc.kill()
        proc.wait()
        up.sock.close()


def test_proxy_survives_odd_server_answers(classes, tmp_path):
    """A truncated /meta answer, a bad status line, a delta that is too large or has trailing bytes: an error answer
    to the game, never a crash of the proxy (it keeps answering)."""
    content, original = _content_set(tmp_path)
    up = Upstream()
    good = _delta(original, original + b"x")
    up.routes["/gamedata/big/gamedata-static.delta"] = (
        b"200 OK", good[:36] + struct.pack("<II", len(original) + (17 << 20), 0))
    up.routes["/gamedata/tail/gamedata-static.delta"] = (b"200 OK", good + b"extra")
    up.routes["/meta"] = (b"200 OK", _field(1, b"\x1a\xff\xff\xff\x0f"))  # field 3 claims ~4 GB
    proxy_port = _free_port()
    proc = _start_proxy(classes, proxy_port, f"localhost:{up.port}", content)
    get = lambda path: _request(proxy_port, f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())  # noqa: E731
    try:
        assert get("/gamedata/big/gamedata-static.bin").startswith(b"HTTP/1.1 502")
        res = get("/gamedata/tail/gamedata-static.bin")
        assert res.startswith(b"HTTP/1.1 502") and b"trailing" in res
        res = _request(proxy_port, b"POST /meta HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        assert res.startswith(b"HTTP/1.1 502") and b"/meta" in res
        assert _body(get("/assets/v1/bundle_a")) == b"bundle-a-bytes" * 1000  # still serving
        assert all("x-arcane-public-base" not in h.lower() for h in up.heads if "/meta" in h)
        delta_heads = [h for h in up.heads if ".delta" in h]
        assert delta_heads and all(f"X-Arcane-Public-Base: http://127.0.0.1:{proxy_port}" in h for h in delta_heads)
    finally:
        proc.kill()
        proc.wait()
        up.sock.close()


def test_meta_forwarding_strips_compression_and_expect(classes, tmp_path):
    content, _ = _content_set(tmp_path)
    up = Upstream()
    up.routes["/meta"] = (b"200 OK", _field(1, _field(3, b"http://server:1")))
    proxy_port = _free_port()
    proc = _start_proxy(classes, proxy_port, f"localhost:{up.port}", content)
    try:
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=10) as s:
            s.sendall(b"POST /meta HTTP/1.1\r\nHost: x\r\nAccept-Encoding: gzip\r\nExpect: 100-continue\r\n"
                      b"Content-Length: 3\r\n\r\n")
            got = b""
            while b"\r\n\r\n" not in got:
                got += s.recv(4096)
            assert got.startswith(b"HTTP/1.1 100")
            s.sendall(b"abc")
            while part := s.recv(4096):
                got += part
        assert b"HTTP/1.1 200" in got
        head = next(h for h in up.heads if h.startswith("POST /meta")).lower()
        assert "accept-encoding" not in head and "expect" not in head and up.bodies[-1] == b"abc"
    finally:
        proc.kill()
        proc.wait()
        up.sock.close()


# the server repository (arcane-server), next to this one or named by ARCANE_SERVER_REPO
SERVER = Path(os.environ.get("ARCANE_SERVER_REPO") or ROOT.parent / "arcane-server")


@pytest.mark.skipif(not (SERVER / "src" / "arcane_server" / "served_gamedata.py").is_file(),
                    reason="the server repository is not next to this one (ARCANE_SERVER_REPO)")
def test_delta_matches_the_server(tmp_path):
    """The app rebuilds what the server serves from the server's own delta (and the tests' stand-in writes the
    same bytes as the server)."""
    out = tmp_path / "classes"
    subprocess.run(["javac", "-nowarn", "--release", "11", "-d", str(out),
                    *(str(CONTENT_SRC / f"{c}.java") for c in PLAIN_CONTENT),
                    str(ROOT / "tests" / "java" / "arcane" / "content" / "ApplyDelta.java")], check=True)
    original = bytes(range(256)) * 300
    cases = {"same": original, "edits": original[:100] + b"\xff" * 7 + original[107:5000] + b"\x00" + original[5001:],
             "appended": original[:-4] + b"\x01\x02\x03\x04" + b"new strings" * 50}
    for name, served in cases.items():
        (tmp_path / f"{name}.orig").write_bytes(original)
        (tmp_path / f"{name}.served").write_bytes(served)
    script = ("import sys, pathlib\n"
              "from arcane_server.served_gamedata import delta\n"
              "d = pathlib.Path(sys.argv[1])\n"
              "for name in sys.argv[2:]:\n"
              "    (d / f'{name}.delta').write_bytes(delta((d / f'{name}.orig').read_bytes(), (d / f'{name}.served').read_bytes()))\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRTUAL_ENV")}
    run = subprocess.run(["uv", "run", "--quiet", "--project", str(SERVER), "python", "-c", script, str(tmp_path),
                          *cases], env=env, capture_output=True, text=True)
    if run.returncode != 0:
        pytest.skip(f"the server's environment is not usable here: {run.stderr.strip()[-200:]}")
    for name, served in cases.items():
        d = (tmp_path / f"{name}.delta").read_bytes()
        assert d == _delta(original, served), name
        assert java(str(out), "arcane.content.ApplyDelta", str(tmp_path / f"{name}.orig"), str(tmp_path / f"{name}.delta"),
                    str(tmp_path / f"{name}.out")) == ["ok"]
        assert (tmp_path / f"{name}.out").read_bytes() == served, name
    other = tmp_path / "other.orig"
    other.write_bytes(original[::-1])
    assert java(str(out), "arcane.content.ApplyDelta", str(other), str(tmp_path / "edits.delta"),
                str(tmp_path / "x.out"))[0].startswith("error: the gamedata delta is for another gamedata")
