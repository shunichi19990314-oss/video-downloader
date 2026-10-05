"""
ffmpeg 依存経路の統合テスト。

A) 映像+音声のマージ        … 専用テスト用 extractor を注入し、
                              video-only / audio-only の 2 フォーマットを返させて
                              ffmpeg マージを確定的に発生させる
B) 音声のみ抽出 → mp3
C) プレイリスト一括取得 → ZIP 化
D) 抽出元ホワイトリスト (ALLOWED_EXTRACTORS)
E) プレイリスト無効時の挙動
F) マージ中間ファイル (.fXXX.mp4) が成果物に混ざらないこと

実行: python tests/ffmpeg_test.py
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
import zipfile
from pathlib import Path

# ---- ffmpeg を PATH に追加 (static_ffmpeg があればそれを使用) -------------
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

if not __import__("shutil").which("ffmpeg"):
    print("ffmpeg が見つかりません。このテストはスキップします。")
    sys.exit(0)

SERVE_DIR = Path("/tmp/vdl-serve2")
for need in ("vid.mp4", "aud.m4a", "full.mp4"):
    if not (SERVE_DIR / need).exists():
        print(f"テスト用メディア {need} がありません。生成してから実行してください。")
        sys.exit(2)

TEST_DIR = Path("/tmp/vdl-test2")
if TEST_DIR.exists():
    import shutil as _sh
    _sh.rmtree(TEST_DIR)
TEST_DIR.mkdir(parents=True)

os.environ.update({
    "AUTH_TOKEN": "test-secret-token",
    "DOWNLOAD_DIR": str(TEST_DIR),
    "MAX_CONCURRENT": "2",
    "FILE_TTL_SECONDS": "300",
    "REAPER_INTERVAL": "30",
    "BLOCK_PRIVATE_HOSTS": "0",
    "MIN_FREE_DISK_MB": "1",
    "ALLOW_PLAYLIST": "1",
    # テスト用 extractor のスキームを許可 (本番では http,https のみ)
    "ALLOWED_SCHEMES": "http,https,localmerge",
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ==========================================================================
# テスト用の静的ファイルサーバ
# ==========================================================================
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SERVE_DIR))


class QuietServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


httpd = QuietServer(("127.0.0.1", 0), Handler)
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

# ==========================================================================
# テスト用 extractor を注入 (映像と音声を別フォーマットで返し、マージさせる)
# ==========================================================================
import yt_dlp                                    # noqa: E402
from yt_dlp.extractor.common import InfoExtractor  # noqa: E402


class LocalMergeTestIE(InfoExtractor):
    """localmerge:<id> を受け取り、video-only + audio-only の 2 形式を返す。"""

    IE_NAME = "localmerge"
    _VALID_URL = r"localmerge://(?P<id>[\w-]+)"

    def _real_extract(self, url):
        video_id = self._match_valid_url(url).group("id")
        vid_size = (SERVE_DIR / "vid.mp4").stat().st_size
        aud_size = (SERVE_DIR / "aud.m4a").stat().st_size
        return {
            "id": video_id,
            "title": "Merge Test Video",
            "uploader": "Test Channel",
            "duration": 2,
            "extractor_key": "LocalMergeTest",
            "formats": [
                {   # 映像のみ (音声トラックなし) → マージが必要になる
                    "format_id": "v1", "url": f"{BASE}/vid.mp4", "ext": "mp4",
                    "vcodec": "h264", "acodec": "none",
                    "width": 320, "height": 240, "filesize": vid_size,
                },
                {   # 音声のみ
                    "format_id": "a1", "url": f"{BASE}/aud.m4a", "ext": "m4a",
                    "vcodec": "none", "acodec": "aac", "filesize": aud_size,
                },
            ],
        }


_orig_add_default = yt_dlp.YoutubeDL.add_default_info_extractors


def _patched_add_default(self):
    """既定の extractor を読み込んだ後、テスト用 IE を *先頭* に挿入する。
    (末尾に追加すると、ほぼ全 URL にマッチする generic extractor が先に拾ってしまう)"""
    _orig_add_default(self)
    ie = LocalMergeTestIE()
    key = ie.ie_key()
    self.add_info_extractor(ie)
    self._ies_instances[key] = ie
    rest = [(k, v) for k, v in self._ies.items() if k != key]
    self._ies.clear()
    self._ies[key] = ie
    for k, v in rest:
        self._ies[k] = v


yt_dlp.YoutubeDL.add_default_info_extractors = _patched_add_default

# ==========================================================================
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'  ok  ' if cond else ' FAIL '} | {name}{(' :: ' + str(extra)) if extra and not cond else ''}")


def ffprobe_streams(path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    streams = json.loads(out.stdout or "{}").get("streams", [])
    return [s.get("codec_type") for s in streams], streams


OBSERVED_STATES = {}


def wait_job(client, job_id, headers, timeout=180, interval=0.1):
    """完了までポーリングし、途中で見えた state を OBSERVED_STATES に記録する。"""
    deadline = time.time() + timeout
    job = None
    seen = set()
    while time.time() < deadline:
        job = client.get(f"/api/status/{job_id}", headers=headers).json()["job"]
        seen.add(job["state"])
        if job["state"] in ("finished", "error"):
            break
        time.sleep(interval)
    OBSERVED_STATES[job_id] = seen
    return job


def submit(client, headers, url, quality="best", playlist=False):
    r = client.post("/api/download",
                    json={"url": url, "quality": quality, "playlist": playlist},
                    headers=headers)
    assert r.status_code == 202, f"{r.status_code}: {r.text[:300]}"
    return r.json()["job"]["id"]


with TestClient(main.app) as client:
    H = {"X-API-Key": "test-secret-token"}

    # ======================================================================
    print("\n--- A) 映像+音声のマージ (ffmpeg) ---")
    jid = submit(client, H, "localmerge://abc123", quality="best")
    job = wait_job(client, jid, H, interval=0.004)
    check("A: finished になった", job["state"] == "finished", (job["state"], job.get("error")))

    files = sorted(TEST_DIR.iterdir())
    check("A: 成果物は 1 ファイルのみ (中間ファイルなし)", len(files) == 1, [p.name for p in files])
    out = files[0] if files else None
    if out:
        check("A: 拡張子が .mp4 (merge_output_format)", out.suffix.lower() == ".mp4", out.name)
        check("A: ファイル名が job_id 始まり", out.name.startswith(jid), out.name)
        kinds, streams = ffprobe_streams(out)
        check("A: 映像+音声が 1 ファイルに結合されている",
              "video" in kinds and "audio" in kinds, kinds)
        check("A: 映像 codec = h264",
              any(s.get("codec_name") == "h264" for s in streams), streams)
        check("A: 音声 codec = aac",
              any(s.get("codec_name") == "aac" for s in streams), streams)
        size_before = out.stat().st_size
        check("A: マージ後サイズ > 映像単体", size_before > (SERVE_DIR / "vid.mp4").stat().st_size,
              size_before)
        check("A: メタ情報 (uploader) が反映", job.get("uploader") == "Test Channel", job.get("uploader"))
        check("A: filesize 記録あり", job.get("filesize") == size_before, (job.get("filesize"), size_before))
        check("A: ffmpeg マージ中に processing 状態へ遷移した",
              "processing" in OBSERVED_STATES.get(jid, set()), OBSERVED_STATES.get(jid))
        check("A: downloading 状態も観測された",
              "downloading" in OBSERVED_STATES.get(jid, set()), OBSERVED_STATES.get(jid))

        r = client.get(f"/api/download/{jid}", headers=H)
        check("A: ダウンロード 200", r.status_code == 200, r.status_code)
        check("A: バイト一致", len(r.content) == size_before, (len(r.content), size_before))
        check("A: content-type が video/mp4", "video" in r.headers.get("content-type", ""),
              r.headers.get("content-type"))
        time.sleep(0.6)
        check("A: ★ 取得後に自動削除", not out.exists())
        check("A: ★ ディレクトリが完全に空", list(TEST_DIR.iterdir()) == [],
              [p.name for p in TEST_DIR.iterdir()])

    # ======================================================================
    print("\n--- B) 音声のみ抽出 (mp3) ---")
    jid_b = submit(client, H, f"{BASE}/full.mp4", quality="audio")
    job = wait_job(client, jid_b, H)
    check("B: finished になった", job["state"] == "finished", (job["state"], job.get("error")))
    files = sorted(TEST_DIR.iterdir())
    check("B: 成果物は 1 ファイル", len(files) == 1, [p.name for p in files])
    if files:
        out = files[0]
        check("B: 拡張子が .mp3", out.suffix.lower() == ".mp3", out.name)
        kinds, streams = ffprobe_streams(out)
        check("B: 音声ストリームのみ", kinds == ["audio"], kinds)
        check("B: codec が mp3", streams and streams[0].get("codec_name") == "mp3", streams)
        check("B: audio_only フラグ", job.get("audio_only") is True, job.get("audio_only"))
        sz = out.stat().st_size
        r = client.get(f"/api/download/{jid_b}", headers=H)
        check("B: ダウンロード 200 / バイト一致",
              r.status_code == 200 and len(r.content) == sz, r.status_code)
        check("B: content-type が audio", "audio" in r.headers.get("content-type", ""),
              r.headers.get("content-type"))
        time.sleep(0.6)
        check("B: ★ 取得後に自動削除", not out.exists())

    # ======================================================================
    print("\n--- C) プレイリスト → ZIP 化 ---")
    # generic extractor が「プレイリスト」と認識するよう、メディア要素を並べる
    (SERVE_DIR / "list.html").write_text(
        "<html><head><title>My Playlist</title></head><body>"
        + '<video controls src="vid.mp4"></video>'
        + '<audio controls src="aud.m4a"></audio>'
        + '<video controls src="full.mp4"></video>'
        + "</body></html>", encoding="utf-8")
    jid_c = submit(client, H, f"{BASE}/list.html", quality="720", playlist=True)
    job = wait_job(client, jid_c, H, timeout=240)
    check("C: finished になった", job["state"] == "finished", (job["state"], job.get("error")))
    files = sorted(TEST_DIR.iterdir())
    zips = [p for p in files if p.suffix.lower() == ".zip"]
    check("C: ZIP が 1 つ生成された", len(zips) == 1, [p.name for p in files])
    if zips:
        zpath = zips[0]
        check("C: ZIP 名が job_id 始まり", zpath.name.startswith(jid_c), zpath.name)
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            check("C: ZIP に複数エントリ", len(names) >= 2, names)
            check("C: 絶対パス/親参照を含まない",
                  all(not n.startswith("/") and ".." not in n and "\\" not in n for n in names), names)
            check("C: ZIP 整合性 OK", zf.testzip() is None)
            check("C: 中身が空でない",
                  all(zf.getinfo(n).file_size > 0 for n in names), names)
        check("C: ★ ZIP 化後に元ファイルは削除済み",
              all(p.suffix.lower() == ".zip" for p in files), [p.name for p in files])
        check("C: is_playlist = True", job.get("is_playlist") is True, job.get("is_playlist"))
        check("C: filename が .zip", (job.get("filename") or "").endswith(".zip"), job.get("filename"))
        sz = zpath.stat().st_size
        r = client.get(f"/api/download/{jid_c}", headers=H)
        check("C: ZIP ダウンロード 200", r.status_code == 200 and len(r.content) == sz, r.status_code)
        check("C: content-type が zip", "zip" in r.headers.get("content-type", ""),
              r.headers.get("content-type"))
        time.sleep(0.6)
        check("C: ★ 取得後に自動削除", not zpath.exists())
    (SERVE_DIR / "list.html").unlink(missing_ok=True)

    # ======================================================================
    print("\n--- D) ALLOWED_EXTRACTORS (yt-dlp ネイティブの許可リスト) ---")
    import fnmatch as _fm
    original_regex = list(main.ALLOWED_EXTRACTOR_REGEXES)
    original_names = list(main.ALLOWED_EXTRACTORS)

    # 本番では両方とも ALLOWED_EXTRACTORS 環境変数から生成される
    main.ALLOWED_EXTRACTORS = ["youtube"]
    main.ALLOWED_EXTRACTOR_REGEXES = [_fm.translate("youtube")]
    jid_d = None
    r = client.post("/api/download", json={"url": f"{BASE}/full.mp4"}, headers=H)
    if r.status_code == 202:
        jid_d = r.json()["job"]["id"]
        job = wait_job(client, jid_d, H, timeout=120)
        check("D: 許可外サイト → error", job["state"] == "error", job["state"])
        check("D: エラー文に許可リストの言及あり",
              "許可" in (job.get("error") or ""), (job.get("error") or "")[:160])
        check("D: ★ ダウンロードされず残骸ゼロ", list(TEST_DIR.iterdir()) == [],
              [p.name for p in TEST_DIR.iterdir()])
    else:
        check("D: 許可外サイトが拒否された", r.status_code in (400, 403, 422), r.status_code)

    main.ALLOWED_EXTRACTORS = ["generic*"]
    main.ALLOWED_EXTRACTOR_REGEXES = [_fm.translate("generic*")]
    jid_ok = submit(client, H, f"{BASE}/full.mp4")
    job = wait_job(client, jid_ok, H)
    check("D: ワイルドカード許可で finished", job["state"] == "finished", (job["state"], job.get("error")))
    client.get(f"/api/download/{jid_ok}", headers=H)
    main.ALLOWED_EXTRACTORS = original_names
    main.ALLOWED_EXTRACTOR_REGEXES = original_regex

    # ======================================================================
    print("\n--- E) プレイリスト無効時の挙動 ---")
    (SERVE_DIR / "list.html").write_text(
        "<html><head><title>Two Media</title></head><body>"
        '<video controls src="vid.mp4"></video>'
        '<audio controls src="aud.m4a"></audio>'
        "</body></html>", encoding="utf-8")
    main.ALLOW_PLAYLIST = False
    r = client.post("/api/download", json={"url": f"{BASE}/list.html", "playlist": True}, headers=H)
    check("E: ALLOW_PLAYLIST=0 + playlist=true → 400", r.status_code == 400, r.status_code)

    # playlist 未指定なら noplaylist=True で 1 件だけ取得される
    jid_e = submit(client, H, f"{BASE}/list.html", quality="720", playlist=False)
    job = wait_job(client, jid_e, H, timeout=180)
    check("E: 単体指定で finished", job["state"] == "finished", (job["state"], job.get("error")))
    files = sorted(TEST_DIR.iterdir())
    check("E: ★ 単体指定なら成果物は 1 件のみ", len(files) == 1, [p.name for p in files])
    check("E: ZIP 化されていない", all(p.suffix.lower() != ".zip" for p in files), [p.name for p in files])
    check("E: is_playlist=False", job.get("is_playlist") is False, job.get("is_playlist"))
    if files:
        client.get(f"/api/download/{jid_e}", headers=H)
        time.sleep(0.6)
    main.ALLOW_PLAYLIST = True
    (SERVE_DIR / "list.html").unlink(missing_ok=True)

    # ======================================================================
    print("\n--- F) 中間ファイル (.fXXX) の扱い ---")
    # マージ前の中間ファイルをわざと残し、成果物として拾われないことを確認する
    jid_f = submit(client, H, "localmerge://ff1", quality="best")
    job = wait_job(client, jid_f, H)
    check("F: マージジョブ完了", job["state"] == "finished", (job["state"], job.get("error")))
    fake_intermediate = TEST_DIR / f"{jid_f}.Merge Test Video.fv1.mp4"
    fake_intermediate.write_bytes(b"Z" * 999_999)   # 成果物より大きい偽の中間ファイル
    collected = main._collect_outputs(jid_f, allow_playlist=False)
    check("F: .fXXX.mp4 は成果物として扱われない",
          all(".fv1." not in p.name for p in collected), [p.name for p in collected])
    check("F: 本物の成果物だけが返る", len(collected) == 1, [p.name for p in collected])
    # 偽中間ファイルはリーパーが FILE_TTL 後に消す (孤立ファイル扱い)
    check("F: 偽中間ファイルは残るが TTL で回収対象", fake_intermediate.exists())
    r = client.get(f"/api/download/{jid_f}", headers=H)
    check("F: 正しい成果物が送出される", r.status_code == 200 and r.content[:1] != b"Z", r.status_code)

    # ==================================================================
    print("\n--- G) 状態遷移のユニット検証 ---")
    g = main.Job(id="00000000-0000-0000-0000-000000000000", url="u",
                 quality="best", audio_only=False, allow_playlist=False)
    g.state = main.JobState.DOWNLOADING
    g.percent = 100.0
    pp = main._make_postprocessor_hook(g)
    pp({"status": "started", "postprocessor": "Merger", "info_dict": {}})
    check("G: Merger 開始 → state=processing",
          g.state == main.JobState.PROCESSING, g.state)
    check("G: Merger の stage 文言", "マージ" in (g.stage or ""), g.stage)
    check("G: processing 時に percent=100", g.percent == 100.0, g.percent)

    g2 = main.Job(id="00000000-0000-0000-0000-000000000001", url="u",
                  quality="audio", audio_only=True, allow_playlist=False)
    pp2 = main._make_postprocessor_hook(g2)
    pp2({"status": "started", "postprocessor": "FFmpegExtractAudio", "info_dict": {}})
    check("G: 音声抽出 → state=processing", g2.state == main.JobState.PROCESSING, g2.state)
    check("G: 音声抽出の stage 文言", "変換" in (g2.stage or ""), g2.stage)

    # 進捗フック: パーセントが後退しないこと
    g3 = main.Job(id="00000000-0000-0000-0000-000000000002", url="u",
                  quality="best", audio_only=False, allow_playlist=False)
    ph = main._make_progress_hook(g3)
    ph({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100,
        "speed": 1048576, "eta": 5, "info_dict": {"title": "T"}})
    check("G: 進捗 50% が記録される", g3.percent == 50.0, g3.percent)
    check("G: フックからタイトルが反映される", g3.title == "T", g3.title)
    check("G: 速度表示が MB/s", "MB/s" in g3.speed_text, g3.speed_text)
    check("G: ETA 表示が整形される", g3.eta_text == "0:05", g3.eta_text)
    ph({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100, "info_dict": {}})
    check("G: 進捗が後退しない (max を維持)", g3.percent == 50.0, g3.percent)
    ph({"status": "finished", "info_dict": {}})
    check("G: finished で 100%", g3.percent == 100.0, g3.percent)
    ph({"status": "downloading", "downloaded_bytes": 0, "total_bytes": None, "info_dict": {}})
    check("G: total_bytes 欠損でも例外にならない", g3.percent == 100.0, g3.percent)

    # 中間ファイル判定の精度
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        real = tdp / "JOBID.full.mp4"; real.write_bytes(b"a" * 10)
        inter = tdp / "JOBID.Title.fv1.mp4"; inter.write_bytes(b"b" * 10)
        merged = tdp / "JOBID.Title.mp4"; merged.write_bytes(b"c" * 30)
        kept = main._filter_intermediates([real, inter, merged])
        names = sorted(x.name for x in kept)
        check("G: タイトルが f 始まりでも誤判定しない (JOBID.full.mp4 は残る)",
              "JOBID.full.mp4" in names, names)
        check("G: 本物の中間ファイルは除外される", "JOBID.Title.fv1.mp4" not in names, names)
        check("G: マージ済み成果物は残る", "JOBID.Title.mp4" in names, names)

httpd.shutdown()

print("\n" + "=" * 62)
print(f"  PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("  失敗項目:")
    for f in FAIL:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
