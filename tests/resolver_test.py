"""
サイト固有リゾルバ (StreamHG / iPlayerHLS) のテスト。

StreamHG を模したローカルサーバを立てて、以下を検証する:
  1) URL 形式の解釈 (/e/<code>, /e/<code>.html, /<code>, /<code>.html, /d/, /f/)
  2) 埋め込みページから .m3u8 を抽出し、実際にダウンロードできる
  3) ドメインロック: 解決で判明した Referer が自動付与され、
     m3u8 と全 .ts セグメントが 403 にならずに取得できる
  4) 失効ページ → 分かりやすい日本語エラー
  5) m3u8 が無い (難読化) ページ → DevTools 案内のエラー
  6) SSRF: ページ内に内部 IP への m3u8 が仕込まれていても拒否される
  7) 対応外ホスト → リゾルバは素通り (None) で既存動作を壊さない
  8) リダイレクト経由の SSRF も再検証される
  9) /api/info でも解決が効く

実行: python tests/resolver_test.py
"""
import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

HLS_DIR = Path("/tmp/hls")
if not (HLS_DIR / "index.m3u8").exists():
    print("HLS テストメディアがありません。生成してください (README 参照)。")
    sys.exit(2)

TEST_DIR = Path("/tmp/vdl-resolver")
if TEST_DIR.exists():
    import shutil as _sh
    _sh.rmtree(TEST_DIR)
TEST_DIR.mkdir(parents=True)

