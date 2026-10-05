"""
============================================================================
 resolvers.py — サイト固有の「URL 解決 (リゾルバ)」層
============================================================================

yt-dlp に専用 extractor が無いサイトについて、
「動画ページの URL」→「実際のメディア URL (.m3u8 / .mp4)」へ変換する。

■ なぜ extractor ではなくリゾルバなのか
  * yt-dlp の private API (_ies の並び替え等) に依存しないため、
    yt-dlp のバージョンアップで壊れにくい。
  * 解決結果 (m3u8) は yt-dlp の generic extractor が確実に扱える
    (= HLS の取得・ffmpeg での多重化は既存の検証済み経路をそのまま使う)。
  * 403 を防ぐための Referer を、解決の過程で自動的に決定できる。
  * ローカルに模擬サーバを立てて、単体テストで完全に検証できる。

■ 現在の対応サイト
  StreamHG / iPlayerHLS (同一プラットフォーム, XFileShare 系)
    - iplayerhls.com … StreamHG のプレイヤー用ドメイン (<title>StreamHG</title>)
    - streamhg.com   … 本体
    埋め込み : https://<host>/e/<file_code>[.html]
    視聴ページ: https://<host>/<file_code>[.html]
    DL ページ : https://<host>/d/<file_code> , /f/<file_code>

■ セキュリティ
  * 取得するページは「既に validate_url を通過した URL」のみ。
  * リダイレクトは自動追従せず、追従先を再度 validate_url で検査する
    (リダイレクト経由の SSRF を防ぐ)。
  * ページ内から抽出した m3u8 URL も **必ず validate_url を通す**
    (悪意あるページが内部 IP を指すケースを防ぐ)。
  * レスポンスサイズと時間に上限を設ける (DoS / メモリ枯渇対策)。
============================================================================
"""

from __future__ import annotations

import gzip
import html as html_lib
import io
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

log = logging.getLogger("video-dl.resolver")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
FETCH_TIMEOUT: int = 25           # ページ取得のタイムアウト (秒)
MAX_PAGE_BYTES: int = 4 * 1024 * 1024   # 読むページサイズの上限 (4MB)
MAX_REDIRECTS: int = 5

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ResolveError(Exception):
    """解決に失敗したことを表す。message はそのままユーザーへ返す文言。"""


@dataclass
class ResolveResult:
    """リゾルバの戻り値。"""

    media_url: str                            # yt-dlp に渡す実際のメディア URL
    title: Optional[str] = None               # 取れれば動画タイトル
    referer: Optional[str] = None             # 403 回避用の Referer
    extra_headers: Dict[str, str] = field(default_factory=dict)
    resolver: str = ""                        # どのリゾルバが解決したか (ログ用)
    page_url: str = ""                        # 解決に使用したページ URL


