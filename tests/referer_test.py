"""
Referer / User-Agent 透過機能のテスト (StreamHG 等のドメインロック対策)。

「正しい Referer が無いと 403 を返す」サーバを立てて、
  1) Referer なし      → 失敗する
  2) Referer あり      → m3u8 と全セグメント(.ts)を取得して mp4 化できる
  3) ヘッダインジェクション / 不正スキーム / 長大値 → 400 で拒否
  4) User-Agent が透過される
  5) /api/info でも Referer が効く
を検証する。

実行: python tests/referer_test.py
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
    print("HLS テストメディアがありません。以下で生成してください:")
    print('  ffmpeg -i full.mp4 -c copy -f hls -hls_time 2 -hls_list_size 0 \\')
    print("         -hls_segment_filename 'seg%03d.ts' /tmp/hls/index.m3u8")
    sys.exit(2)

TEST_DIR = Path("/tmp/vdl-ref")
if TEST_DIR.exists():
    import shutil as _sh
    _sh.rmtree(TEST_DIR)
TEST_DIR.mkdir(parents=True)

os.environ.update({
    "AUTH_TOKEN": "t",
    "DOWNLOAD_DIR": str(TEST_DIR),
    "BLOCK_PRIVATE_HOSTS": "0",
    "MIN_FREE_DISK_MB": "1",
    "FILE_TTL_SECONDS": "300",
    "REAPER_INTERVAL": "30",
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ==========================================================================
# ドメインロックを模した HLS サーバ
#   ALLOWED_REFERER を持つリクエストだけ通す。それ以外は 403。
#   受信した User-Agent を記録して透過を検証する。
# ==========================================================================
ALLOWED_REFERER = "https://allowed-site.example/embed/abc123"
SEEN_UAS = []
REQUEST_COUNT = {"ok": 0, "denied": 0}


class LockedHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HLS_DIR), **kw)

    def _check(self):
        ref = self.headers.get("Referer")
        SEEN_UAS.append(self.headers.get("User-Agent") or "")
        if ref == ALLOWED_REFERER:
            REQUEST_COUNT["ok"] += 1
            return True
        REQUEST_COUNT["denied"] += 1
        self.send_error(403, "Forbidden: referer required")
        return False

    def do_GET(self):
        if self._check():
            super().do_GET()

    def do_HEAD(self):
        if self._check():
            super().do_HEAD()

    def log_message(self, *a):
        pass


class LockedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, *a):
        pass


srv = LockedServer(("127.0.0.1", 0), LockedHandler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
M3U8 = f"http://127.0.0.1:{PORT}/index.m3u8"

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'  ok   ' if cond else ' FAIL '} | {name}" + (f" :: {extra}" if not cond and extra else ""))


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


with TestClient(main.app) as client:
    H = {"X-API-Key": "t"}

    # ---------------------------------------------------------------
    print("\n--- 1) Referer なし → 403 で失敗する ---")
    clean()
    r = client.post("/api/download", json={"url": M3U8, "quality": "best"}, headers=H)
    check("ジョブ受付 202", r.status_code == 202, r.text[:200])
    job = wait_job(client, r.json()["job"]["id"])
    check("Referer なし → error", job["state"] == "error", job["state"])
    check("拒否された回数が記録されている", REQUEST_COUNT["denied"] > 0, REQUEST_COUNT)
    check("残骸ファイルが無い", list(TEST_DIR.iterdir()) == [], [p.name for p in TEST_DIR.iterdir()])

    # ---------------------------------------------------------------
    print("\n--- 2) Referer あり → m3u8 と全セグメントを取得できる ---")
    clean()
    before = dict(REQUEST_COUNT)
    r = client.post("/api/download",
                    json={"url": M3U8, "quality": "best", "referer": ALLOWED_REFERER},
                    headers=H)
    check("ジョブ受付 202", r.status_code == 202, r.text[:200])
    jid = r.json()["job"]["id"]
    job = wait_job(client, jid)
    check("★ Referer 透過で finished になった", job["state"] == "finished",
          (job["state"], (job.get("error") or "")[:200]))

    if job["state"] == "finished":
        files = sorted(TEST_DIR.iterdir())
        check("成果物が 1 ファイル", len(files) == 1, [p.name for p in files])
        out = files[0]
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(out)],
            capture_output=True, text=True)
        kinds = [s.get("codec_type") for s in json.loads(probe.stdout or "{}").get("streams", [])]
        check("映像+音声が揃った mp4 になった", "video" in kinds and "audio" in kinds, kinds)
        check("全セグメントが結合されている (>100KB)", out.stat().st_size > 100_000, out.stat().st_size)
        check("セグメント(.ts)のリクエストも Referer 付きで通った",
              REQUEST_COUNT["ok"] - before["ok"] >= 5,   # m3u8 x N + ts x 4
              REQUEST_COUNT["ok"] - before["ok"])
        check("403 拒否が増えていない", REQUEST_COUNT["denied"] == before["denied"],
              (REQUEST_COUNT["denied"], before["denied"]))
        check("public() に referer が含まれる", job.get("referer") == ALLOWED_REFERER, job.get("referer"))
        rr = client.get(f"/api/download/{jid}", headers=H)
        check("クライアントへ送出 200", rr.status_code == 200, rr.status_code)
        time.sleep(0.6)
        check("送出後に自動削除", list(TEST_DIR.iterdir()) == [], [p.name for p in TEST_DIR.iterdir()])

    # ---------------------------------------------------------------
    print("\n--- 3) Referer のバリデーション (ヘッダインジェクション対策) ---")
    bad = [
        ("https://ok.example/x\r\nX-Injected: 1", "CRLF インジェクション"),
        ("https://ok.example/x\nSet-Cookie: a=b", "LF インジェクション"),
        ("https://ok.example/\x00x", "NUL 文字"),
        ("file:///etc/passwd", "file スキーム"),
        ("javascript:alert(1)", "javascript スキーム"),
        ("not a url", "ホスト名なし"),
        ("https://ok.example/" + "a" * 3000, "長すぎる値"),
    ]
    for value, label in bad:
        r = client.post("/api/download", json={"url": M3U8, "referer": value}, headers=H)
        check(f"referer 拒否: {label} → 4xx", 400 <= r.status_code < 500,
              f"{r.status_code}: {r.text[:120]}")

    # user_agent も同様に検証される
    r = client.post("/api/download",
                    json={"url": M3U8, "user_agent": "Mozilla/5.0\r\nX-Evil: 1"}, headers=H)
    check("user_agent の CRLF も拒否", 400 <= r.status_code < 500, r.status_code)
    r = client.post("/api/download", json={"url": M3U8, "user_agent": "U" * 900}, headers=H)
    check("user_agent の長さ超過も拒否", 400 <= r.status_code < 500, r.status_code)

    # 空文字 / null は「指定なし」として扱う (エラーにしない)
    for empty in ["", "   ", None]:
        r = client.post("/api/download", json={"url": M3U8, "referer": empty}, headers=H)
        check(f"referer={empty!r} は無視されて 202", r.status_code == 202, r.status_code)
        if r.status_code == 202:
            wait_job(client, r.json()["job"]["id"], timeout=60)
        clean()

    # ---------------------------------------------------------------
    print("\n--- 4) User-Agent が透過される ---")
    clean()
    SEEN_UAS.clear()
    CUSTOM_UA = "MyCustomAgent/9.9 (test)"
    r = client.post("/api/download",
                    json={"url": M3U8, "referer": ALLOWED_REFERER, "user_agent": CUSTOM_UA},
                    headers=H)
    job = wait_job(client, r.json()["job"]["id"])
    check("UA 指定でも finished", job["state"] == "finished", (job["state"], (job.get("error") or "")[:150]))
    check("★ サーバがカスタム UA を受信した", any(CUSTOM_UA in ua for ua in SEEN_UAS),
          sorted(set(SEEN_UAS))[:3])
    clean()

    # ---------------------------------------------------------------
    print("\n--- 5) /api/info でも Referer が効く ---")
    r = client.post("/api/info", json={"url": M3U8}, headers=H)
    check("Referer なしの /api/info は失敗 (4xx/5xx)", r.status_code >= 400, r.status_code)
    r = client.post("/api/info", json={"url": M3U8, "referer": ALLOWED_REFERER}, headers=H)
    check("★ Referer 付きの /api/info は 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    if r.status_code == 200:
        info = r.json()
        check("info にフォーマット情報がある", bool(info.get("formats")) or bool(info.get("title")),
              list(info.keys()))

    # ---------------------------------------------------------------
    print("\n--- 6) 既存機能が壊れていないこと (回帰) ---")
    r = client.get("/api/health")
    check("/api/health 200", r.status_code == 200)
    r = client.get("/")
    check("GET / 200", r.status_code == 200)
    check("UI に referer 入力欄がある", 'id="referer"' in r.text or "referer" in r.text.lower())
    r = client.get("/api/presets", headers=H)
    check("/api/presets 200", r.status_code == 200)

srv.shutdown()

print("\n" + "=" * 62)
print(f"  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  失敗項目:")
    for f in FAIL:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