os.environ.update({
    "AUTH_TOKEN": "t",
    "DOWNLOAD_DIR": str(TEST_DIR),
    "BLOCK_PRIVATE_HOSTS": "0",   # ローカルサーバにアクセスさせるため
    "MIN_FREE_DISK_MB": "1",
    "FILE_TTL_SECONDS": "300",
    "REAPER_INTERVAL": "30",
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import resolvers  # noqa: E402
# 127.0.0.1 を「StreamHG 系ホスト」として登録し、実コード経路をそのまま通す
resolvers.STREAMHG_HOSTS.add("127.0.0.1")

# ==========================================================================
# StreamHG 風の模擬サーバ
# ==========================================================================
VALID = "validcode123"      # 正常に m3u8 が取れる
EXPIRED = "expiredcode1"    # 失効ページ
OBFUSC = "obfuscated11"     # m3u8 が無い (難読化)
SSRFID = "ssrfcode1234"     # 内部IPを指す m3u8 が仕込まれている
REDIRECT = "redirectcode1"  # リダイレクトで内部IPへ飛ばす

PAGE_TMPL = """<!DOCTYPE html><html><head><title>{title}</title></head><body>
<div id="player"></div>
<script>
  var player = jwplayer("player");
  player.setup({{
    sources: [{{ file: "{media}", type: "application/vnd.apple.mpegurl" }}],
    title: "{title}",
    image: "/images/thumb.jpg"
  }});
</script>
</body></html>"""

EXPIRED_PAGE = """<html><body style="width:100%;height:100%;padding:0;margin:0;">
<center>
<div style="position: absolute;top:50%;width:100%;text-align:center;font: 15px Verdana;">\
File is no longer available as it expired or has been deleted.</div>
</center>
<img src="/images/player_blank.jpg" id="over">
</body></html>"""

OBFUSC_PAGE = "<!DOCTYPE html><html><head><title>Player</title></head><body>" + \
    "<script>var _0x1a2b=['\\x68','\\x74'];eval(function(p,a,c,k,e){}(0,1));</script>" * 40 + \
    "</body></html>"

SSRF_PAGE = PAGE_TMPL.format(title="evil", media="http://169.254.169.254/latest/meta-data/x.m3u8")

STATS = {"ok": 0, "denied": 0, "hits": []}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path
        STATS["hits"].append(path)

        # --- 埋め込みプレイヤーページ ---
        if path.startswith("/e/") or path.startswith("/d/") or path.startswith("/f/"):
            code = path.split("/")[-1].replace(".html", "")
            if code == VALID:
                media = f"http://127.0.0.1:{PORT}/hls/index.m3u8"
                return self._send(200, PAGE_TMPL.format(title="Sample Movie 1080p - StreamHG", media=media))
            if code == EXPIRED:
                return self._send(200, EXPIRED_PAGE)
            if code == OBFUSC:
                return self._send(200, OBFUSC_PAGE)
            if code == SSRFID:
                return self._send(200, SSRF_PAGE)
            if code == REDIRECT:
                return self._send(302, "", extra={"Location": "http://127.0.0.1:1/steal"})
            return self._send(200, EXPIRED_PAGE)

        # 視聴ページ /<code>  (embed へ正規化されるはず)
        if path.strip("/").replace(".html", "") == VALID and path.count("/") <= 2:
            media = f"http://127.0.0.1:{PORT}/hls/index.m3u8"
            return self._send(200, PAGE_TMPL.format(title="Watch Page Title", media=media))

        # --- ドメインロックされた HLS ---
        if path.startswith("/hls/"):
            ref = self.headers.get("Referer") or ""
            # 正体: /e/<code>.html からの Referer だけ許可する
            if "/e/" in ref and VALID in ref:
                STATS["ok"] += 1
                fpath = HLS_DIR / path.split("/hls/")[-1]
                if fpath.is_file():
                    ctype = "application/vnd.apple.mpegurl" if fpath.suffix == ".m3u8" else "video/mp2t"
                    return self._send(200, fpath.read_bytes(), ctype)
                return self._send(404, "not found")
            STATS["denied"] += 1
            return self._send(403, "Forbidden: referer required")

        return self._send(404, "not found")

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, *a):
        pass


srv = Server(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'  ok   ' if cond else ' FAIL '} | {name}" + (f" :: {extra}" if not cond and extra else ""))


H = {"X-API-Key": "t"}


def wait_job(client, jid, timeout=120):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/status/{jid}", headers=H).json()["job"]
        if job["state"] in ("finished", "error"):
            return job
        time.sleep(0.2)
    return job


def clean():
    for f in TEST_DIR.iterdir():
        f.unlink()


def base(path):
    return f"http://127.0.0.1:{PORT}{path}"


def _raises(fn):
    """fn が何らかの例外を投げたら True。"""
    try:
        fn()
        return False
    except Exception:
        return True


# ==========================================================================
print("\n--- 1) URL 形式の解釈 (parse_streamhg_code / is_streamhg_url) ---")
cases = [
    ("https://iplayerhls.com/e/tosva74t17xo", "tosva74t17xo"),
    ("https://iplayerhls.com/e/tosva74t17xo.html", "tosva74t17xo"),
    ("https://iplayerhls.com/e/svdyfxg6p0up.html", "svdyfxg6p0up"),
    ("https://iplayerhls.com/tosva74t17xo", "tosva74t17xo"),
    ("https://iplayerhls.com/tosva74t17xo.html", "tosva74t17xo"),
    ("https://streamhg.com/e/abc123def456", "abc123def456"),
    ("https://www.iplayerhls.com/d/abc123def456", "abc123def456"),
    ("https://iplayerhls.com/f/abc123def456", "abc123def456"),
    ("http://iplayerhls.com/e/abc123def456", "abc123def456"),
]
for url, expect in cases:
    got = resolvers.parse_streamhg_code(url)
    check(f"解析 {url.replace('https://','')[:44]} -> {expect}", got == expect, got)

check("is_streamhg_url: iplayerhls", resolvers.is_streamhg_url("https://iplayerhls.com/e/abc123def456"))
check("is_streamhg_url: streamhg", resolvers.is_streamhg_url("https://streamhg.com/e/abc123def456"))
check("is_streamhg_url: 対応外ホストは False",
      not resolvers.is_streamhg_url("https://youtube.com/watch?v=x"))
check("is_streamhg_url: 静的アセットは False",
      not resolvers.is_streamhg_url("https://iplayerhls.com/HG1/js/app.js"))
check("is_streamhg_url: トップページは False",
      not resolvers.is_streamhg_url("https://iplayerhls.com/"))
check("embed_url 正規化",
      resolvers.embed_url("https://iplayerhls.com/tosva74t17xo", "tosva74t17xo")
      == "https://iplayerhls.com/e/tosva74t17xo.html",
      resolvers.embed_url("https://iplayerhls.com/tosva74t17xo", "tosva74t17xo"))

# ==========================================================================
print("\n--- 2) & 3) ページ解決 → ドメインロック付き HLS を取得 ---")
with TestClient(main.app) as client:
    clean()
    STATS["ok"] = STATS["denied"] = 0
    STATS["hits"].clear()

    for form in [f"/e/{VALID}", f"/e/{VALID}.html", f"/{VALID}", f"/{VALID}.html", f"/d/{VALID}"]:
        clean()
        STATS["ok"] = STATS["denied"] = 0
        r = client.post("/api/download", json={"url": base(form), "quality": "best"}, headers=H)
        check(f"受付 202 ({form})", r.status_code == 202, f"{r.status_code} {r.text[:150]}")
        if r.status_code != 202:
            continue
        jid = r.json()["job"]["id"]
        job = wait_job(client, jid)
        check(f"★ {form} → finished", job["state"] == "finished",
              (job["state"], (job.get("error") or "")[:220]))
        if job["state"] != "finished":
            continue
        check(f"resolver が記録されている ({form})",
              (job.get("resolver") or "").startswith("streamhg"), job.get("resolver"))
        check(f"Referer が自動設定された ({form})", bool(job.get("referer")), job.get("referer"))
        check(f"403 拒否が発生していない ({form})", STATS["denied"] == 0,
              {"denied": STATS["denied"], "ok": STATS["ok"]})
        files = sorted(TEST_DIR.iterdir())
        check(f"成果物1件 ({form})", len(files) == 1, [p.name for p in files])
        if files:
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                    "-of", "json", str(files[0])], capture_output=True, text=True)
            kinds = [s.get("codec_type") for s in json.loads(probe.stdout or "{}").get("streams", [])]
            check(f"映像+音声が揃った mp4 ({form})", "video" in kinds and "audio" in kinds, kinds)
            check(f"全セグメント取得済み (>100KB, {form})",
                  files[0].stat().st_size > 100_000, files[0].stat().st_size)
            client.get(f"/api/download/{jid}", headers=H)
            time.sleep(0.5)
        # 最初の1形式だけ詳細検証すれば十分なので、タイトルは全形式で確認
        check(f"★ タイトルがページから抽出され generic で上書きされていない ({form})",
              job.get("title") == "Sample Movie 1080p", job.get("title"))
        break  # 詳細検証は /e/<code> で1回行い、他形式は下のループで軽く確認

    # 各 URL 形式がすべて解決できること (軽量に finished だけ確認)
    for form in [f"/e/{VALID}", f"/e/{VALID}.html", f"/{VALID}", f"/{VALID}.html", f"/d/{VALID}", f"/f/{VALID}"]:
        clean()
        r = client.post("/api/download", json={"url": base(form)}, headers=H)
        if r.status_code != 202:
            check(f"形式 {form} 受付", False, r.status_code)
            continue
        job = wait_job(client, r.json()["job"]["id"])
        check(f"形式 {form} でダウンロード成功", job["state"] == "finished",
              (job["state"], (job.get("error") or "")[:180]))
        clean()

    # ------------------------------------------------------------------
    print("\n--- 4) 失効ページ → 分かりやすいエラー ---")
    clean()
    r = client.post("/api/download", json={"url": base(f"/e/{EXPIRED}.html")}, headers=H)
    job = wait_job(client, r.json()["job"]["id"])
    check("失効 → error 状態", job["state"] == "error", job["state"])
    err = job.get("error") or ""
    check("エラー文に「失効」の案内がある", "失効" in err or "削除" in err, err[:200])
    check("120日の説明が含まれる", "120" in err, err[:250])
    check("残骸ファイルなし", list(TEST_DIR.iterdir()) == [], [p.name for p in TEST_DIR.iterdir()])

    # ------------------------------------------------------------------
    print("\n--- 5) m3u8 が無い (難読化) ページ → DevTools 案内 ---")
    clean()
    r = client.post("/api/download", json={"url": base(f"/e/{OBFUSC}.html")}, headers=H)
    job = wait_job(client, r.json()["job"]["id"])
    check("難読化 → error 状態", job["state"] == "error", job["state"])
    err = job.get("error") or ""
    check("エラー文に DevTools の手順がある", "DevTools" in err or "m3u8" in err, err[:250])
    check("エラー文に Referer の案内がある", "Referer" in err or "referer" in err.lower(), err[:250])

    # ------------------------------------------------------------------
    # (SSRF / リダイレクト検証は API 経由だと URL 自体が弾かれるため、
    #  下の 12-B) でリゾルバを直接呼ぶ単位テストとして行う)

    # ------------------------------------------------------------------
    print("\n--- 8) 対応外ホストは素通り (既存動作を壊さない) ---")
    check("YouTube URL はリゾルバが None を返す",
          resolvers.resolve_if_supported("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None)
    check("m3u8 直リンクも None (そのまま yt-dlp へ)",
          resolvers.resolve_if_supported("https://example.com/x/index.m3u8") is None)
    clean()
    # 実際のローカル HLS 直リンクが従来通り動くこと
    r = client.post("/api/download", json={"url": base(f"/hls/index.m3u8"),
                                           "referer": base(f"/e/{VALID}.html")}, headers=H)
    # 上記は 403 (referer に /e/ と VALID を含むので実際は通る)
    if r.status_code == 202:
        job = wait_job(client, r.json()["job"]["id"])
        check("m3u8 直リンク + Referer は従来通り動作", job["state"] == "finished",
              (job["state"], (job.get("error") or "")[:150]))
    clean()

    # ------------------------------------------------------------------
    print("\n--- 9) /api/info でも解決が効く ---")
    r = client.post("/api/info", json={"url": base(f"/e/{VALID}.html")}, headers=H)
    check("/api/info → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        info = r.json()
        check("resolved_url が返る (m3u8 に解決された)",
              bool(info.get("resolved_url")) and ".m3u8" in info["resolved_url"], info.get("resolved_url"))
    r = client.post("/api/info", json={"url": base(f"/e/{EXPIRED}.html")}, headers=H)
    check("失効ページの /api/info → 422", r.status_code == 422, r.status_code)
    check("失効理由が detail に入る", "失効" in (r.json().get("detail") or ""), r.json().get("detail", "")[:150])

    # ------------------------------------------------------------------
    print("\n--- 10) ENABLE_SITE_RESOLVERS=0 で無効化できる ---")
    main.ENABLE_SITE_RESOLVERS = False
    clean()
    r = client.post("/api/download", json={"url": base(f"/e/{VALID}.html")}, headers=H)
    job = wait_job(client, r.json()["job"]["id"])
    check("無効時はリゾルバを通らない (resolver=None)", not job.get("resolver"), job.get("resolver"))
    main.ENABLE_SITE_RESOLVERS = True
    clean()

    # ------------------------------------------------------------------
    print("\n--- 11) 回帰: 既存エンドポイント ---")
    check("/api/health 200", client.get("/api/health").status_code == 200)
    r = client.get("/api/presets", headers=H)
    check("/api/presets 200", r.status_code == 200)
    if r.status_code == 200:
        pr = r.json()
        check("presets に resolver_sites がある", "streamhg" in (pr.get("resolver_sites") or {}),
              pr.get("resolver_sites"))
        check("presets に resolvers_enabled がある", pr.get("resolvers_enabled") is True)
    html = client.get("/").text
    check("UI が 200 でレンダリングされる", "Personal Video Downloader" in html)

# ==========================================================================
print("\n--- 12) リゾルバ単体: HTML 解析の網羅性 ---")
patterns = [
    ('JWPlayer sources', 'sources: [{ file: "https://h.example.com/a/index.m3u8", type:"hls" }]'),
    ('file: 単独', 'file: "https://h.example.com/b/index.m3u8"'),
    ("<source> タグ", '<source src="https://h.example.com/c/index.m3u8" type="application/x-mpegURL">'),
    ('src: 形式', 'src: "https://h.example.com/d/index.m3u8"'),
    ('playlist: 形式', 'playlist: "https://h.example.com/e/index.m3u8"'),
    ("エスケープ済み \\/", 'file: "https:\\/\\/h.example.com\\/f\\/index.m3u8"'),
    ("HTMLエスケープ &amp;", 'file: "https://h.example.com/g/index.m3u8?a=1&amp;b=2"'),
    ("プロトコル相対 //", 'file: "//h.example.com/h/index.m3u8"'),
    ("mp4 直リンク", '<source src="https://h.example.com/i/movie.mp4">'),
]
for label, html in patterns:
    found = resolvers.extract_media_urls(html)
    ok = len(found) >= 1 and found[0][1].startswith("http")
    check(f"抽出: {label}", ok, found[:2])

check("抽出: m3u8 がクエリ付きでも取れる",
      any("token=abc" in u for _, u in resolvers.extract_media_urls(
          'file:"https://h.example.com/x/index.m3u8?token=abc&exp=1"')))
check("抽出: 何も無いページは空リスト", resolvers.extract_media_urls("<html><body>hi</body></html>") == [])

# 失効判定
check("失効判定: 本物の失効ページ (793B 相当)", resolvers.detect_gone(EXPIRED_PAGE) is not None)
check("失効判定: 通常ページは None", resolvers.detect_gone(PAGE_TMPL.format(title="t", media="m")) is None)
big = "<html>" + ("x" * 6000) + "File is no longer available</html>"
check("失効判定: 大きなページは誤爆しない", resolvers.detect_gone(big) is None)

# タイトル抽出
check("タイトル抽出: <title> から装飾を除去",
      resolvers.extract_title("<title>Sample Movie 1080p - StreamHG</title>") == "Sample Movie 1080p",
      resolvers.extract_title("<title>Sample Movie 1080p - StreamHG</title>"))
check("タイトル抽出: サイト名だけなら None", resolvers.extract_title("<title>StreamHG</title>") is None)
check("タイトル抽出: og:title 対応",
      resolvers.extract_title('<meta property="og:title" content="My Video">') == "My Video")

# ヘッダインジェクション (リゾルバ経由でも安全であること)
check("_unescape: バックスラッシュ除去",
      resolvers._unescape("https:\\/\\/a.example\\/x.m3u8") == "https://a.example/x.m3u8",
      resolvers._unescape("https:\\/\\/a.example\\/x.m3u8"))

# ==========================================================================
print("\n--- 12-B) 単位テスト: リゾルバ経由の SSRF 防御 ---")


def strict_validate(u):
    """本番設定 (BLOCK_PRIVATE_HOSTS=1) 相当の validate_url。"""
    saved = main.BLOCK_PRIVATE_HOSTS
    main.BLOCK_PRIVATE_HOSTS = True
    try:
        return main.validate_url(u)
    finally:
        main.BLOCK_PRIVATE_HOSTS = saved


# 6) ページ内に内部IPへの m3u8 が仕込まれているケース
STATS["hits"].clear()
try:
    res = resolvers.resolve_streamhg(base(f"/e/{SSRFID}.html"), validate_url=strict_validate)
    check("内部IP を指す m3u8 は拒否される", False, f"解決できてしまった: {res.media_url}")
except resolvers.ResolveError as exc:
    msg = str(exc)
    check("内部IP を指す m3u8 は拒否される", True)
    check("拒否理由がメッセージに含まれる",
          ("セキュリティ検査" in msg) or ("プライベート" in msg) or ("内部" in msg), msg[:200])
except Exception as exc:
    check("内部IP を指す m3u8 は拒否される", False, f"想定外の例外 {type(exc).__name__}: {exc}")
check("169.254.169.254 へアクセスしていない",
      not any("169.254" in h for h in STATS["hits"]), STATS["hits"][:6])

# 7) リダイレクトで内部IPへ飛ばすケース
try:
    resolvers.resolve_streamhg(base(f"/e/{REDIRECT}.html"), validate_url=strict_validate)
    check("リダイレクト先の内部IPは拒否される", False, "解決できてしまった")
except resolvers.ResolveError as exc:
    check("リダイレクト先の内部IPは拒否される", True)
except main.ValidationError as exc:
    check("リダイレクト先の内部IPは拒否される", True)
except Exception as exc:
    check("リダイレクト先の内部IPは拒否される", False, f"{type(exc).__name__}: {exc}")

# 通常の URL は strict_validate でも通ること (誤検知がないこと)
check("本番設定相当でも通常URLは受理される",
      strict_validate("https://iplayerhls.com/e/abc123def456") == "https://iplayerhls.com/e/abc123def456")
check("strict_validate: file:// は拒否",
      _raises(lambda: strict_validate("file:///etc/passwd")))
check("strict_validate: localhost m3u8 は拒否",
      _raises(lambda: strict_validate("http://127.0.0.1/x.m3u8")))

srv.shutdown()

print("\n" + "=" * 62)
print(f"  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  失敗項目:")
    for f in FAIL:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
