# ============================================================================
#  自分専用の動画ダウンローダ (FastAPI + yt-dlp + ffmpeg) — Railway 用イメージ
#
#  ビルド:   docker build -t my-video-dl .
#  ローカル: docker run --rm -p 8080:8080 -e AUTH_TOKEN=secret my-video-dl
#  Railway:  リポジトリ直下にこのファイルを置くだけで自動検出されます。
#            (ビルドログに "Using detected Dockerfile!" が出れば OK)
#            ※ ファイル名は先頭大文字の `Dockerfile` である必要があります。
# ============================================================================

FROM python:3.11-slim

# ---- 環境変数 ---------------------------------------------------------------
# PYTHONDONTWRITEBYTECODE : .pyc を作らない (コンテナを汚さない)
# PYTHONUNBUFFERED        : ログを即時出力 (Railway のログ画面にすぐ反映)
# XDG_CACHE_HOME / HOME   : yt-dlp のキャッシュを /tmp 配下に置く。
#                           非 root ユーザでも確実に書き込める場所にするため。
# PORT                    : Railway が実行時に注入する (下の CMD で参照)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/tmp \
    PORT=8080

# ---- システム依存 -----------------------------------------------------------
# ffmpeg      : yt-dlp が「映像ストリーム + 音声ストリーム」を 1 本に
#               マージするために必須。これが無いと高画質 DL が失敗します。
# ca-certificates : https 通信に必要。
# curl        : Docker の HEALTHCHECK と Railway 上でのデバッグ用。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Python 依存 ------------------------------------------------------------
# requirements.txt だけを先に COPY することで、コード変更時に
# pip install のレイヤキャッシュが効くようにする (ビルド高速化)。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- アプリケーション本体 ----------------------------------------------------
# .dockerignore で tests/ や __pycache__ などは除外しています。
COPY . .

# ---- 実行用ユーザ & ダウンロード先ディレクトリ --------------------------------
# root で動かさないための最低限のハードニング。
# /app/downloads は main.py の DOWNLOAD_DIR デフォルト値と一致させています。
#
# ★ 注意: Railway で Volume を /app/downloads にマウントする場合、Volume は
#   root 所有でマウントされるため、appuser では書き込めません。
#   その場合はサービス変数に RAILWAY_RUN_UID=0 を追加してください。
#   (main.py が起動時に書き込み可否を検査し、分かりやすくエラーを出します)
RUN mkdir -p /app/downloads \
 && useradd --create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# ---- ヘルスチェック (ローカル docker 用 / Railway は自前の healthcheckPath) ---
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/api/health" || exit 1

# ---- 起動 -------------------------------------------------------------------
# Railway は $PORT を注入するため、シェル経由で展開する必要がある
# (JSON 配列形式の exec だと $PORT が展開されないので注意)。
#
# * --workers 1              : 進捗はプロセス内メモリで管理しているため、
#                              複数ワーカーにすると状態が分散して壊れます。
#                              必ず 1 にしてください。
# * --proxy-headers /
#   --forwarded-allow-ips='*': Railway の LB 経由でもクライアント IP が
#                              正しく取れるようにする。
# * --timeout-keep-alive 75  : Railway のプロキシ (75秒) より短くして 502 を防ぐ。
CMD uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" \
    --workers 1 --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75

# ============================================================================
#  yt-dlp の更新について
# ----------------------------------------------------------------------------
#  サイト側の仕様変更でダウンロードが失敗するようになったら、コンテナ内で
#  `yt-dlp --update` を実行するのは避けてください (root 所有の site-packages に
#  書き込めず失敗します / 再デプロイで消えます)。
#
#  正しい手順: requirements.txt の `yt-dlp>=...` の下限バージョンを上げて
#              再デプロイ (Railway が自動的に新しいイメージをビルドします)。
# ============================================================================
