"""
main.py の統合スモークテスト。

ローカル HTTP サーバに置いた sample.mp4 を yt-dlp(generic extractor) に
ダウンロードさせ、認証 → ジョブ作成 → 進捗ポーリング → ファイル取得 →
自動削除 → リーパー、までを通しで確認する。

実行:  python tests/smoke_test.py
"""
import functools
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path

# ---- テスト用環境変数は main を import する前に設定する -------------------
TEST_DIR = Path("/tmp/vdl-test")
if TEST_DIR.exists():
    import shutil as _sh
    _sh.rmtree(TEST_DIR)
TEST_DIR.mkdir(parents=True)

SERVE_DIR = Path("/tmp/vdl-serve")
SERVE_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE = SERVE_DIR / "sample.mp4"
SAMPLE.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(300_000))  # 疑似MP4
SAMPLE_SIZE = SAMPLE.stat().st_size

os.environ.update({
    "AUTH_TOKEN": "test-secret-token",
    "DOWNLOAD_DIR": str(TEST_DIR),
    "MAX_CONCURRENT": "2",
    "FILE_TTL_SECONDS": "60",
    "REAPER_INTERVAL": "5",
    "JOB_TTL_SECONDS": "3600",
    "BLOCK_PRIVATE_HOSTS": "0",   # ローカルHTTPサーバにアクセスさせるため
    "MIN_FREE_DISK_MB": "1",
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- ローカル静的ファイルサーバ -----------------------------------------
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SERVE_DIR))


class QuietServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


httpd = QuietServer(("127.0.0.1", 0), Handler)
HTTP_PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE_VIDEO_URL = f"http://127.0.0.1:{HTTP_PORT}/sample.mp4"

# ---- 本体 import --------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'  ok  ' if cond else ' FAIL '} | {name}{(' :: ' + str(extra)) if extra and not cond else ''}")


