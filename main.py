"""
============================================================================
 自分だけが使える動画ダウンローダ  (Railway / FastAPI + yt-dlp + ffmpeg)
============================================================================

■ 構成
    main.py                … このファイル (FastAPI アプリ本体)
    templates/index.html   … フロントエンド UI (Jinja2)
    requirements.txt       … 依存パッケージ
    Dockerfile             … ffmpeg 込みの実行環境

■ 主な機能
    1. AUTH_TOKEN による API 認証 (X-API-Key ヘッダ / token・api_key クエリ)
    2. 非同期ダウンロード (スレッドプール) + progress_hooks による進捗管理
    3. ポーリング用 API と、おまけの SSE ストリーム
    4. エフェメラルストレージ対策の自動削除
       - ファイル送出直後の BackgroundTask 削除
       - TTL 超過ジョブ / 孤立ファイル / .part を掃除するリーパー
    5. SSRF・パストラバーサル対策の URL / ファイル名バリデーション
    6. Referer / User-Agent の透過 (ドメインロックされたプレイヤー対策)
       → リクエストごとに指定可能。m3u8 本体だけでなく全 .ts セグメントにも適用

■ 環境変数
  【必須】
    AUTH_TOKEN            API トークン。未設定なら起動を拒否します (事故防止)。
                          生成例: openssl rand -hex 32
  【任意 / 未設定ならデフォルト値】
    PORT                  Railway が自動注入 (Dockerfile の CMD で使用)
    DOWNLOAD_DIR          保存先ディレクトリ                 (default: /app/downloads)
    MAX_CONCURRENT        同時ダウンロード数                 (default: 2)
    MIN_FREE_DISK_MB      これ未満の空き容量なら受付停止(507)  (default: 200)
    MAX_FILESIZE_MB       0=無効。超過ファイルは破棄して失敗   (default: 0)
    FILE_TTL_SECONDS      未取得ファイルの保持秒数            (default: 600)
    JOB_TTL_SECONDS       完了ジョブをメモリから消すまでの秒    (default: 3600)
    REAPER_INTERVAL       掃除スレッドの実行間隔(秒)          (default: 20)
    TEMP_FILE_GRACE_SECONDS 孤立した .part の猶予秒数         (default: 1800)
    MAX_HISTORY           メモリ上に保持するジョブ数の上限     (default: 200)
    ALLOWED_SCHEMES       許可する URL スキーム              (default: http,https)
    BLOCK_PRIVATE_HOSTS   1 で localhost/内部IP を拒否(SSRF)   (default: 1)
    ALLOW_PLAYLIST        1 でプレイリスト一括取得を許可       (default: 0)
    ALLOWED_EXTRACTORS    カンマ区切りの抽出元ホワイトリスト    (default: 空=全て)
                          例) ALLOWED_EXTRACTORS=youtube,twitter  (glob可)
    COOKIES_FILE          cookies.txt の絶対パス (年齢制限/会員限定用)
    AUDIO_CODEC           「音声のみ」の変換形式              (default: mp3)
    AUDIO_QUALITY         「音声のみ」のビットレート(kbps)     (default: 192)
    LOG_LEVEL             ログレベル                          (default: INFO)
    ENABLE_SITE_RESOLVERS 1 でサイト固有リゾルバを有効化       (default: 1)
                          (StreamHG / iPlayerHLS 等を .m3u8 へ解決)
    RESOLVER_EXTRA_HOSTS  リゾルバ対象に追加するホスト名 (カンマ区切り)
                          例) RESOLVER_EXTRA_HOSTS=streamhg.net,foo.example

■ リクエスト単位のパラメータ (環境変数ではない)
    POST /api/download と POST /api/info は以下を受け付けます。
      url          必須。動画ページ URL / 直接の .m3u8 (HLS) URL
      quality      best / 1080 / 720 / 480 / audio
      playlist     true で再生リスト一括 (ALLOW_PLAYLIST=1 のときのみ有効)
      referer      任意。ドメインロックされたプレイヤーが 403 を返す場合に、
                   動画ページを表示していた URL を指定する。
                   CRLF・制御文字・http(s) 以外のスキームは 400 で拒否。
      user_agent   任意。User-Agent ヘッダの上書き (同上の検証あり)
============================================================================
"""

from __future__ import annotations

import asyncio
import fnmatch
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote, urlparse

import yt_dlp

import resolvers
from resolvers import ResolveError

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# ロガー
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("video-dl")
# yt-dlp 自身のログは WARNING 以上に抑える (INFO だと大量に出るため)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)


# ===========================================================================
# 1. 設定 (環境変数)
# ===========================================================================
def _env_str(name: str, default: str) -> str:
    """環境変数を文字列で取得。空文字はデフォルト扱い。"""
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    """環境変数を int で取得。壊れた値は黙ってデフォルトにフォールバック。"""
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        log.warning("環境変数 %s=%r を int 解釈できないため %d を使用", name, raw, default)
        return default


