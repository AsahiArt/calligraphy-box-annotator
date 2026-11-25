"""Back-card geometry and dark-mode colors, checked in a headless browser."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "cba" / "anki_templates" / "default"
CHROME = next(
    (Path(candidate) for candidate in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable") if Path(candidate).is_file()),
    None,
)
ARTIFACTS = Path("/opt/cursor/artifacts")


def _glyph() -> str:
    image = Image.new("RGB", (180, 220), (244, 236, 216))
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, 163, 203), outline=(28, 26, 23), width=4)
    draw.line((40, 40, 140, 180), fill=(28, 26, 23), width=5)
    draw.line((140, 50, 40, 170), fill=(28, 26, 23), width=3)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _fill(template: str, char: str, count: int, glyph: str) -> str:
    images = [glyph] * count
    text = template
    text = text.replace("{{Char}}", char)
    text = text.replace("{{Image}}", f'<img src="{images[0]}" alt="{char}">')
    text = text.replace("{{Variants}}", "\n".join(f'<img src="{src}" alt="{char}">' for src in images))
    text = text.replace("{{Sources}}", "\n".join(f"plate-01·{index}" for index in range(count)))
    text = text.replace("{{Count}}", str(count))
    return text


def _page(body: str, css: str, klass: str) -> str:
    html_open = f'<html class="{klass}">' if klass else "<html>"
    return f"""<!DOCTYPE html>{html_open}<head><meta charset="utf-8"><style>{css}</style></head>
