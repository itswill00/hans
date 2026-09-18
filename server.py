#!/usr/bin/env python3
# ponytail: stdlib only, one file. Saves config.json + image upload. No deps, no framework.
import http.server, json, os, pathlib, mimetypes, urllib.parse, time, re

ROOT = pathlib.Path(__file__).parent
CONFIG = ROOT / "config.json"
ASSETS = ROOT / "assets"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
ALLOWED_ORIGINS = set()  # same-origin only; tunnel viewers can't POST anyway (they hit Cloudflare, not this)

def guess_type(p: pathlib.Path):
    t,_ = mimetypes.guess_type(str(p))
    return t or "application/octet-stream"

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # no cache for config/admin
        if self.path.split("?")[0] in ("/config.json", "/admin.html"):
            self.send_header("Cache-Control", "no-store")
        # clickjacking + nosniff for all
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def has_admin_token(self):
        if not ADMIN_TOKEN:
            return False
        tok = self.headers.get("X-Admin-Token") or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return tok == ADMIN_TOKEN

    def is_local_request(self):
        # Local (loopback/private) OR valid admin token bypasses tunnel block
        if self.has_admin_token():
            return True
        # Tunnel via cloudflared connects as 127.0.0.1 but carries Cf-Ray — must be
        # treated as public, not local. Check BEFORE loopback, otherwise tunnel
        # bypasses auth entirely (client 127.0.0.1 would return True early).
        if "Cf-Ray" in self.headers or "Cf-Connecting-Ip" in self.headers:
            return False
        client = self.client_address[0] if self.client_address else ""
        if client.startswith("127.") or client == "::1" or client == "::ffff:127.0.0.1":
            return True
        # allow RFC1918 / link-local for local admin without tunnel (direct IP, no Cf-Ray)
        return client.startswith("192.168.") or client.startswith("10.") or client.startswith("172.")

    def do_GET(self):
        clean_path = urllib.parse.urlparse(self.path).path

        # Protect sensitive files from being downloaded
        if any(clean_path.endswith(ext) for ext in (".py", ".log", ".bak")) or ".bak." in clean_path:
            self.send_error(404, "not found")
            return

        # Admin HTML always servable — real gate is JS (fetch config.json) + POST /api/*
        if clean_path == "/admin.html" and "admin_token" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query):
            qtok = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("admin_token", [""])[0]
            if ADMIN_TOKEN and qtok and qtok != ADMIN_TOKEN:
                self.send_error(401, "bad admin_token")
                return

        # /config.json
        if clean_path == "/config.json":
            if self.is_local_request():
                return super().do_GET()
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            qtok = (q.get("admin_token") or [""])[0]
            is_admin = (ADMIN_TOKEN and qtok == ADMIN_TOKEN) or self.has_admin_token()
            if is_admin:
                return super().do_GET()
            if "admin" in q:
                self.send_error(401, "admin token required")
                return
            try:
                raw = CONFIG.read_text(encoding="utf-8")
                data = json.loads(raw)
                acts = data.get("activities")
                if isinstance(acts, list):
                    data["activities"] = [a for a in acts if a.get("enabled") is not False]
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            except Exception:
                body = CONFIG.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
            return

        host = self.headers.get("Host", "")
        if host.startswith("www."):
            # ponytail: canonical redirect — www -> apex, single host. No Cloudflare rule needed.
            target = f"https://hanscloudserge.web.id{clean_path}"
            if urllib.parse.urlparse(self.path).query:
                target += "?" + urllib.parse.urlparse(self.path).query
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            return
        return super().do_GET()

    def do_POST(self):
        # Admin edits allowed locally or with token
        if self.path.startswith("/api/") and self.has_admin_token():
            pass  # token valid even via tunnel — allow
        elif not self.is_local_request():
            # also allow token via ?admin_token query on POST (fallback for simple fetch)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            qtok = (q.get("admin_token") or [""])[0]
            if not (ADMIN_TOKEN and qtok == ADMIN_TOKEN):
                self.send_error(403, "Forbidden: Modifications only allowed locally or with admin token")
                return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/save":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1 * 1024 * 1024:
                self.send_error(413, "payload too large (max 1MB)")
                return
            if length == 0:
                self.send_error(400, "empty payload")
                return
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception as e:
                self.send_error(400, f"bad json: {e}")
                return
            # minimal validation
            if not isinstance(data, dict) or "profile" not in data:
                self.send_error(400, "invalid config shape")
                return
            # backup
            if CONFIG.exists():
                bak = CONFIG.with_suffix(f".bak.{int(time.time())}.json")
                try:
                    bak.write_bytes(CONFIG.read_bytes())
                except Exception:
                    pass
            CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/upload":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self.send_error(400, "expected multipart/form-data")
                return
            # parse multipart without cgi (removed in 3.13). Frontend sends canvas JPEG.
            length = int(self.headers.get("Content-Length", "0"))
            if length > 9 * 1024 * 1024:
                self.send_error(413, "payload too large (max 9MB)")
                return
            raw = self.rfile.read(length)
            # extract file bytes between first \r\n\r\n and last \r\n--  (ponytail: naive but enough — frontend sends single file field)
            try:
                hdr_end = raw.index(b"\r\n\r\n") + 4
                # find trailing boundary
                tail = raw.rindex(b"\r\n--")
                data = raw[hdr_end:tail]
                # strip possible trailing \r\n
                if data.endswith(b"\r\n"):
                    data = data[:-2]
            except Exception:
                self.send_error(400, "multipart parse failed")
                return
            if not data or len(data) > 8 * 1024 * 1024:
                self.send_error(400, "empty or too large (max 8MB)")
                return
            is_jpeg = data[:2] == b"\xff\xd8"
            is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
            is_webp = data[:4] == b"RIFF" and b"WEBP" in data[:16]
            if not (is_jpeg or is_png or is_webp):
                self.send_error(400, "only jpg/png/webp")
                return
            ASSETS.mkdir(parents=True, exist_ok=True)
            base = f"photo-{int(time.time())}.jpg"
            out = ASSETS / base
            out.write_bytes(data)
            rel = f"assets/{base}"
            body = json.dumps({"ok": True, "path": rel}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404, "not found")

    def log_message(self, format, *args):
        # quiet
        pass

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--allow-lan", action="store_true", help="allow 0.0.0.0 bind for LAN admin (default 127.0.0.1 only)")
    args = ap.parse_args()
    if args.allow_lan:
        args.host = "0.0.0.0"
    # ensure config exists
    if not CONFIG.exists():
        raise SystemExit(f"missing {CONFIG}")
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    # serve from ROOT
    os.chdir(ROOT)
    print(f"serving {ROOT} at http://{args.host}:{args.port}/  (admin at /admin.html)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