def _env_flag(name: str, default: bool) -> bool:
    """環境変数を bool で取得 (1/true/yes/on を真とみなす)。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


AUTH_TOKEN: str = os.getenv("AUTH_TOKEN", "").strip()
DOWNLOAD_DIR: Path = Path(_env_str("DOWNLOAD_DIR", "/app/downloads")).resolve()

MAX_CONCURRENT: int = max(1, _env_int("MAX_CONCURRENT", 2))
MIN_FREE_DISK_MB: int = max(0, _env_int("MIN_FREE_DISK_MB", 200))
MAX_FILESIZE_MB: int = max(0, _env_int("MAX_FILESIZE_MB", 0))          # 0 = 無効
JOB_TTL_SECONDS: int = max(60, _env_int("JOB_TTL_SECONDS", 3600))
FILE_TTL_SECONDS: int = max(60, _env_int("FILE_TTL_SECONDS", 600))
MAX_HISTORY: int = max(20, _env_int("MAX_HISTORY", 200))
REAPER_INTERVAL: int = max(5, _env_int("REAPER_INTERVAL", 20))
# 実行中ジョブに属さない一時ファイル (.part 等) の猶予時間
TEMP_FILE_GRACE_SECONDS: int = max(60, _env_int("TEMP_FILE_GRACE_SECONDS", 1800))

ALLOWED_SCHEMES: Set[str] = {
    s.strip().lower() for s in _env_str("ALLOWED_SCHEMES", "http,https").split(",") if s.strip()
}
ALLOWED_EXTRACTORS: List[str] = [
    e.strip().lower() for e in _env_str("ALLOWED_EXTRACTORS", "").split(",") if e.strip()
]
# yt-dlp の "allowed_extractors" は正規表現として解釈されるため、
# 指定しやすい glob 形式 (youtube*, twitter) を正規表現へ変換しておく。
ALLOWED_EXTRACTOR_REGEXES: List[str] = [
    fnmatch.translate(pat) for pat in ALLOWED_EXTRACTORS
]
ALLOW_PLAYLIST: bool = _env_flag("ALLOW_PLAYLIST", False)
BLOCK_PRIVATE_HOSTS: bool = _env_flag("BLOCK_PRIVATE_HOSTS", True)
COOKIES_FILE: str = _env_str("COOKIES_FILE", "").strip()

# サイト固有リゾルバ (StreamHG / iPlayerHLS など、yt-dlp に extractor が無い
# HLS ホストを .m3u8 へ解決する) を有効にするか。 1=有効(既定) / 0=無効
ENABLE_SITE_RESOLVERS: bool = _env_flag("ENABLE_SITE_RESOLVERS", True)

# リゾルバの対象に追加するホスト名 (カンマ区切り)。
# StreamHG 系はミラードメインが多いため、コード変更せずに追加できるようにする。
#   例) RESOLVER_EXTRA_HOSTS=streamhg.net,myhost.example
RESOLVER_EXTRA_HOSTS: List[str] = [
    h.strip().lower().removeprefix("www.")
    for h in _env_str("RESOLVER_EXTRA_HOSTS", "").split(",") if h.strip()
]
if RESOLVER_EXTRA_HOSTS:
    for _h in RESOLVER_EXTRA_HOSTS:
        resolvers.STREAMHG_HOSTS.add(_h)
        resolvers.STREAMHG_HOSTS.add(f"www.{_h}")

APP_START_TIME: float = time.time()
APP_VERSION: str = "1.2.0"


# ===========================================================================
# 2. 例外 / バリデーション (SSRF・パストラバーサル対策)
# ===========================================================================
class ValidationError(Exception):
    """400 相当。ユーザー起因の入力エラー。"""


class ExtractionError(Exception):
    """yt-dlp が失敗したケース (存在しないURL / 非公開 / 地域制限 など)。"""


# job_id は必ずこの形。これ以外は一切受け付けない = パストラバーサルを根本から遮断。
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def validate_job_id(job_id: str) -> str:
    """job_id が UUID 形式であることを確認する。"""
    job_id = (job_id or "").strip().lower()
    if not _JOB_ID_RE.match(job_id):
        # ※ ここにユーザー入力をそのまま含めない (ログ汚染 / XSS 対策)
        raise HTTPException(status_code=404, detail="ジョブが見つかりません (job_id が不正です)。")
    return job_id


def validate_url(raw_url: str) -> str:
    """
    ダウンロード対象 URL のバリデーション。

    対策している攻撃:
      * file:// / gopher:// / dict:// などの危険スキーム (ローカルファイル読み取り)
      * 認証情報 (user:pass@) を含む URL
      * localhost / 127.0.0.1 / 169.254.169.254 / 10.x / 192.168.x などの
        内部ネットワーク (メタデータサービス含む) への SSRF
    """
    url = (raw_url or "").strip()
    if not url:
        raise ValidationError("URL が入力されていません。")
    if len(url) > 2048:
        raise ValidationError("URL が長すぎます (2048 文字以内)。")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValidationError(f"URL を解釈できません: {exc}") from exc

    # --- スキーム ---
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValidationError(
            f"許可されていないスキーム '{scheme or '(なし)'}' です。"
            f"使用可能: {', '.join(sorted(ALLOWED_SCHEMES))}"
        )

    # --- ホスト ---
    host = parsed.hostname  # IDNA デコード + 小文字化 + ポート除去済み
    if not host:
        raise ValidationError("URL にホスト名が含まれていません。")

    if parsed.username or parsed.password:
        raise ValidationError("URL 内に認証情報 (user:pass@) を含めることはできません。")

    if parsed.port is not None and not (0 < parsed.port < 65536):
        raise ValidationError("ポート番号が不正です。")

    # --- 内部ネットワーク (SSRF) ブロック ---
    if BLOCK_PRIVATE_HOSTS:
        host_l = host.lower().rstrip(".")
        if host_l in {"localhost", "ip6-localhost", "ip6-loopback"}:
            raise ValidationError("ローカルホストへのアクセスは禁止されています。")

        # ホスト名を IP リテラルとして解釈してみる (例: 127.0.0.1 / ::1)
        candidate: Optional[str] = host_l
        if host_l.startswith("[") and host_l.endswith("]"):
            candidate = host_l[1:-1]

        try:
            ip_obj = ipaddress.ip_address(candidate)  # type: ignore[arg-type]
        except ValueError:
            ip_obj = None

        if ip_obj is not None:
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local       # 169.254.x.x (クラウドのメタデータ)
                or ip_obj.is_reserved
                or ip_obj.is_multicast
                or ip_obj.is_unspecified
            ):
                raise ValidationError("プライベート/内部 IP アドレスへのアクセスは禁止されています。")
        else:
            # ドメイン名の場合も、よくある内部ホスト名だけは明示的に弾く
            if host_l.endswith((".local", ".internal", ".localdomain")):
                raise ValidationError("内部ドメインへのアクセスは禁止されています。")
            # 10 進整数表記 (http://2130706433/) などの回避手法対策
            if host_l.isdigit():
                try:
                    ip_obj = ipaddress.ip_address(int(host_l))
                    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                        raise ValidationError("プライベート IP へのアクセスは禁止されています。")
                except ValueError:
                    pass

    return url


# HTTP ヘッダ値に含めてはいけない文字 (改行 = ヘッダインジェクション)
_HEADER_UNSAFE_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")


def validate_header_value(value: Optional[str], field_name: str,
                          max_len: int = 512) -> Optional[str]:
    """
    HTTP ヘッダ値として安全かどうかを検証する。

    改行 (\r\n) を混ぜると「ヘッダインジェクション」で任意ヘッダを
    追加できてしまうため、制御文字を一切許さない。
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if len(v) > max_len:
        raise ValidationError(f"{field_name} が長すぎます ({max_len} 文字以内)。")
    if _HEADER_UNSAFE_RE.search(v):
        raise ValidationError(f"{field_name} に改行や制御文字を含めることはできません。")
    return v


def validate_referer(value: Optional[str]) -> Optional[str]:
    """
    Referer ヘッダの検証。http(s) の URL 形式のみ許可する。

    ※ Referer は「送信先 URL に添えるヘッダ」であって新たなリクエスト先では
      ないため、validate_url のプライベートIPブロックは適用しない
      (適用すると legit なサイト内 Referer を弾いてしまう)。
    """
    v = validate_header_value(value, "referer", max_len=2048)
    if v is None:
        return None
    parsed = urlparse(v)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValidationError("referer は http:// または https:// で始まる URL を指定してください。")
    if not parsed.hostname:
        raise ValidationError("referer にホスト名が含まれていません。")
    return v


def safe_filename(name: Optional[str], fallback: str = "download") -> str:
    """
    レスポンス用のファイル名をサニタイズする。
    yt-dlp 由来のタイトルには '/' や '..' 、制御文字が含まれ得るため必ず通す。
    """
    name = unquote(name or "").strip()
    name = name.replace("\x00", "")
    # ディレクトリ区切り・親参照を除去
    name = re.sub(r"[\\/]+", "_", name)
    name = re.sub(r"\.\.+", ".", name)
    # 制御文字と、HTTP ヘッダを壊す文字を除去
    name = re.sub(r"[\x00-\x1f\x7f\"'<>|:;*?]", "", name)
    name = name.strip(" .")
    if not name:
        name = fallback
    return name[:180]  # Content-Disposition が長くなりすぎないように


def free_disk_mb() -> float:
    """DOWNLOAD_DIR の空き容量 (MB)。取得失敗時は inf を返して処理を止めない。"""
    try:
        usage = shutil.disk_usage(DOWNLOAD_DIR)
        return usage.free / (1024 * 1024)
    except OSError:
        return float("inf")


# ===========================================================================
# 3. ジョブモデル (進捗はメモリ上に保持)
# ===========================================================================
class JobState:
    QUEUED = "queued"            # スレッドプールの空き待ち
    DOWNLOADING = "downloading"  # 動画/音声を取得中
    PROCESSING = "processing"    # ffmpeg でマージ / 変換中
    FINISHED = "finished"        # 取得可能
    ERROR = "error"              # 失敗