# ---------------------------------------------------------------------------
# HTTP 取得 (リダイレクトを手動制御して SSRF を防ぐ)
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """自動リダイレクトを無効化し、Location を呼び出し側で検査させる。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def fetch_page(
    url: str,
    referer: Optional[str] = None,
    user_agent: Optional[str] = None,
    validate_url: Optional[Callable[[str], str]] = None,
) -> Tuple[str, str]:
    """
    HTML ページを取得して (最終URL, 本文) を返す。

    validate_url を渡すと、リダイレクト先を都度検査する (SSRF 対策)。
    """
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        headers = {
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(current, headers=headers)
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read(MAX_PAGE_BYTES + 1)
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_PAGE_BYTES + 1)
                    except OSError:
                        pass
                return current, raw.decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            # 3xx は HTTPError として上がる (redirect_request が None のため)
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location")
                if not loc:
                    raise ResolveError(f"リダイレクト先が不明です (HTTP {exc.code})。")
                nxt = urljoin(current, loc)
                # リダイレクト先を再検証してから追従する
                if validate_url is not None:
                    validate_url(nxt)
                current = nxt
                referer = url  # 元のページを Referer にしておく
                continue
            if exc.code == 403:
                raise ResolveError(
                    "ページへのアクセスが拒否されました (403 Forbidden)。\n"
                    "このサイトは Referer やドメインロックで保護されている可能性があります。"
                )
            if exc.code == 404:
                raise ResolveError("ページが見つかりません (404)。URL を確認してください。")
            raise ResolveError(f"ページの取得に失敗しました (HTTP {exc.code})。")
        except Exception as exc:  # noqa: BLE001
            raise ResolveError(f"ページの取得に失敗しました: {type(exc).__name__}: {exc}")
    raise ResolveError("リダイレクト回数が上限を超えました。")


# ---------------------------------------------------------------------------
# HTML からの m3u8 / mp4 URL 抽出
# ---------------------------------------------------------------------------
# 優先順位つき。JWPlayer / VideoJS / プレーンな <source> の順に探す。
_MEDIA_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("jwplayer-file", re.compile(r'''["']?file["']?\s*:\s*["']([^"']+?\.m3u8[^"']*)["']''', re.I)),
    ("source-file", re.compile(r'''\bsources?\s*:\s*\[\s*\{[^}]*?["']?file["']?\s*:\s*["']([^"']+?\.m3u8[^"']*)["']''', re.I | re.S)),
    ("html-source", re.compile(r'''<source[^>]+src\s*=\s*["']([^"']+?\.m3u8[^"']*)["']''', re.I)),
    ("src-colon", re.compile(r'''\bsrc\s*:\s*["']([^"']+?\.m3u8[^"']*)["']''', re.I)),
    ("playlist-colon", re.compile(r'''\b(?:playlist|manifest|hls|url)\s*[:=]\s*["']([^"']+?\.m3u8[^"']*)["']''', re.I)),
    # 最後の砦: 文中の .m3u8 URL を直接スキャン
    ("bare-scan", re.compile(r'''(https?:\\/?\\/[^"'\s<>\\]+?\.m3u8[^"'\s<>\\]*)''', re.I)),
]

# m3u8 が取れなかった場合のフォールバック (直リンク mp4)
_DIRECT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("html-source-mp4", re.compile(r'''<source[^>]+src\s*=\s*["']([^"']+?\.mp4[^"']*)["']''', re.I)),
    ("file-mp4", re.compile(r'''["']?file["']?\s*:\s*["']([^"']+?\.mp4[^"']*)["']''', re.I)),
]

# 「ファイルが無い」系ページの判定文言
_GONE_PATTERNS = [
    "file is no longer available",
    "expired or has been deleted",
    "file not found",
    "no such file",
    "the file you are looking for is not available",
    "file was deleted",
    "video not found",
]

_TITLE_PATTERNS = [
    re.compile(r"<title[^>]*>\s*(.+?)\s*</title>", re.I | re.S),
    re.compile(r'''["']?title["']?\s*:\s*["']([^"']{3,200})["']''', re.I),
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
]


def _unescape(value: str) -> str:
    """JS/HTML 由来のエスケープを解除して、素の URL に戻す。"""
    v = value.strip()
    v = v.replace("\\/", "/")            # JS の JSON エスケープ
    v = v.replace("\\u002F", "/").replace("\\u002f", "/")
    v = v.replace("\\u003A", ":").replace("\\u003a", ":")
    v = v.replace("\\/", "/")
    v = html_lib.unescape(v)             # &amp; などの HTML エスケープ
    v = v.replace("\\", "")              # 残ったバックスラッシュ
    return v.strip()


def extract_media_urls(page_html: str) -> List[Tuple[str, str]]:
    """
    ページ HTML から (発見方法, メディアURL) のリストを優先順位つきで返す。
    """
    found: List[Tuple[str, str]] = []
    seen = set()

    for how, pattern in _MEDIA_PATTERNS:
        for m in pattern.findall(page_html):
            url = _unescape(m)
            if url.startswith("//"):
                url = "https:" + url
            if not url.lower().startswith(("http://", "https://")):
                continue
            if url not in seen:
                seen.add(url)
                found.append((how, url))

    if not found:
        # m3u8 が無い場合は直リンク mp4 を探す
        for how, pattern in _DIRECT_PATTERNS:
            for m in pattern.findall(page_html):
                url = _unescape(m)
                if url.startswith("//"):
                    url = "https:" + url
                if url.lower().startswith(("http://", "https://")) and url not in seen:
                    seen.add(url)
                    found.append((how + "(direct)", url))
    return found


def extract_title(page_html: str) -> Optional[str]:
    """ページから動画タイトルらしき文字列を取り出す。"""
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(page_html)
        if not m:
            continue
        title = html_lib.unescape(m.group(1)).strip()
        title = re.sub(r"\s+", " ", title)
        # サイト名だけのタイトルは情報量がないので無視する
        low = title.lower()
        if not title or low in {"streamhg", "iplayerhls", "player", "index of /player"}:
            continue
        # 「動画名 - StreamHG」のような装飾を取り除く
        for sep in (" | ", " - ", " :: "):
            if sep in title:
                head = title.split(sep)[0].strip()
                if len(head) >= 3:
                    title = head
                    break
        return title[:200] or None
    return None


def detect_gone(page_html: str) -> Optional[str]:
    """「失効・削除済み」ページなら、その旨のメッセージを返す。"""
    low = page_html.lower()
    # ページが小さい (= プレイヤーが無くメッセージだけ) 場合のみ判定する。
    # 大きな正常ページ内にたまたま文言が含まれる誤爆を防ぐため。
    if len(page_html) > 4000:
        return None
    for needle in _GONE_PATTERNS:
        if needle in low:
            return (
                "このファイルは既に失効または削除されています。\n"
                "(StreamHG 系のホストは無料アカウントの非アクティブなファイルを "
                "120 日で削除します)"
            )
    return None


# ---------------------------------------------------------------------------
# StreamHG / iPlayerHLS リゾルバ
# ---------------------------------------------------------------------------
# 対応ホスト。同一プラットフォーム (XFileShare / "HG" テーマ) のドメイン群。
STREAMHG_HOSTS = {
    "iplayerhls.com",
    "www.iplayerhls.com",
    "streamhg.com",
    "www.streamhg.com",
    "vidshared.com",
    "www.vidshared.com",
}

# file_code は 12 文字前後の英数字 (例: tosva74t17xo, svdyfxg6p0up)
_FILE_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")

# URL パターン:
#   /e/<code>[.html]  埋め込みプレイヤー  ← これが本命
#   /d/<code>         ダウンロードページ
#   /f/<code>         ファイルページ
#   /<code>[.html]    視聴ページ
_STREAMHG_PATTERNS = [
    re.compile(r"^/(?P<kind>e|d|f)/(?P<code>[A-Za-z0-9]+?)(?:\.html?)?/?$", re.I),
    re.compile(r"^/(?P<code>[A-Za-z0-9]{6,32})(?:\.html?)?/?$", re.I),
]


def is_streamhg_url(url: str) -> bool:
    """StreamHG 系の動画 URL かどうかを判定する。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in STREAMHG_HOSTS:
        return False
    return parse_streamhg_code(url) is not None