with TestClient(main.app) as client:
    H = {"X-API-Key": "test-secret-token"}

    # ---------- 1. 静的ページ / ヘルス ----------
    r = client.get("/")
    check("GET / が 200", r.status_code == 200, r.status_code)
    check("GET / に HTML が含まれる", "<!DOCTYPE html>" in r.text and "startBtn" in r.text)
    check("品質プリセットがテンプレートに展開されている", "最高画質" in r.text)

    r = client.get("/api/health")
    check("GET /api/health が認証不要で 200", r.status_code == 200, r.status_code)
    check("health が ffmpeg の有無を返す", "ffmpeg" in r.json())

    # ---------- 2. 認証 ----------
    r = client.post("/api/download", json={"url": BASE_VIDEO_URL})
    check("トークンなし → 401", r.status_code == 401, r.status_code)

    r = client.post("/api/download", json={"url": BASE_VIDEO_URL},
                    headers={"X-API-Key": "wrong-token"})
    check("誤トークン → 403", r.status_code == 403, r.status_code)

    r = client.get("/api/jobs", headers={"Authorization": "Bearer test-secret-token"})
    check("Authorization: Bearer でも通る", r.status_code == 200, r.status_code)

    r = client.get("/api/jobs?token=test-secret-token")
    check("クエリ ?token= でも通る", r.status_code == 200, r.status_code)

    # ---------- 3. URL バリデーション (SSRF / スキーム) ----------
    bad_urls = [
        ("file:///etc/passwd", "file スキーム拒否"),
        ("gopher://127.0.0.1:6379/_INFO", "gopher スキーム拒否"),
        ("http://user:pass@example.com/x", "URL内認証情報の拒否"),
        ("not a url", "不正な URL の拒否"),
        ("", "空 URL の拒否"),
    ]
    for url, label in bad_urls:
        r = client.post("/api/download", json={"url": url}, headers=H)
        check(f"{label} → 4xx", 400 <= r.status_code < 500, f"{r.status_code}: {r.text[:120]}")

    # BLOCK_PRIVATE_HOSTS=1 のケースを個別に検証
    os.environ["BLOCK_PRIVATE_HOSTS"] = "1"
    main.BLOCK_PRIVATE_HOSTS = True
    for url, label in [
        ("http://127.0.0.1/x.mp4", "loopback IP 拒否"),
        ("http://localhost/x.mp4", "localhost 拒否"),
        ("http://169.254.169.254/latest/meta-data/", "クラウドメタデータ拒否"),
        ("http://192.168.1.1/router.mp4", "プライベートIP拒否"),
        ("http://[::1]/x.mp4", "IPv6 loopback 拒否"),
        ("http://foo.internal/x.mp4", ".internal 拒否"),
    ]:
        r = client.post("/api/download", json={"url": url}, headers=H)
        check(f"SSRF対策: {label}", r.status_code == 400, f"{r.status_code}: {r.text[:150]}")
    main.BLOCK_PRIVATE_HOSTS = False
    os.environ["BLOCK_PRIVATE_HOSTS"] = "0"

    # ---------- 4. 正常系: ジョブ作成 → 完了 → 取得 → 自動削除 ----------
    r = client.post("/api/download", json={"url": BASE_VIDEO_URL, "quality": "best"}, headers=H)
    check("POST /api/download → 202", r.status_code == 202, f"{r.status_code}: {r.text[:200]}")
    job = r.json()["job"]
    job_id = job["id"]
    check("job_id が UUID 形式", len(job_id) == 36, job_id)
    check("初期状態が queued/downloading", job["state"] in ("queued", "downloading"), job["state"])
    check("レスポンスに filepath が含まれない (情報漏えい防止)",
          "filepath" not in json.dumps(job), list(job.keys()))

    # 進捗ポーリング
    deadline = time.time() + 60
    final = None
    seen_states, seen_percent = set(), []
    while time.time() < deadline:
        r = client.get(f"/api/status/{job_id}", headers=H)
        if r.status_code != 200:
            break
        final = r.json()["job"]
        seen_states.add(final["state"])
        seen_percent.append(final["percent"])
        if final["state"] in ("finished", "error"):
            break
        time.sleep(0.3)

    check("status が取得できる", final is not None)
    check("ジョブが finished になった", final and final["state"] == "finished",
          final and (final["state"], final.get("error")))
    check("進捗が 100% に到達", final and final["percent"] == 100.0, final and final["percent"])
    check("progress_hooks で中間進捗を観測", len(set(seen_percent)) > 0)
    check("タイトルが取得できている", bool(final and final.get("title")), final and final.get("title"))
    check("filesize が記録されている", bool(final and final.get("filesize")), final and final.get("filesize"))
    check("download_path が返る", final and final.get("download_path") == f"/api/download/{job_id}")

    files_before = list(TEST_DIR.iterdir())
    check("実ファイルが DOWNLOAD_DIR に生成された", len(files_before) == 1,
          [p.name for p in files_before])
    check("ファイル名が job_id で始まる (パストラバーサル対策)",
          all(p.name.startswith(job_id) for p in files_before), [p.name for p in files_before])

    # ダウンロード
    r = client.get(f"/api/download/{job_id}", headers=H)
    check("GET /api/download → 200", r.status_code == 200, r.status_code)
    check("ファイルサイズが一致", len(r.content) == SAMPLE_SIZE, (len(r.content), SAMPLE_SIZE))
    check("Content-Disposition が attachment",
          "attachment" in r.headers.get("content-disposition", ""), r.headers.get("content-disposition"))
    check("no-store ヘッダが付与", "no-store" in r.headers.get("cache-control", ""))

    time.sleep(0.6)  # BackgroundTask の完了を待つ
    check("★ 取得直後にサーバ上から自動削除された", list(TEST_DIR.iterdir()) == [],
          [p.name for p in TEST_DIR.iterdir()])

    r = client.get(f"/api/download/{job_id}", headers=H)
    check("削除後の再取得 → 410 Gone", r.status_code == 410, r.status_code)

    # ---------- 5. job_id のバリデーション ----------
    for bad_id, label in [
        ("../../etc/passwd", "パストラバーサル"),
        ("../main.py", "親ディレクトリ参照"),
        ("not-a-uuid", "不正フォーマット"),
        ("%2e%2e%2f%2e%2e%2fetc%2fpasswd", "URLエンコード済みトラバーサル"),
    ]:
        r = client.get(f"/api/status/{bad_id}", headers=H)
        check(f"job_id バリデーション: {label} → 404", r.status_code == 404, r.status_code)
        r = client.get(f"/api/download/{bad_id}", headers=H)
        check(f"download の job_id 検証: {label} → 404", r.status_code == 404, r.status_code)

    # ---------- 6. エラー系: 存在しないURL / 404 ----------
    r = client.post("/api/download",
                    json={"url": f"http://127.0.0.1:{HTTP_PORT}/does-not-exist.mp4"},
                    headers=H)
    check("404動画でもジョブは作成される (202)", r.status_code == 202, r.status_code)
    err_id = r.json()["job"]["id"]
    deadline = time.time() + 60
    err_job = None
    while time.time() < deadline:
        err_job = client.get(f"/api/status/{err_id}", headers=H).json()["job"]
        if err_job["state"] in ("finished", "error"):
            break
        time.sleep(0.3)
    check("存在しないURL → error 状態", err_job and err_job["state"] == "error", err_job)
    check("エラーメッセージが日本語で返る",
          bool(err_job and err_job.get("error")) and any(
              c >= "\u3040" for c in err_job["error"]), err_job and err_job.get("error"))
    check("エラー時に残骸ファイルが残っていない", list(TEST_DIR.iterdir()) == [],
          [p.name for p in TEST_DIR.iterdir()])

    r = client.get(f"/api/download/{err_id}", headers=H)
    check("error ジョブの download → 409", r.status_code == 409, r.status_code)

    # ---------- 7. 一覧 / 削除 ----------
    r = client.get("/api/jobs", headers=H)
    check("GET /api/jobs → 200", r.status_code == 200)
    ids = [j["id"] for j in r.json()["jobs"]]
    check("履歴にジョブが含まれる", job_id in ids and err_id in ids, ids[:5])
    check("一覧に filepath が漏れない", "filepath" not in r.text)

    r = client.delete(f"/api/jobs/{err_id}", headers=H)
    check("DELETE /api/jobs/{id} → 200", r.status_code == 200, r.status_code)
    r = client.get(f"/api/status/{err_id}", headers=H)
    check("削除済みジョブ → 404", r.status_code == 404, r.status_code)

    # ---------- 8. リーパー: TTL 超過ファイルの自動削除 ----------
    orphan = TEST_DIR / "orphan-file.mp4"
    orphan.write_bytes(b"x" * 10)
    old = time.time() - 3600  # FILE_TTL を大幅に超えた mtime
    os.utime(orphan, (old, old))
    part = TEST_DIR / "leftover.mp4.part"
    part.write_bytes(b"y" * 10)
    os.utime(part, (old, old))

    check("リーパー待ち: 孤立ファイルが存在する", orphan.exists() and part.exists())
    deadline = time.time() + 30
    while time.time() < deadline and (orphan.exists() or part.exists()):
        time.sleep(0.5)
    check("★ リーパーが孤立ファイルを自動削除", not orphan.exists(), "orphan残存")
    check("★ リーパーが古い .part を自動削除", not part.exists(), ".part残存")

    # ---------- 9. 完了ファイルの TTL 削除 ----------
    r = client.post("/api/download", json={"url": BASE_VIDEO_URL}, headers=H)
    ttl_id = r.json()["job"]["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        j = client.get(f"/api/status/{ttl_id}", headers=H).json()["job"]
        if j["state"] in ("finished", "error"):
            break
        time.sleep(0.3)
    check("TTL検証用ジョブが完了", j["state"] == "finished", j.get("error"))
    generated = [p for p in TEST_DIR.iterdir() if p.name.startswith(ttl_id)]
    check("TTL検証用ファイルが生成された", len(generated) == 1, [p.name for p in generated])
    # TTL は「完了時刻 (finished_at)」を基準に判定されるので、それを過去に巻き戻す
    with main.JOBS_LOCK:
        ttl_job = main.JOBS[ttl_id]
    with ttl_job._lock:
        ttl_job.finished_at = time.time() - (main.FILE_TTL_SECONDS + 30)
    deadline = time.time() + 30
    while time.time() < deadline and any(p.exists() for p in generated):
        time.sleep(0.5)
    check("★ TTL 超過の未取得ファイルをリーパーが削除", all(not p.exists() for p in generated),
          [p.name for p in generated if p.exists()])
    r = client.get(f"/api/download/{ttl_id}", headers=H)
    check("削除後の取得 → 410", r.status_code == 410, r.status_code)
    r = client.get(f"/api/status/{ttl_id}", headers=H).json()["job"]
    check("TTL削除後は download_path が消える", r.get("download_path") is None, r.get("download_path"))
    check("TTL削除後の stage が更新される", "自動削除" in (r.get("stage") or ""), r.get("stage"))

    # ---------- 10. SSE ストリーム ----------
    r = client.post("/api/download", json={"url": BASE_VIDEO_URL}, headers=H)
    sse_id = r.json()["job"]["id"]
    with client.stream("GET", f"/api/stream/{sse_id}?token=test-secret-token") as resp:
        check("SSE エンドポイント 200", resp.status_code == 200, resp.status_code)
        check("SSE の Content-Type", "text/event-stream" in resp.headers.get("content-type", ""),
              resp.headers.get("content-type"))
        chunks = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                chunks.append(json.loads(line[5:].strip()))
            if len(chunks) >= 2 or (chunks and chunks[-1]["state"] in ("finished", "error")):
                break
        check("SSE で進捗イベントを受信", len(chunks) >= 1, len(chunks))
        check("SSE のペイロードに id が入っている", chunks and chunks[0]["id"] == sse_id)

    # ---------- 11. 同時実行制御 ----------
    main.MAX_CONCURRENT = 1
    main._download_semaphore = threading.Semaphore(1)
    ids2 = []
    for _ in range(3):
        rr = client.post("/api/download", json={"url": BASE_VIDEO_URL}, headers=H)
        ids2.append(rr.json()["job"]["id"])
    running = 0
    for i in ids2:
        st = client.get(f"/api/status/{i}", headers=H).json()["job"]["state"]
        running += 1 if st in ("downloading", "processing") else 0
    check("同時実行数がセマフォで制限されている (<=1 が downloading)", running <= 1, running)
    deadline = time.time() + 90
    while time.time() < deadline:
        states = [client.get(f"/api/status/{i}", headers=H).json()["job"]["state"] for i in ids2]
        if all(s in ("finished", "error") for s in states):
            break
        time.sleep(0.5)
    check("直列化しても全ジョブが完了", all(s == "finished" for s in states), states)

    # ---------- 12. /api/info ----------
    r = client.post("/api/info", json={"url": BASE_VIDEO_URL}, headers=H)
    check("POST /api/info → 200", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
    r = client.post("/api/info", json={"url": "file:///etc/passwd"}, headers=H)
    check("POST /api/info も file:// を拒否", r.status_code == 400, r.status_code)

    # ---------- 13. safe_filename ----------
    cases = [
        ("../../etc/passwd", "passwd 等の安全な名前"),
        ('a"b<c>d|e:f*g?h', "危険文字の除去"),
        ("normal_video-title.mp4", "通常のファイル名は維持"),
        ("", "空文字はフォールバック"),
        ("x" * 500, "長すぎる名前は切り詰め"),
    ]
    for raw, label in cases:
        out = main.safe_filename(raw, fallback="download")
        ok = ("/" not in out and "\\" not in out and ".." not in out
              and '"' not in out and len(out) <= 180 and out)
        check(f"safe_filename: {label}", ok, f"{raw!r} -> {out!r}")

httpd.shutdown()

print("\n" + "=" * 62)
print(f"  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  失敗項目:")
    for f in FAIL:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