@dataclass
class Job:
    """1 件のダウンロードジョブ。progress_hooks から別スレッドで更新される。"""

    id: str
    url: str
    quality: str
    audio_only: bool
    allow_playlist: bool
    # ドメインロックされたプレイヤー (StreamHG 等) 用のヘッダ透過
    referer: Optional[str] = None
    user_agent: Optional[str] = None
    resolver: Optional[str] = None        # どのリゾルバが URL を解決したか
    created_at: float = field(default_factory=time.time)

    state: str = JobState.QUEUED
    percent: float = 0.0
    stage: str = "準備中"
    speed_text: str = ""
    eta_text: str = ""
    error_message: Optional[str] = None

    title: Optional[str] = None
    uploader: Optional[str] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    extractor: Optional[str] = None
    webpage_url: Optional[str] = None
    is_playlist: bool = False
    entries_total: Optional[int] = None

    filename: Optional[str] = None      # クライアントに提示する表示名
    filepath: Optional[str] = None      # サーバ内の絶対パス (外部には返さない)
    filesize: Optional[int] = None
    finished_at: Optional[float] = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ---- 状態更新ヘルパ (全てロック内で行う) --------------------------------
    def set_progress(
        self,
        percent: Optional[float],
        stage: Optional[str] = None,
        speed_text: str = "",
        eta_text: str = "",
    ) -> None:
        with self._lock:
            if percent is not None:
                # 完了後のフックで 0% が飛んでくることがあるので後退させない
                self.percent = max(self.percent, min(100.0, max(0.0, float(percent))))
            if stage:
                self.stage = stage
            if speed_text:
                self.speed_text = speed_text
            if eta_text:
                self.eta_text = eta_text

    def set_meta(self, info: Dict[str, Any]) -> None:
        """yt-dlp の info_dict から表示用メタ情報を反映する。"""
        with self._lock:
            self.title = info.get("title") or info.get("webpage_url_basename") or self.url
            self.uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id")
            self.duration = info.get("duration")
            self.thumbnail = info.get("thumbnail")
            self.extractor = info.get("extractor_key") or info.get("extractor")
            self.webpage_url = info.get("webpage_url") or self.url
            entries = info.get("entries")
            if entries is not None:
                self.is_playlist = True
                try:
                    self.entries_total = len([e for e in entries if e])
                except TypeError:
                    self.entries_total = None

    def set_stage(self, stage: str, processing: bool = False) -> None:
        """
        後処理 (ffmpeg マージ / 音声変換) フェーズに入ったことを記録する。
        processing=True のときは state も PROCESSING へ遷移させ、
        UI 側が「ダウンロードは終わったが変換中」を区別できるようにする。
        """
        with self._lock:
            self.stage = stage
            if processing:
                self.state = JobState.PROCESSING
                self.percent = 100.0     # 取得自体は完了しているので 100% 表示
                self.speed_text = ""
                self.eta_text = ""

    def finish(self, filepath: Path, display_name: str) -> None:
        with self._lock:
            self.state = JobState.FINISHED
            self.percent = 100.0
            self.stage = "完了"
            self.speed_text = ""
            self.eta_text = ""
            self.filepath = str(filepath)
            self.filename = safe_filename(display_name, fallback=f"{self.id}.bin")
            try:
                self.filesize = filepath.stat().st_size
            except OSError:
                self.filesize = None
            self.finished_at = time.time()

    def fail(self, message: str) -> None:
        with self._lock:
            self.state = JobState.ERROR
            self.stage = "失敗"
            self.error_message = message[:2000]
            self.speed_text = ""
            self.eta_text = ""
            self.finished_at = time.time()

    # ---- 外部公開用の辞書 (filepath など内部情報は絶対に含めない) --------------
    def public(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "state": self.state,
                "percent": round(self.percent, 2),
                "stage": self.stage,
                "speed": self.speed_text,
                "eta": self.eta_text,
                "error": self.error_message,
                "title": self.title,
                "uploader": self.uploader,
                "duration": self.duration,
                "thumbnail": self.thumbnail,
                "extractor": self.extractor,
                "webpage_url": self.webpage_url,
                "is_playlist": self.is_playlist,
                "entries_total": self.entries_total,
                "quality": self.quality,
                "audio_only": self.audio_only,
                "referer": self.referer,
                "resolver": self.resolver,
                "filename": self.filename,
                "filesize": self.filesize,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                # 生成済みなら取得用 URL (token はフロント側で付与する)
                "download_path": f"/api/download/{self.id}" if self.filepath else None,
                "expires_in": (
                    max(0, int(self.finished_at + FILE_TTL_SECONDS - time.time()))
                    if self.finished_at
                    else None
                ),
            }


# ---- ジョブストア (メモリのみ。 Railway は再起動で消える前提) ---------------
JOBS: "OrderedDict[str, Job]" = OrderedDict()
JOBS_LOCK = threading.Lock()
_download_semaphore = threading.Semaphore(MAX_CONCURRENT)
_executor: Optional[ThreadPoolExecutor] = None


def _register(job: Job) -> None:
    """ジョブを登録し、履歴上限を超えたら古い完了済みジョブから破棄する。"""
    with JOBS_LOCK:
        JOBS[job.id] = job
        JOBS.move_to_end(job.id)
        while len(JOBS) > MAX_HISTORY:
            for old_id, old_job in list(JOBS.items()):
                if old_job.state in (JobState.FINISHED, JobState.ERROR):
                    JOBS.pop(old_id, None)
                    break
            else:
                break  # すべて実行中なら無理に消さない


def _prune_jobs() -> None:
    """TTL を超えた完了/失敗ジョブをメモリから削除する (リーパーが呼ぶ)。"""
    now = time.time()
    with JOBS_LOCK:
        for job_id in list(JOBS.keys()):
            job = JOBS[job_id]
            if job.state not in (JobState.FINISHED, JobState.ERROR):
                continue
            if job.finished_at is not None and now - job.finished_at > JOB_TTL_SECONDS:
                JOBS.pop(job_id, None)


def _delete_file(path: Optional[str], reason: str = "") -> bool:
    """ファイルを安全に削除する (存在しなくてもエラーにしない)。"""
    if not path:
        return False
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
            log.info("削除しました (%s): %s", reason or "cleanup", p.name)
            return True
    except OSError as exc:
        log.warning("ファイル削除に失敗 %s: %s", path, exc)
    return False


# ===========================================================================
# 4. yt-dlp の実行 (ワーカースレッド)
# ===========================================================================
# UI から選べる品質プリセット。key = フロントから送られる値。
QUALITY_PRESETS: Dict[str, Dict[str, Any]] = {
    "best": {
        "label": "最高画質 (動画+音声をマージ)",
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
    },
    "1080": {
        "label": "1080p 以下",
        "format": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
    },
    "720": {
        "label": "720p 以下",
        "format": "bestvideo*[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
    },
    "480": {
        "label": "480p 以下 (低速回線向け)",
        "format": "bestvideo*[height<=480]+bestaudio/best[height<=480]/best",
        "merge_output_format": "mp4",
    },
    "audio": {
        "label": "音声のみ (MP3)",
        "format": "bestaudio/best",
        "merge_output_format": "m4a",
        "audio_only": True,
    },
}


def _probe_opts(allow_playlist: bool = False, referer: Optional[str] = None,
                user_agent: Optional[str] = None) -> Dict[str, Any]:
    """メタ情報だけを取得するための軽量オプション。"""
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": not allow_playlist,
        "socket_timeout": 20,
        "retries": 2,
        "cachedir": os.path.join(os.getenv("XDG_CACHE_HOME", "/tmp/.cache"), "yt-dlp"),
    }
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    if ALLOWED_EXTRACTOR_REGEXES:
        opts["allowed_extractors"] = list(ALLOWED_EXTRACTOR_REGEXES)
    headers: Dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if user_agent:
        headers["User-Agent"] = user_agent
    if headers:
        opts["http_headers"] = headers
    return opts


def _extractor_keys(info: Dict[str, Any]) -> List[str]:
    """info_dict から抽出元キーを (プレイリストの中身含め) 集める。"""
    keys: List[str] = []
    top = info.get("extractor_key") or info.get("extractor") or info.get("ie_key")
    if top:
        keys.append(str(top).lower())
    for entry in info.get("entries") or []:
        if not entry:
            continue
        ek = entry.get("ie_key") or entry.get("extractor_key") or entry.get("extractor")
        if ek:
            keys.append(str(ek).lower())
    return keys


def _assert_extractor_allowed(info: Dict[str, Any]) -> None:
    """
    ALLOWED_EXTRACTORS が設定されている場合、その抽出元だけが許可される。
    ワイルドカード (youtube*, generic など) 対応。
    """
    if not ALLOWED_EXTRACTORS:
        return
    keys = _extractor_keys(info) or ["unknown"]
    for key in keys:
        if any(fnmatch.fnmatch(key, pattern) for pattern in ALLOWED_EXTRACTORS):
            continue
        raise ExtractionError(
            f"抽出元 '{key}' はこのサーバでは許可されていません。"
            f" (許可リスト: {', '.join(ALLOWED_EXTRACTORS)})"
        )


def _hook_size(d: Dict[str, Any]) -> Optional[int]:
    """progress_hooks の dict から総サイズを安全に取り出す。"""
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    return int(total) if isinstance(total, (int, float)) and total > 0 else None


def _fmt_eta(seconds: Optional[float]) -> str:
    if not seconds or seconds < 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _make_progress_hook(job: Job):
    """
    yt-dlp に渡す progress_hooks を生成する。
    これは yt-dlp のワーカースレッドから同期呼び出しされるため、
    重い処理 (ログ出力・IO) を避けて状態更新だけに留める。
    """

    def hook(d: Dict[str, Any]) -> None:
        try:
            info = d.get("info_dict") or {}
            if not job.title and info.get("title"):
                job.set_meta(info)

            st = d.get("status")
            if st == "downloading":
                total = _hook_size(d)
                downloaded = d.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100.0) if total else None
                speed = d.get("speed")
                job.set_progress(
                    percent=percent,
                    stage="ダウンロード中",
                    speed_text=f"{speed / 1024 / 1024:.2f} MB/s" if speed else "",
                    eta_text=_fmt_eta(d.get("eta")),
                )
            elif st == "finished":
                job.set_progress(100.0, stage="ダウンロード完了")
        except Exception:  # フック内部の例外でダウンロードを殺さない
            log.exception("progress_hook 内でエラー (無視します)")

    return hook