def parse_streamhg_code(url: str) -> Optional[str]:
    """URL から file_code を取り出す。対応しない形式なら None。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in STREAMHG_HOSTS:
        return None
    path = parsed.path or "/"
    for pattern in _STREAMHG_PATTERNS:
        m = pattern.match(path)
        if not m:
            continue
        code = m.groupdict().get("code") or ""
        if _FILE_CODE_RE.match(code):
            return code
    return None


def _reconstruct_netloc(parsed, default_host: str = "iplayerhls.com") -> str:
    """
    scheme://netloc を再構成する。

    ★ parsed.hostname はポート番号を含まないため、そのまま使うと
      標準ポート以外 (例: http://host:8080/e/xxx) で接続先を失う。
      既定ポート (http=80 / https=443) 以外なら明示的に付け直す。
    """
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    host = (parsed.hostname or default_host).lower()
    # IPv6 リテラル ([::1]) は角括弧を復元する
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = None
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        return f"{host}:{port}"
    return host


def embed_url(url: str, code: str) -> str:
    """file_code から埋め込みプレイヤー URL を組み立てる。"""
    parsed = urlparse(url)
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    netloc = _reconstruct_netloc(parsed)
    return f"{scheme}://{netloc}/e/{code}.html"


def resolve_streamhg(
    url: str,
    user_agent: Optional[str] = None,
    validate_url: Optional[Callable[[str], str]] = None,
) -> ResolveResult:
    """
    StreamHG / iPlayerHLS の動画ページ URL を、実際の .m3u8 へ解決する。

    手順:
      1. URL から file_code を抽出し、埋め込みプレイヤー URL (/e/<code>.html) を作る
      2. そのページを取得する (Referer には元の URL を付ける)
      3. 失効ページなら分かりやすいエラーにする
      4. ページ内の .m3u8 (無ければ .mp4) を優先順位つきで抽出する
      5. 抽出した URL を validate_url で再検査する (SSRF 対策)
      6. 後続のセグメント取得が 403 にならないよう、Referer に
         埋め込みページ URL を設定して返す
    """
    code = parse_streamhg_code(url)
    if not code:
        raise ResolveError("StreamHG 系の URL から file_code を解釈できませんでした。")

    page = embed_url(url, code)
    log.info("[resolver] streamhg: code=%s page=%s", code, page)

    body_html = fetch_page(
        page,
        referer=url if url != page else None,
        user_agent=user_agent,
        validate_url=validate_url,
    )[1]

    # --- 失効 / 削除 ---
    gone = detect_gone(body_html)
    if gone:
        raise ResolveError(gone)

    # --- メディア URL の抽出 ---
    candidates = extract_media_urls(body_html)
    if not candidates:
        raise ResolveError(
            "プレイヤーページから .m3u8 / .mp4 の URL を見つけられませんでした。\n"
            "ページが難読化された JavaScript でマニフェストを組み立てている可能性があります。\n\n"
            "【対処法】ブラウザの DevTools (F12) → Network → 絞り込みに m3u8 と入力し、\n"
            "表示された .m3u8 の URL を本アプリの URL 欄に直接貼り付けてください。\n"
            "403 になる場合は「詳細設定」の Referer に動画ページの URL を指定します。"
        )

    title = extract_title(body_html)

    # 優先順位が最も高い候補から試し、validate_url を通ったものを使う
    last_error: Optional[str] = None
    for how, media in candidates:
        try:
            if validate_url is not None:
                validate_url(media)
            log.info("[resolver] streamhg: 解決成功 method=%s title=%s", how, title or "(不明)")
            return ResolveResult(
                media_url=media,
                title=title,
                # セグメント (.ts) 取得時の 403 を防ぐため、プレイヤーページを Referer にする
                referer=page,
                extra_headers={},
                resolver=f"streamhg/{how}",
                page_url=page,
            )
        except Exception as exc:  # noqa: BLE001 - validate_url 由来の ValidationError 含む
            last_error = str(exc)
            log.warning("[resolver] 候補をスキップ (%s): %s", how, last_error[:120])
            continue

    raise ResolveError(
        "ページから URL を抽出できましたが、すべてセキュリティ検査で拒否されました。\n"
        f"詳細: {last_error or '不明'}"
    )


# ---------------------------------------------------------------------------
# 汎用ディスパッチ
# ---------------------------------------------------------------------------
def resolve_if_supported(
    url: str,
    user_agent: Optional[str] = None,
    validate_url: Optional[Callable[[str], str]] = None,
) -> Optional[ResolveResult]:
    """
    対応サイトなら解決結果を、対応外なら None を返す。

    呼び出し側 (main.py) は None のとき URL をそのまま yt-dlp に渡す。
    """
    if is_streamhg_url(url):
        return resolve_streamhg(url, user_agent=user_agent, validate_url=validate_url)
    return None


def describe_support() -> Dict[str, List[str]]:
    """UI / API から「対応サイト」を案内するための情報。"""
    return {
        "streamhg": sorted(
            {h for h in STREAMHG_HOSTS if not h.startswith("www.")}
        ),
    }