<body><div id="qa"><div class="card">{body}</div></div>
<pre id="metrics"></pre>
<script>
setTimeout(function() {{
  function box(el) {{
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return {{t: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width), b: Math.round(r.bottom)}};
  }}
  var card = document.querySelector(".card");
  var img = document.querySelector(".slot img") || document.querySelector(".solo-img img") || document.querySelector(".han.printed");
  var paper = getComputedStyle(card).backgroundColor;
  var filter = img ? getComputedStyle(img).filter : "";
  document.getElementById("metrics").textContent = JSON.stringify({{
    card: box(card),
    reprise: box(document.querySelector(".reprise")),
    gallery: box(document.querySelector(".gallery")),
    subject: box(img),
    paper: paper,
    filter: filter
  }});
}}, 900);
</script></body></html>"""


def _measure(html: str, size: str, *, color_scheme: str | None = None) -> dict:
    if CHROME is None:
        raise unittest.SkipTest("chrome is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "card.html"
        path.write_text(html, encoding="utf-8")
        command = [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--virtual-time-budget=2500",
            f"--window-size={size}",
            "--dump-dom",
            path.as_uri(),
        ]
        if color_scheme:
            command.insert(-1, f"--force-prefers-color-scheme={color_scheme}")
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = ""
        completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30, check=False)
        match = re.search(r'<pre id="metrics">(\{.*?\})</pre>', completed.stdout, re.DOTALL)
        if match is None:
            raise AssertionError(completed.stderr[-500:] or completed.stdout[-500:])
        return json.loads(match.group(1))


def _measure_prefers_dark(html: str) -> dict:
    """Ask Chrome to emulate prefers-color-scheme: dark and read the card metrics."""
    if CHROME is None:
        raise unittest.SkipTest("chrome is not installed")
    import socket
    import struct
    import urllib.request

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        page = folder / "card.html"
        page.write_text(html, encoding="utf-8")
        profile = folder / "profile"
        port = 9334
        proc = subprocess.Popen(
            [
                str(CHROME),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--window-size=390,844",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sock = None
        try:
            targets = None
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.5) as resp:
                        targets = json.load(resp)
                        break
                except OSError:
                    time.sleep(0.1)
            if not targets:
                raise AssertionError("Chrome did not open a debugging port")
            ws_url = targets[0]["webSocketDebuggerUrl"]
            host_port, ws_path = ws_url.split("://", 1)[1].split("/", 1)
            host, port_s = host_port.split(":")
            sock = socket.create_connection((host, int(port_s)))
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                (
                    f"GET /{ws_path} HTTP/1.1\r\nHost: {host}:{port_s}\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += sock.recv(4096)

            def send(obj: dict) -> None:
                data = json.dumps(obj).encode()
                mask = os.urandom(4)
                header = bytearray([0x81])
                length = len(data)
                if length < 126:
                    header.append(0x80 | length)
                else:
                    header.append(0x80 | 126)
                    header += struct.pack("!H", length)
                header += mask
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
                sock.sendall(bytes(header) + payload)

            def recv() -> dict:
                header = b""
                while len(header) < 2:
                    header += sock.recv(2 - len(header))
                length = header[1] & 127
                if length == 126:
                    extra = b""
                    while len(extra) < 2:
                        extra += sock.recv(2 - len(extra))
                    length = struct.unpack("!H", extra)[0]
                elif length == 127:
                    extra = b""
                    while len(extra) < 8:
                        extra += sock.recv(8 - len(extra))
                    length = struct.unpack("!Q", extra)[0]
                chunks = b""
                while len(chunks) < length:
                    chunks += sock.recv(length - len(chunks))
                return json.loads(chunks)

            call_id = 0

            def call(method: str, params: dict | None = None) -> dict:
                nonlocal call_id
                call_id += 1
                message = {"id": call_id, "method": method}
                if params:
                    message["params"] = params
                send(message)
                while True:
                    reply = recv()
                    if reply.get("id") == call_id:
                        return reply

            call(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-color-scheme", "value": "dark"}]},
            )
            call("Page.navigate", {"url": page.as_uri()})
            time.sleep(1.2)
            evaluated = call(
                "Runtime.evaluate",
                {
                    "expression": "document.getElementById('metrics') && document.getElementById('metrics').textContent",
                    "returnByValue": True,
                },
            )
            value = evaluated.get("result", {}).get("result", {}).get("value")
            if not value:
                raise AssertionError(evaluated)
            return json.loads(value)
        finally:
            if sock is not None:
                sock.close()
            proc.terminate()
            proc.wait(timeout=5)


def _screenshot(html: str, dest: Path, size: str) -> None:
    if CHROME is None or not ARTIFACTS.is_dir():
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "card.html"
        path.write_text(html, encoding="utf-8")
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = ""
        subprocess.run(
            [
                str(CHROME),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--virtual-time-budget=2500",
                f"--window-size={size}",
                f"--screenshot={dest}",
                path.as_uri(),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )


class TemplateContractTests(unittest.TestCase):
    def test_back_is_compact_and_dark_mode_does_not_invert_images(self) -> None:
        css = (TEMPLATES / "style.css").read_text(encoding="utf-8")
        back = (TEMPLATES / "back.html").read_text(encoding="utf-8")
        front = (TEMPLATES / "front.html").read_text(encoding="utf-8")
        front_char = (TEMPLATES / "front-char.html").read_text(encoding="utf-8")
        shupu = (ROOT / "cba" / "anki_templates" / "shupu_back" / "back.html").read_text(encoding="utf-8")
        self.assertNotIn("grid-template-rows: minmax(0, 1fr) minmax(0, 1fr)", css)
        self.assertIn("flex: 0 0 auto", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn(".nightMode", css)
        self.assertIn(".night_mode", css)
        self.assertIn("--paper: #1a1714", css)
        self.assertIn("--paper: #f4ecd8", css)
        self.assertIn("filter: none", css)
        self.assertIn(".leaf:not(.back) .front-single", css)
        self.assertIn("solo-img", front)
        self.assertIn("function measure()", front)
        self.assertIn("function measure()", front_char)
        self.assertIn('classList.toggle("few"', back)
        self.assertNotIn("{{Image}}", front_char)
        for field in ("Simplified", "Traditional", "Same", "CursiveChar", "ImgSun", "ImgMifu", "ImgHuaisu"):
            self.assertIn("{{" + field + "}}", shupu)
        self.assertNotIn("{{Rank}}", shupu)
        self.assertNotIn("{{Char}}", shupu)
        self.assertNotIn("{{Variants}}", shupu)


class RenderedLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if CHROME is None:
            raise unittest.SkipTest("chrome is not installed")
        cls.css = (TEMPLATES / "style.css").read_text(encoding="utf-8")
        cls.back = (TEMPLATES / "back.html").read_text(encoding="utf-8")
        cls.front = (TEMPLATES / "front.html").read_text(encoding="utf-8")
        cls.front_char = (TEMPLATES / "front-char.html").read_text(encoding="utf-8")
        cls.glyph = _glyph()

    def _html(self, kind: str, count: int, klass: str) -> str:
        template = {"back": self.back, "image": self.front, "char": self.front_char}[kind]
        return _page(_fill(template, "甲", count, self.glyph), self.css, klass)

    def test_back_header_is_compact_on_phone_and_desktop(self) -> None:
        for size in ("390,844", "1280,800"):
            for count in (1, 6):
                metrics = _measure(self._html("back", count, ""), size)
                card = metrics["card"]["h"]
                reprise = metrics["reprise"]["h"]
                gallery = metrics["gallery"]["h"]
                self.assertLess(reprise, card * 0.22, metrics)
                self.assertGreater(gallery, card * 0.70, metrics)
                self.assertLessEqual(metrics["reprise"]["t"], 8, metrics)
                self.assertLessEqual(metrics["gallery"]["t"], metrics["reprise"]["t"] + reprise + 4, metrics)
                self.assertGreater(metrics["subject"]["h"], 80, metrics)
                self.assertEqual(metrics["filter"], "none", metrics)
                if count == 1:
                    self.assertGreater(metrics["subject"]["h"], card * 0.35, metrics)

    def test_front_stays_vertically_centered(self) -> None:
        for kind in ("image", "char"):
            for size in ("390,844", "1280,800"):
                metrics = _measure(self._html(kind, 1, ""), size)
                card = metrics["card"]
                subject = metrics["subject"]
                card_mid = card["t"] + card["h"] / 2
                subject_mid = subject["t"] + subject["h"] / 2
                self.assertLess(abs(subject_mid - card_mid), card["h"] * 0.18, metrics)

    def test_night_mode_and_prefers_color_scheme_are_dark_without_inverting(self) -> None:
        night = _measure(self._html("back", 1, "nightMode"), "390,844")
        snake = _measure(self._html("back", 6, "night_mode"), "1280,800")
        scheme = _measure_prefers_dark(self._html("back", 1, ""))
        for metrics in (night, snake, scheme):
            channels = [int(part) for part in re.findall(r"\d+", metrics["paper"])]
            self.assertGreaterEqual(len(channels), 3, metrics)
            self.assertLess(sum(channels[:3]) / 3, 80, metrics)
            self.assertEqual(metrics["filter"], "none", metrics)
            self.assertLess(metrics["reprise"]["h"], metrics["card"]["h"] * 0.22, metrics)
        light = _measure(self._html("image", 1, ""), "390,844", color_scheme="light")
        light_channels = [int(part) for part in re.findall(r"\d+", light["paper"])]
        self.assertGreater(sum(light_channels[:3]) / 3, 180, light)

    def test_save_review_screenshots(self) -> None:
        if not ARTIFACTS.is_dir():
            self.skipTest("artifact directory is not mounted")
        shots = [
            ("after-back-one-light-390x844.png", "back", 1, "", "390,844"),
            ("after-back-many-light-390x844.png", "back", 6, "", "390,844"),
            ("after-back-one-light-1280x800.png", "back", 1, "", "1280,800"),
            ("after-back-many-light-1280x800.png", "back", 6, "", "1280,800"),
            ("after-back-one-dark-390x844.png", "back", 1, "nightMode", "390,844"),
            ("after-back-many-dark-390x844.png", "back", 6, "nightMode", "390,844"),
            ("after-back-one-dark-1280x800.png", "back", 1, "nightMode", "1280,800"),
            ("after-front-image-light-390x844.png", "image", 1, "", "390,844"),
            ("after-front-image-dark-390x844.png", "image", 1, "nightMode", "390,844"),
            ("after-front-char-light-390x844.png", "char", 1, "", "390,844"),
            ("after-front-char-dark-1280x800.png", "char", 1, "nightMode", "1280,800"),
        ]
        for filename, kind, count, klass, size in shots:
            _screenshot(self._html(kind, count, klass), ARTIFACTS / filename, size)
            self.assertTrue((ARTIFACTS / filename).is_file(), filename)
            self.assertGreater((ARTIFACTS / filename).stat().st_size, 1000, filename)