def _make_postprocessor_hook(job: Job):
    """ffmpeg によるマージ/変換フェーズの進捗表示用。"""

    def hook(d: Dict[str, Any]) -> None:
        try:
            name = (d.get("postprocessor") or "").lower()
            st = d.get("status")
            if st == "started":
                if "merger" in name:
                    job.set_stage("動画と音声をマージ中 (ffmpeg)", processing=True)
                elif "extractaudio" in name or "mp3" in name:
                    job.set_stage("音声を抽出/変換中 (ffmpeg)", processing=True)
                elif "ffmpeg" in name:
                    job.set_stage("ffmpeg で後処理中", processing=True)
                else:
                    job.set_progress(None, stage=f"後処理中: {d.get('postprocessor') or ''}")
            elif st == "finished":
                job.set_progress(None, stage="後処理完了")
        except Exception:
            log.exception("postprocessor_hook 内でエラー (無視します)")

    return hook


# yt-dlp がマージ前に作る中間ファイル名のパターン
#   例) "JOBID.Title.f137.mp4" / "JOBID.Title.fa1.m4a"
# 注意: 単純に ".f<英数>." を弾くと "JOBID.full.mp4" (タイトルが f 始まり) を
#       誤判定するため、"`.f<id>` を除いた名前が実在するときだけ" 中間ファイルと
#       みなす (=_filter_intermediates)。
_INTERMEDIATE_RE = re.compile(r"^(?P<stem>.+)\.f[A-Za-z0-9_-]+\.(?P<ext>[A-Za-z0-9]+)$")


def _paths_from_info(info: Any) -> List[Path]:
    """
    yt-dlp が返す info_dict から「最終的な成果物のパス」を正確に取得する。

    `requested_downloads[*].filepath` は ffmpeg によるマージ/変換・MoveFiles まで
    完了した後の実パスなので、ファイル名から推測するより確実に正しい。
    プレイリストの場合は entries を再帰的に辿る。
    """
    paths: List[Path] = []
    if not isinstance(info, dict):
        return paths

    for rd in info.get("requested_downloads") or []:
        fp = (rd or {}).get("filepath")
        if fp:
            paths.append(Path(fp))

    for entry in info.get("entries") or []:
        if entry:
            paths.extend(_paths_from_info(entry))

    # 重複排除 (順序は維持)
    seen: Set[str] = set()
    unique: List[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _filter_intermediates(candidates: List[Path]) -> List[Path]:
    """glob で集めた候補から、マージ前の中間ファイルを取り除く。"""
    names = {p.name for p in candidates}
    kept: List[Path] = []
    for path in candidates:
        m = _INTERMEDIATE_RE.match(path.name)
        if m and f"{m.group('stem')}.{m.group('ext')}" in names:
            # 例: "JOB.Title.f137.mp4" に対して "JOB.Title.mp4" が存在する
            log.debug("中間ファイルを除外: %s", path.name)
            continue
        kept.append(path)
    return kept


def _collect_outputs(job_id: str, allow_playlist: bool) -> List[Path]:
    """
    ダウンロード生成物を job_id プレフィックスで収集する。

    ファイル名は必ず "{job_id}." / "{job_id}-" で始まるため、
    ユーザー入力がパスに混入する余地がなく (パストラバーサル不可)、
    他ジョブのファイルと混ざることもない。

    allow_playlist=False なのに複数ファイルが生成された場合
    (例: generic extractor が HTML 内の複数メディアを playlist 扱いした、
     サムネイル/字幕などの副産物が出た) は、
    最大のものを採用し **残りは即座に削除** する。
    放置するとエフェメラルストレージを無駄に圧迫するため。
    """
    files: List[Path] = []
    if not DOWNLOAD_DIR.exists():
        return files
    for path in DOWNLOAD_DIR.glob(f"{job_id}*"):
        # 一時ファイル (.part / .ytdl / .temp / .frag) は完成品ではない
        if path.suffix.lower() in {".part", ".ytdl", ".temp", ".frag"}:
            continue
        if path.is_file():
            files.append(path)

    # ffmpeg マージ前の中間ファイル (<name>.f<format_id>.<ext>) が残っていた場合、
    # それを成果物として拾うと「映像だけ/音声だけ」を渡してしまうため除外する。
    return _filter_intermediates(files)


def _select_outputs(files: List[Path], allow_playlist: bool, job_id: str) -> List[Path]:
    """
    成果物を「実際にクライアントへ渡す形」に整える。

    * allow_playlist=True  → 全件をそのまま返す (呼び出し側で ZIP 化)
    * allow_playlist=False → 1 件だけを残し、**残りは即座に削除**する。
      単体のつもりで複数生成される典型例:
        - generic extractor が HTML 内の複数メディアを playlist 扱いした
        - サムネイルやカバー画像などの副産物が出た
      放置するとエフェメラルストレージを圧迫するため、ここで確実に消す。
      残す 1 件は「最も大きいファイル」= 実体のある動画/音声とみなす。
    """
    if allow_playlist or len(files) <= 1:
        return files

    files = sorted(files, key=lambda p: p.stat().st_size if p.exists() else -1, reverse=True)
    keep, extras = files[:1], files[1:]
    log.info("[job=%s] 単体指定なのに %d 件生成されたため %s を採用、他 %d 件を削除",
             job_id[:8], len(files), keep[0].name, len(extras))
    for extra in extras:
        _delete_file(str(extra), reason="単体ジョブの余剰生成物")
    return keep


def _build_ydl_opts(job: Job, preset: Dict[str, Any]) -> Dict[str, Any]:
    """yt-dlp に渡すオプションを組み立てる。"""
    if job.allow_playlist:
        # "%(playlist_index|0)s" のように既定値を書くことで、
        # playlist_index を持たない抽出元でも outtmpl 展開エラーにならない。
        outtmpl = str(DOWNLOAD_DIR / f"{job.id}-%(playlist_index|0)s.%(title).120B.%(ext)s")
    else:
        outtmpl = str(DOWNLOAD_DIR / f"{job.id}.%(title).120B.%(ext)s")

    opts: Dict[str, Any] = {
        # --- 基本 ---
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": False,   # タイトルはそのまま (表示名は safe_filename で処理)
        "windowsfilenames": False,
        "overwrites": True,
        "continuedl": True,
        "noplaylist": not job.allow_playlist,  # 単体URLにプレイリストが付いていても1件だけ
        # --- ffmpeg マージ / 後処理 ---
        "format": preset["format"],
        "merge_output_format": preset.get("merge_output_format"),
        # --- 安定性 ---
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10485760,        # 10MB 分割で大きいファイルの失敗を減らす
        "ignoreerrors": False,              # 例外を投げてもらう (ここで握りつぶさない)
        # --- 進捗 ---
        "progress_hooks": [_make_progress_hook(job)],
        "postprocessor_hooks": [_make_postprocessor_hook(job)],
        # --- SSRF 追加防御: yt-dlp 側でも http/https のみ許可 ---
        "allowed_extensions": ["ALL"],
        # --- 保存場所 ---
        "cachedir": os.path.join(os.getenv("XDG_CACHE_HOME", "/tmp/.cache"), "yt-dlp"),
    }

    # 音声のみ → MP3 に変換
    if preset.get("audio_only") or job.audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": os.getenv("AUDIO_CODEC", "mp3"),
                "preferredquality": os.getenv("AUDIO_QUALITY", "192"),
            }
        ]
        opts["merge_output_format"] = None

    # --- Referer / User-Agent の透過 -------------------------------------
    # StreamHG などの「ドメインロックされたプレイヤー」は、正しい Referer が
    # 無いと m3u8 やセグメント (.ts) へのアクセスを 403 で拒否します。
    # yt-dlp の http_headers は 本体・マニフェスト・全セグメントのリクエストに
    # 適用されるため、ここで一度設定すれば HLS 全体に効きます。
    custom_headers: Dict[str, str] = {}
    if job.referer:
        custom_headers["Referer"] = job.referer
    if job.user_agent:
        custom_headers["User-Agent"] = job.user_agent
    if custom_headers:
        opts["http_headers"] = custom_headers

    # 抽出元ホワイトリスト (yt-dlp 純正オプション / 正規表現リスト)
    # 許可されていないサイトの URL は "Unsupported URL" として即座に弾かれ、
    # ダウンロード自体が始まらない = 帯域・ストレージの無駄が発生しない。
    if ALLOWED_EXTRACTOR_REGEXES:
        opts["allowed_extractors"] = list(ALLOWED_EXTRACTOR_REGEXES)

    # クッキーファイル (年齢制限・メンバー限定動画など)
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE

    # 上限サイズ指定があれば yt-dlp 側でも早期に弾く
    if MAX_FILESIZE_MB > 0:
        opts["max_filesize"] = MAX_FILESIZE_MB * 1024 * 1024

    return opts


def _zip_playlist(job: Job, files: List[Path]) -> Path:
    """プレイリスト(複数ファイル)を 1 つの zip にまとめて返す。"""
    zip_path = DOWNLOAD_DIR / f"{job.id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for f in files:
            # arcname にはサーバ内のパスを含めず、ファイル名だけを入れる
            zf.write(f, arcname=safe_filename(f.name, fallback=f.stem))
    for f in files:
        _delete_file(str(f), reason="zip 化後の元ファイル")
    return zip_path


def _run_download(job: Job) -> None:
    """
    ワーカースレッドで実行される本体。
    例外はすべて捕捉して job.fail() に落とし、プロセスを殺さない。
    """
    # 同時実行数を絞る (Railway の RAM/CPU・帯域を守る)
    with _download_semaphore:
        with JOBS_LOCK:
            job.state = JobState.DOWNLOADING
        log.info("[job=%s] 開始 url=%s quality=%s playlist=%s referer=%s",
                 job.id, job.url, job.quality, job.allow_playlist,
                 "set" if job.referer else "-")
        preset = QUALITY_PRESETS.get(job.quality, QUALITY_PRESETS["best"])

        try:
            # --- 開始前のディスク容量チェック --------------------------------
            free_mb = free_disk_mb()
            if free_mb < MIN_FREE_DISK_MB:
                raise ExtractionError(
                    f"サーバの空き容量が不足しています (残り {free_mb:.0f}MB / 必要 {MIN_FREE_DISK_MB}MB)。"
                    "少し待ってから再試行してください。"
                )

            # --- サイト固有リゾルバ -----------------------------------------
            # yt-dlp に専用 extractor が無いサイト (StreamHG / iPlayerHLS など) は、
            # ここで「動画ページ URL → 実際の .m3u8 URL」へ解決しておく。
            # 解決後は yt-dlp の generic extractor が HLS として確実に扱える。
            target_url = job.url
            resolver_title: Optional[str] = None
            if ENABLE_SITE_RESOLVERS:
                try:
                    resolved = resolvers.resolve_if_supported(
                        job.url,
                        user_agent=job.user_agent,
                        validate_url=validate_url,
                    )
                except (ResolveError, ValidationError) as exc:
                    # ページ取得失敗 / ファイル失効 / m3u8 が見つからない など
                    raise ExtractionError(str(exc)) from exc

                if resolved is not None:
                    target_url = resolved.media_url
                    # ユーザーが Referer を明示していなければ、解決の過程で判明した
                    # プレイヤーページ URL を Referer にする。
                    # これが無いと .ts セグメントの取得が 403 になる。
                    if not job.referer and resolved.referer:
                        job.referer = validate_header_value(resolved.referer, "referer", 2048)
                    with job._lock:
                        job.resolver = resolved.resolver
                    if resolved.title:
                        resolver_title = resolved.title
                        job.set_meta({"title": resolved.title})
                    log.info("[job=%s] リゾルバ解決 %s -> %s (referer=%s)",
                             job.id, resolved.resolver, target_url[:110],
                             "set" if job.referer else "-")

            # リゾルバによって Referer が決まる場合があるため、
            # オプションの組み立てはこの後に行う。
            opts = _build_ydl_opts(job, preset)

            # --- ダウンロード実行 -------------------------------------------
            # 抽出元ホワイトリストは yt-dlp 純正の "allowed_extractors" で
            # 制限済み (=_build_ydl_opts 参照)。余計な事前リクエストは発生しない。
            #
            # download() ではなく extract_info(download=True) を使う理由:
            #   返り値の info_dict に requested_downloads[*].filepath が入っており、
            #   ffmpeg マージ後の「最終的な出力パス」を推測なしで正確に知れるため。
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target_url, download=True) or {}
                job.set_meta(info)          # タイトル等の確定値を反映
                # リゾルバがページから取ったタイトルの方が有益な場合が多い。
                # m3u8 を generic extractor で読むと "index" 等の味気ない名前になるため、
                # 抽出元が generic (またはタイトル未取得) のときはリゾルバの値を優先する。
                if resolver_title and (
                    not info.get("title") or info.get("extractor_key") == "Generic"
                ):
                    job.set_meta({"title": resolver_title})
                paths = _paths_from_info(info)
                if not paths:
                    # requested_downloads が無い古い挙動への保険
                    try:
                        candidate = Path(ydl.prepare_filename(info))
                        if candidate.is_file():
                            paths = [candidate]
                    except Exception:
                        log.debug("prepare_filename フォールバックに失敗", exc_info=True)

            if not job.title:
                job.set_meta({"title": job.url})

            # --- 生成物の収集 ------------------------------------------------
            # 第一候補: yt-dlp が報告した実パス / 第二候補: job_id での glob
            files = [path for path in paths if path.is_file()]
            if not files:
                files = _collect_outputs(job.id, job.allow_playlist)
            files = _select_outputs(files, job.allow_playlist, job.id)
            if not job.allow_playlist:
                # 単体モードで配信するなら、UI 上もプレイリスト扱いにしない
                with job._lock:
                    job.is_playlist = False
            if not files:
                raise ExtractionError(
                    "ダウンロードは完了しましたが、ファイルが見つかりませんでした。"
                    "(既に存在する動画でスキップされた可能性があります)"
                )

            if len(files) > 1:
                final_path = _zip_playlist(job, files)
                display_name = f"{job.title or 'playlist'} ({len(files)} files).zip"
            else:
                final_path = files[0]
                display_name = final_path.name
                # 表示名から job_id プレフィックスを外して綺麗にする
                display_name = re.sub(rf"^{re.escape(job.id)}[-.]?", "", display_name)

            # --- サイズ上限チェック ------------------------------------------
            size_bytes = final_path.stat().st_size
            if MAX_FILESIZE_MB > 0 and size_bytes > MAX_FILESIZE_MB * 1024 * 1024:
                _delete_file(str(final_path), reason="サイズ上限超過")
                raise ExtractionError(
                    f"ファイルサイズ {size_bytes / 1024 / 1024:.1f}MB が上限 "
                    f"{MAX_FILESIZE_MB}MB を超えたため破棄しました。"
                )

            job.finish(final_path, display_name)
            log.info("[job=%s] 完了 file=%s size=%.1fMB",
                     job.id, final_path.name, size_bytes / 1024 / 1024)

        except yt_dlp.utils.DownloadError as exc:
            # 存在しないURL / 非公開 / 年齢制限 / 地域制限 / ネットワークエラー など
            job.fail(_friendly_error(str(exc)))
            log.warning("[job=%s] DownloadError: %s", job.id, exc)
        except yt_dlp.utils.ExtractorError as exc:
            job.fail(_friendly_error(str(exc)))
            log.warning("[job=%s] ExtractorError: %s", job.id, exc)
        except ValidationError as exc:
            job.fail(str(exc))
        except ExtractionError as exc:
            job.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 想定外も必ずユーザーへ返す
            log.exception("[job=%s] 想定外のエラー", job.id)
            job.fail(f"内部エラーが発生しました: {exc.__class__.__name__}: {exc}")
        finally:
            # 失敗時は残骸 (.part など) を掃除する
            if job.state == JobState.ERROR:
                _cleanup_partial(job.id)


def _cleanup_partial(job_id: str) -> None:
    """ジョブの一時ファイル・生成物をまとめて削除する。"""
    if not DOWNLOAD_DIR.exists():
        return
    for path in DOWNLOAD_DIR.glob(f"{job_id}*"):
        _delete_file(str(path), reason="失敗ジョブの残骸")


def _friendly_error(message: str) -> str:
    """yt-dlp の長い英語エラーを、UI に出しやすい日本語に寄せる。"""
    m = message.strip()
    lowered = m.lower()

    mapping = [
        ("video unavailable", "この動画は視聴できません (非公開・削除済み・メンバー限定の可能性)。"),
        ("private video", "この動画は非公開 (プライベート) です。"),
        ("sign in to confirm your age", "年齢制限のある動画です。COOKIES_FILE の設定が必要です。"),
        ("sign in to confirm you're not a bot", "YouTube 側でボット判定されました。COOKIES_FILE の設定を推奨します。"),
        ("members-only", "メンバー限定の動画です。COOKIES_FILE の設定が必要です。"),
        ("login", "この動画の視聴にはログイン (クッキー) が必要です。"),
        ("the uploader has not made this video available in your country", "この動画は配信地域外のため取得できません。"),
        ("copyright", "著作権保護により取得できません。"),
        # allowed_extractors で制限した場合の yt-dlp メッセージ
        (
            "no suitable extractor found",
            (
                f"この URL の抽出元は許可されていません (許可リスト: {', '.join(ALLOWED_EXTRACTORS)})。"
                if ALLOWED_EXTRACTORS
                else "この URL に対応する抽出元が見つかりませんでした。"
            ),
        ),
        (
            "unsupported url",
            (
                f"この URL の抽出元は許可されていません (許可リスト: {', '.join(ALLOWED_EXTRACTORS)})。"
                if ALLOWED_EXTRACTORS
                else "この URL には対応していません (yt-dlp 非対応サイト、または URL の形式が不正)。"
            ),
        ),
        ("no video formats", "利用可能な動画フォーマットが見つかりませんでした。"),
        ("requested format is not available", "指定した画質のフォーマットが存在しません。別の画質でお試しください。"),
        ("http error 404", "URL が見つかりません (404)。アドレスを確認してください。"),
        ("http error 403", "アクセスが拒否されました (403)。"),
        ("http error 429", "リクエストが多すぎます (429)。少し待ってから再試行してください。"),
        ("unable to download webpage", "ページを取得できませんでした。URL を確認するか、少し待ってから再試行してください。"),
        ("name or service not known", "ホスト名を解決できません (DNS エラー)。URL を確認してください。"),
        ("timed out", "接続がタイムアウトしました。再試行してください。"),
        ("the file is too large", "ファイルが大きすぎるため取得できません。"),
        ("ffmpeg", "ffmpeg の処理に失敗しました。別の画質や「音声のみ」でお試しください。"),
    ]
    for needle, ja in mapping:
        if needle in lowered:
            # 元のメッセージも併記しておくと調査しやすい
            return f"{ja}\n\n[詳細] {m[:600]}"

    return f"ダウンロードに失敗しました。\n\n[詳細] {m[:800]}"


# ===========================================================================
# 5. バックグラウンド: リーパー (ストレージ自動最適化)
# ===========================================================================
_reaper_stop = threading.Event()


def _reaper_loop() -> None:
    """
    一定間隔で走り、以下を自動削除する:
      1) FILE_TTL_SECONDS を過ぎた完了ジョブのファイル
         → 「ダウンロードし終わった直後に消す」の保険。
           実際の即時削除は /api/download の BackgroundTask が担当。
      2) どのジョブにも紐づかない孤立ファイル (再デプロイ・クラッシュ残骸)
      3) 古い .part / .ytdl / .frag 一時ファイル
      4) TTL を過ぎたジョブのメモリ上の記録
    """
    log.info("リーパースレッド起動 (interval=%ds, file_ttl=%ds)", REAPER_INTERVAL, FILE_TTL_SECONDS)
    while not _reaper_stop.is_set():
        _reaper_stop.wait(REAPER_INTERVAL)
        if _reaper_stop.is_set():
            break
        try:
            now = time.time()

            # --- 1) TTL 超過ファイル ---
            with JOBS_LOCK:
                jobs_snapshot = list(JOBS.values())
            known_paths: Set[str] = set()      # 触ってはいけない完成品
            active_paths: Set[str] = set()      # 実行中ジョブの一時ファイル
            for job in jobs_snapshot:
                if job.filepath:
                    known_paths.add(job.filepath)
                # 実行中ジョブの生成物 (.part 含む) は絶対に消さない。
                # 低速回線で長時間かかる DL の .part を誤削除すると失敗するため。
                if job.state in (JobState.QUEUED, JobState.DOWNLOADING, JobState.PROCESSING):
                    for p in DOWNLOAD_DIR.glob(f"{job.id}*"):
                        active_paths.add(str(p))

                # 完了から FILE_TTL_SECONDS を過ぎていたら実ファイルを破棄する
                if (
                    job.state == JobState.FINISHED
                    and job.finished_at
                    and now - job.finished_at > FILE_TTL_SECONDS
                ):
                    if _delete_file(job.filepath, reason=f"TTL超過 job={job.id[:8]}"):
                        with job._lock:
                            job.filepath = None
                            job.stage = "ファイルは自動削除されました (TTL 超過)"
                    elif job.filepath and not Path(job.filepath).exists():
                        # 実体が既に無い (例: 別経路で消えた) なら状態だけ整合させる
                        with job._lock:
                            job.filepath = None
                            job.stage = "ファイルは既に削除されています"

            # --- 2) & 3) ディレクトリ走査 ---
            if DOWNLOAD_DIR.exists():
                for path in DOWNLOAD_DIR.iterdir():
                    if not path.is_file():
                        continue
                    p_str = str(path)
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue

                    is_temp = path.suffix.lower() in {".part", ".ytdl", ".temp", ".frag"}

                    # 完成品 / 実行中ジョブのファイルはスキップ
                    if p_str in known_paths or p_str in active_paths:
                        continue

                    if is_temp:
                        # どのジョブにも属さない一時ファイルで、
                        # 30 分以上更新がないもの = クラッシュ残骸とみなす
                        if now - mtime > TEMP_FILE_GRACE_SECONDS:
                            _delete_file(p_str, reason="古い一時ファイル")
                    elif now - mtime > FILE_TTL_SECONDS:
                        _delete_file(p_str, reason="孤立ファイル")

            # --- 4) メモリ上のジョブ記録 ---
            _prune_jobs()

        except Exception:
            log.exception("リーパー実行中にエラー (継続します)")
    log.info("リーパースレッド終了")


# ===========================================================================
# 6. 認証 (AUTH_TOKEN)
# ===========================================================================
def require_auth(
    request: Request,
    # FastAPI の Header は自動で "x_api_key" -> "x-api-key" に変換されるが、
    # 明示的に alias を指定して /docs 上でも分かりやすくしておく。
    x_api_key: Optional[str] = Header(None, alias="X-API-Key", description="APIトークン"),
    token: Optional[str] = Query(None, description="APIトークン (クエリパラメータ)"),
    api_key: Optional[str] = Query(None, description="APIトークン (クエリパラメータ別名)"),
) -> str:
    """
    AUTH_TOKEN を検証する依存関数。受け付ける場所は以下の 4 通り。

      1. ヘッダ  X-API-Key: <token>          … フロントエンドの fetch が使用
      2. ヘッダ  Authorization: Bearer <token>… curl 等での手動確認用
      3. クエリ  ?token=<token>              … <a href> / location.href 直リンク用
      4. クエリ  ?api_key=<token>            … 同上 (別名)

    * クエリを許可しているのは、ブラウザが直接開くダウンロードリンクに
      カスタムヘッダを付けられないためです (トレードオフは README 参照)。
    * 比較は hmac.compare_digest を使い、タイミング攻撃を防ぎます。
    """
    if not AUTH_TOKEN:
        # 起動時に強制終了させているので通常ここには来ない (二重の保険)
        raise HTTPException(status_code=503, detail="サーバの AUTH_TOKEN が未設定です。")

    candidate: Optional[str] = x_api_key or request.headers.get("x-api-key")

    if not candidate:
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            candidate = auth_header[7:].strip()

    if not candidate:
        candidate = token or api_key

    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API トークンが必要です。ヘッダ 'X-API-Key' またはクエリ 'token' で指定してください。",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not hmac.compare_digest(candidate.strip(), AUTH_TOKEN):
        log.warning("認証失敗 (client=%s)", request.client.host if request.client else "unknown")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API トークンが正しくありません。",
        )
    return candidate.strip()


# ===========================================================================
# 7. リクエスト/レスポンス モデル
# ===========================================================================
class DownloadRequest(BaseModel):
    """POST /api/download のボディ。"""

    url: str = Field(..., description="ダウンロード対象の URL", min_length=1, max_length=2048)
    quality: str = Field("best", description="best / 1080 / 720 / 480 / audio")
    playlist: bool = Field(False, description="再生リスト全体を取得するか")
    referer: Optional[str] = Field(
        None, max_length=2048,
        description="Referer ヘッダ。ドメインロックされたプレイヤー (StreamHG 等) の "
                    "m3u8 が 403 になる場合に、元ページの URL を指定する")
    user_agent: Optional[str] = Field(
        None, max_length=512, description="User-Agent ヘッダの上書き (任意)")

    def normalized_quality(self) -> str:
        q = (self.quality or "best").strip().lower()
        return q if q in QUALITY_PRESETS else "best"


class InfoRequest(BaseModel):
    """POST /api/info のボディ (URL のメタ情報だけ確認する)。"""

    url: str = Field(..., min_length=1, max_length=2048)
    referer: Optional[str] = Field(None, max_length=2048)
    user_agent: Optional[str] = Field(None, max_length=512)


# ===========================================================================
# 8. FastAPI アプリ
# ===========================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時 / 終了時のリソース管理。"""
    global _executor

    # --- AUTH_TOKEN 未設定なら起動させない (認証なし公開事故の防止) -----------
    if not AUTH_TOKEN:
        log.error("=" * 70)
        log.error("環境変数 AUTH_TOKEN が設定されていないため起動を中止します。")
        log.error("例: AUTH_TOKEN=$(openssl rand -hex 32)")
        log.error("=" * 70)
        # Railway のログに分かりやすく出すために exit する
        sys.exit(1)

    # --- 保存先の作成と「実際に書き込めるか」の検証 -------------------------
    # Railway で Volume を /app/downloads にマウントした場合、Volume は root 所有で
    # マウントされるため、非 root ユーザ (appuser) だと書き込めません。
    # その場合は各ジョブが分かりにくいエラーで失敗するため、起動時に検知して
    # 対処方法ごとログに出し、即座に起動を止めます (fail-fast)。
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        probe = DOWNLOAD_DIR / ".write-probe"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        log.error("=" * 70)
        log.error("保存先ディレクトリに書き込めません: %s", DOWNLOAD_DIR)
        log.error("原因: %s", exc)
        log.error("-" * 70)
        log.error("対処法 (いずれか):")
        log.error("  1) Railway の Volume をマウントしている場合 →")
        log.error("     サービス変数に RAILWAY_RUN_UID=0 を追加して再デプロイ")
        log.error("     (Volume は root 所有でマウントされるため)")
        log.error("  2) 永続化が不要なら DOWNLOAD_DIR=/tmp/downloads を指定")
        log.error("     (コンテナ内で常に書き込み可能 / 再起動で消える)")
        log.error("=" * 70)
        sys.exit(1)

    log.info("保存先ディレクトリ: %s (空き %.0fMB, 書込OK)", DOWNLOAD_DIR, free_disk_mb())

    _executor = ThreadPoolExecutor(
        max_workers=max(MAX_CONCURRENT * 2, 4),
        thread_name_prefix="ytdl",
    )

    reaper = threading.Thread(target=_reaper_loop, name="reaper", daemon=True)
    reaper.start()

    log.info("起動完了: 同時実行数=%d, file_ttl=%ds, playlist=%s, allow_extractors=%s",
             MAX_CONCURRENT, FILE_TTL_SECONDS, ALLOW_PLAYLIST, ALLOWED_EXTRACTORS or "ALL")
    log.info("リゾルバ: enabled=%s 対象ホスト=%s",
             ENABLE_SITE_RESOLVERS,
             sorted(resolvers.STREAMHG_HOSTS) if ENABLE_SITE_RESOLVERS else "(無効)")

    try:
        yield
    finally:
        log.info("シャットダウン開始")
        _reaper_stop.set()
        if _executor:
            _executor.shutdown(wait=False, cancel_futures=True)
        # エフェメラルストレージなので、終了時に全ファイルを消しておく
        if DOWNLOAD_DIR.exists():
            for path in DOWNLOAD_DIR.glob("*"):
                if path.is_file():
                    _delete_file(str(path), reason="shutdown")


app = FastAPI(
    title="Personal Video Downloader",
    description="自分専用の認証付き動画ダウンローダ (yt-dlp + ffmpeg)",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# Jinja2 テンプレート (このファイルと同じ階層の templates/ を参照)
_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


# ---- エラーハンドラ: ValidationError を 400 JSON に変換 -------------------
@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ===========================================================================
# 9. ルート定義 (Routes)
# ===========================================================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """UI 本体。トークンは localStorage に保存するため、ページ自体は公開で OK。"""
    return templates.TemplateResponse(
        request,                       # 新シグネチャ (Starlette >= 0.29)
        "index.html",
        {
            # 旧シグネチャでも動くよう request を context にも入れておく
            "request": request,
            "app_version": APP_VERSION,
            "qualities": [
                {"id": k, "label": v["label"]} for k, v in QUALITY_PRESETS.items()
            ],
            "allow_playlist": ALLOW_PLAYLIST,
            "file_ttl": FILE_TTL_SECONDS,
            "resolvers_enabled": ENABLE_SITE_RESOLVERS,
            "resolver_sites": resolvers.describe_support() if ENABLE_SITE_RESOLVERS else {},
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> JSONResponse:
    """404 ノイズを避けるための空レスポンス。"""
    return JSONResponse(status_code=204, content=None)


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    """
    認証不要のヘルスチェック (Railway の Healthcheck Path に /api/health を設定可能)。
    """
    with JOBS_LOCK:
        counts: Dict[str, int] = {}
        for job in JOBS.values():
            counts[job.state] = counts.get(job.state, 0) + 1
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "auth_configured": bool(AUTH_TOKEN),
        "jobs": counts,
        "free_disk_mb": round(free_disk_mb(), 1),
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


@app.get("/api/presets")
async def presets(_: str = Depends(require_auth)) -> Dict[str, Any]:
    """UI が使う設定値 (品質プリセットなど) を返す。"""
    return {
        "qualities": [{"id": k, "label": v["label"]} for k, v in QUALITY_PRESETS.items()],
        "allow_playlist": ALLOW_PLAYLIST,
        "file_ttl_seconds": FILE_TTL_SECONDS,
        "job_ttl_seconds": JOB_TTL_SECONDS,
        "max_filesize_mb": MAX_FILESIZE_MB,
        "max_concurrent": MAX_CONCURRENT,
        "allowed_extractors": ALLOWED_EXTRACTORS,
        "resolvers_enabled": ENABLE_SITE_RESOLVERS,
        "resolver_sites": resolvers.describe_support() if ENABLE_SITE_RESOLVERS else {},
        "resolver_extra_hosts": RESOLVER_EXTRA_HOSTS,
    }


@app.post("/api/download", status_code=202)
async def create_download(
    payload: DownloadRequest = Body(...),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    """
    ダウンロードジョブを新規作成し、即座に 202 Accepted を返す。
    実際の取得はバックグラウンドスレッドで進行 → /api/status/{job_id} で進捗確認。
    """
    # --- 入力バリデーション (SSRF / スキーム / 内部IP) ---
    url = validate_url(payload.url)

    # --- ディスク容量の事前チェック ---
    free_mb = free_disk_mb()
    if free_mb < MIN_FREE_DISK_MB:
        raise HTTPException(
            status_code=507,
            detail=f"サーバの空き容量が不足しています (残り {free_mb:.0f}MB)。しばらく待ってから再試行してください。",
        )

    # --- プレイリスト許可チェック ---
    allow_playlist = bool(payload.playlist and ALLOW_PLAYLIST)
    if payload.playlist and not ALLOW_PLAYLIST:
        raise HTTPException(
            status_code=400,
            detail="プレイリストの一括取得はこのサーバでは無効化されています (ALLOW_PLAYLIST=1 で有効化)。",
        )

    # ワーカープールの準備を確認してからジョブを登録する。
    # (登録後に失敗すると queued のまま履歴に残り続けてしまうため)
    if _executor is None:  # 通常は起きない (lifespan 未通過)
        raise HTTPException(status_code=503, detail="サーバがまだ初期化中です。少し待って再試行してください。")

    # Referer / User-Agent はヘッダインジェクション対策の検証を通す
    referer = validate_referer(payload.referer)
    user_agent = validate_header_value(payload.user_agent, "user_agent")

    job = Job(
        id=str(uuid.uuid4()),
        url=url,
        quality=payload.normalized_quality(),
        audio_only=(payload.normalized_quality() == "audio"),
        allow_playlist=allow_playlist,
        referer=referer,
        user_agent=user_agent,
    )
    _register(job)
    _executor.submit(_run_download, job)
    log.info("[job=%s] 受付完了 (queue 投入)", job.id)

    return {"job": job.public(), "message": "ダウンロードを開始しました。"}


@app.get("/api/jobs")
async def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    """直近のジョブ一覧 (新しい順)。ページ再読み込み後の履歴復元に使う。"""
    with JOBS_LOCK:
        jobs = list(JOBS.values())[::-1][:limit]
    return {"count": len(jobs), "jobs": [j.public() for j in jobs]}


@app.get("/api/status/{job_id}")
async def job_status(job_id: str, _: str = Depends(require_auth)) -> Dict[str, Any]:
    """
    ポーリング用エンドポイント。1 件のジョブの現在状態を返す。
    """
    job_id = validate_job_id(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません (サーバ再起動で消えた可能性があります)。")
    return {"job": job.public()}


@app.get("/api/stream/{job_id}")
async def job_stream(
    job_id: str,
    request: Request,
    _: str = Depends(require_auth),
) -> StreamingResponse:
    """
    おまけ: Server-Sent Events 版の進捗ストリーム。
    フロントは既定でポーリングを使いますが、下記のように SSE も利用可能です。

        const es = new EventSource(`/api/stream/${id}?token=${TOKEN}`);
        es.onmessage = (e) => render(JSON.parse(e.data).job);
    """
    job_id = validate_job_id(job_id)

    async def event_generator():
        # 初回は hello を送って接続確立を知らせる
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                yield f"event: error\ndata: {{\"detail\": \"job not found: {job_id}\"}}\n\n"
                break
            snapshot = job.public()
            # SSE の data には改行を含められないため JSON 一行で送る
            yield f"event: progress\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if snapshot["state"] in (JobState.FINISHED, JobState.ERROR):
                yield "event: done\ndata: {}\n\n"
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # プロキシでのバッファリング抑止
        },
    )


@app.post("/api/info")
async def probe_info(
    payload: InfoRequest = Body(...),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    """
    ダウンロードせずに URL のメタ情報だけ取得する (プレビュー用)。
    ブロッキング処理なので run_in_threadpool でイベントループを止めない。
    """
    url = validate_url(payload.url)

    referer = validate_referer(payload.referer)
    user_agent = validate_header_value(payload.user_agent, "user_agent")

    # サイト固有リゾルバが使えるなら、先に実メディア URL へ解決しておく
    probe_url = url
    probe_referer = referer
    if ENABLE_SITE_RESOLVERS:
        try:
            resolved = resolvers.resolve_if_supported(
                url, user_agent=user_agent, validate_url=validate_url)
        except (ResolveError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if resolved is not None:
            probe_url = resolved.media_url
            probe_referer = probe_referer or resolved.referer

    def _probe() -> Dict[str, Any]:
        opts = _probe_opts(allow_playlist=False, referer=probe_referer,
                           user_agent=user_agent)
        opts["extract_flat"] = "in_playlist"   # プレイリストは中身を展開せず軽く確認
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(probe_url, download=False) or {}
        # ホワイトリスト設定時は、許可されていないサイトをここで弾く
        _assert_extractor_allowed(result)
        return result

    try:
        info = await asyncio.to_thread(_probe)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=422, detail=_friendly_error(str(exc))) from exc
    except ExtractionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("info 取得に失敗")
        raise HTTPException(status_code=500, detail=f"情報取得に失敗しました: {exc}") from exc

    formats = [
        {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution"),
            "height": f.get("height"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vbr": f.get("vbr"),
        }
        for f in (info.get("formats") or [])[:40]
    ]

    return {
        "resolved_url": probe_url if probe_url != url else None,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "is_playlist": bool(info.get("_type") == "playlist" or info.get("entries")),
        "entries": len(info.get("entries") or []) if info.get("entries") else None,
        "formats": formats,
    }


@app.get("/api/download/{job_id}")
async def download_file(
    job_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _: str = Depends(require_auth),
) -> FileResponse:
    """
    完了済みファイルをクライアントへ送出する。

    ★ ストレージ自動最適化のポイント ★
    送出が終わった直後に BackgroundTask でローカルファイルを削除する。
    Railway のエフェメラルストレージを圧迫しないための主経路。
    (取りに来られなかったファイルはリーパーが FILE_TTL_SECONDS 後に削除)
    """
    job_id = validate_job_id(job_id)

    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません。")

    with job._lock:
        filepath = job.filepath
        filename = job.filename or f"{job.id}.bin"
        state = job.state

    if state != JobState.FINISHED:
        raise HTTPException(
            status_code=409,
            detail=f"このジョブはまだ完了していません (現在の状態: {state})。",
        )
    if not filepath:
        raise HTTPException(
            status_code=410,
            detail="ファイルは既に自動削除されました。再度ダウンロードを実行してください。",
        )

    # ---- パストラバーサル最終防御 ------------------------------------------
    # job_id は UUID 検証済み & filepath は自サーバが生成した絶対パスだが、
    # 念のため DOWNLOAD_DIR の外を指していないことを確認する。
    target = Path(filepath).resolve()
    if not str(target).startswith(str(DOWNLOAD_DIR.resolve()) + os.sep):
        log.error("不正なファイルパス要求を検知: %s", filepath)
        raise HTTPException(status_code=400, detail="不正なファイルパスです。")
    if not target.is_file():
        raise HTTPException(status_code=410, detail="ファイルが存在しません (既に削除されました)。")

    # ---- 送出後の自動削除を予約 ---------------------------------------------
    def _purge_after_send(path: str, jid: str) -> None:
        _delete_file(path, reason=f"クライアントへ送出済み job={jid[:8]}")
        with JOBS_LOCK:
            j = JOBS.get(jid)
        if j is not None:
            with j._lock:
                j.filepath = None
                j.stage = "送信完了・ファイルを自動削除しました"

    background_tasks.add_task(_purge_after_send, str(target), job.id)

    media_type = (
        mimetypes.guess_type(target.name)[0]
        or ("application/zip" if target.suffix.lower() == ".zip" else "application/octet-stream")
    )
    log.info("[job=%s] 送出開始 %s (%.1fMB)",
             job.id, target.name, target.stat().st_size / 1024 / 1024)

    return FileResponse(
        path=str(target),
        filename=safe_filename(filename, fallback=f"{job.id}.bin"),
        media_type=media_type,
        headers={
            # 同一ファイルを何度も取りに来させない (キャッシュ無効化)
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/api/jobs/{job_id}")
async def cancel_or_delete_job(job_id: str, _: str = Depends(require_auth)) -> Dict[str, Any]:
    """
    ジョブを明示的に破棄する (ファイルを即削除)。
    実行中ジョブは yt-dlp のスレッドを安全に止められないため、
    ファイル削除のみ行い「キャンセル要求済み」として扱う。
    """
    job_id = validate_job_id(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません。")

    _cleanup_partial(job.id)
    with job._lock:
        job.filepath = None

    if job.state in (JobState.QUEUED, JobState.DOWNLOADING, JobState.PROCESSING):
        with job._lock:
            job.stage = "キャンセル要求済み (完了後に自動削除されます)"
        return {"deleted": True, "state": job.state, "message": "キャンセルを要求しました。生成物は削除されます。"}

    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return {"deleted": True, "message": "ジョブを削除しました。"}


# ===========================================================================
# 10. ローカル起動用エントリポイント
# ===========================================================================
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    if not AUTH_TOKEN:
        print("!! 環境変数 AUTH_TOKEN を設定してください。例: AUTH_TOKEN=hogehoge python main.py")
        sys.exit(1)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=_env_flag("RELOAD", False),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
