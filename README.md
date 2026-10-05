# 🎬 Personal Video Downloader (Railway / FastAPI + yt-dlp + ffmpeg)

**自分だけが使える、認証付きの動画ダウンローダ** です。
URL を貼ると、バックグラウンドで `yt-dlp` がダウンロード → `ffmpeg` が映像と音声をマージ →
進捗バーがリアルタイムで動き → 完了後にブラウザへファイルを送出 → **送出直後にサーバ上のファイルは自動削除** されます
(Railway のエフェメラルストレージを圧迫しません)。

```
video-dl/
├── main.py               # FastAPI アプリ本体 (認証 / ジョブ管理 / 進捗 / 自動削除)
├── templates/
│   └── index.html        # フロントエンド UI (依存 CDN なしのインライン CSS/JS)
├── resolvers.py          # サイト固有リゾルバ (StreamHG / iPlayerHLS)
├── requirements.txt      # Python 依存
├── Dockerfile            # ffmpeg 込みの実行環境 (非 root / uvicorn 起動)
├── .dockerignore
├── .env.example          # 環境変数のサンプル
└── tests/
    ├── smoke_test.py     # 認証・SSRF・進捗・自動削除・リーパー (79項目)
    ├── ffmpeg_test.py    # マージ・MP3・ZIP・許可リスト (68項目)
    ├── referer_test.py   # Referer 透過・ヘッダインジェクション防御 (35項目)
    └── resolver_test.py  # StreamHG 系リゾルバ (模擬サーバで検証, 75項目)
```

---

## ✨ 機能

| 要件 | 実装 |
|---|---|
| **認証** | `AUTH_TOKEN` を `X-API-Key` ヘッダ / `Authorization: Bearer` / `?token=` クエリのいずれかで検証。`hmac.compare_digest` でタイミング攻撃対策。**未設定なら起動を拒否** |
| **非同期 DL** | `ThreadPoolExecutor` + `Semaphore(MAX_CONCURRENT)` でバックグラウンド実行。受付は即 `202 Accepted` |
| **進捗管理** | `yt-dlp` の `progress_hooks` / `postprocessor_hooks` をメモリ上の `Job` に反映。`%`・速度・ETA・ffmpeg 状態まで取得 |
| **進捗取得 API** | ポーリング用 `GET /api/status/{job_id}` と、おまけの SSE `GET /api/stream/{job_id}` |
| **ストレージ自動最適化** | ① 送出完了直後に `BackgroundTask` で削除 ② TTL 超過分をリーパースレッドが削除 ③ 孤立ファイル/`.part` 残骸も回収 ④ シャットダウン時に全削除 |
| **UI** | トークンは `localStorage` 保存、進捗バー、完了後の取得ボタン、履歴、エラー表示、設定の記憶 |
| **エラー処理** | `DownloadError` / `ExtractorError` を捕捉し、英語メッセージを日本語に翻訳して返却 |
| **サイト固有リゾルバ** | `iplayerhls.com` / `streamhg.com` の動画ページ URL を、内部の `.m3u8` + 必要な `Referer` へ自動変換 (`resolvers.py`)。失効ページは日本語で明確に通知 |
| **Referer / UA 透過** | ドメインロックされたプレイヤー (StreamHG 等) 向け。m3u8 本体と全 `.ts` セグメントに自動付与。CRLF インジェクションは 400 で拒否 |
| **セキュリティ** | スキーム許可制、内部 IP/localhost/クラウドメタデータ (169.254.169.254) への SSRF ブロック、UUID 検証によるパストラバーサル遮断、ファイル名サニタイズ、非 root 実行 |

---

## 🚀 Railway へのデプロイ手順

### 1. リポジトリを用意する

この `video-dl/` 配下のファイルを **リポジトリの直下** に置きます
(`Dockerfile` は必ず先頭大文字・ルート直下。Railway が自動検出します)。

```bash
cd video-dl
git init && git add -A && git commit -m "video downloader"
git remote add origin https://github.com/<あなた>/<リポジトリ>.git
git push -u origin main
```

### 2. トークンを生成しておく

```bash
openssl rand -hex 32
# → 64文字の16進文字列が出力されます (これが AUTH_TOKEN の値になります)
```

### 3. Railway でプロジェクトを作成

1. [railway.com](https://railway.com) → **New Project**
2. **Deploy from GitHub repo** を選び、先ほどのリポジトリを選択
   (初回は GitHub アプリのインストール/リポジトリ許可が必要)
3. ビルドログに以下が出れば Dockerfile が使われています:
   ```
   ==========================Using detected Dockerfile!==========================
   ```

### 4. 環境変数を設定する ★最重要

サービスを選択 → **Variables** タブ → **New Variable** (または **Raw Editor** に一括貼り付け):

| 変数名 | 値 | 必須 |
|---|---|---|
| `AUTH_TOKEN` | 手順2で生成した文字列 | **必須** (未設定だと起動失敗します) |
| `ALLOWED_EXTRACTORS` | 例: `youtube` (空なら全サイト) | 任意 |
| `MAX_CONCURRENT` | `2` | 任意 |
| `FILE_TTL_SECONDS` | `600` | 任意 |
| `ALLOW_PLAYLIST` | `1` でプレイリスト一括取得を許可 | 任意 |
| `MAX_FILESIZE_MB` | 例: `2000` (`0`=無制限) | 任意 |

> `PORT` は Railway が自動注入するので **設定しないでください**
> (固定値を入れると起動ポートがズレて 502 になります)。

Raw Editor に貼る場合の例:

```
AUTH_TOKEN=<手順2で生成した64文字の値を貼り付け>
ALLOWED_EXTRACTORS=youtube
MAX_CONCURRENT=2
FILE_TTL_SECONDS=600
```

### 5. ドメインを発行する

サービス → **Settings** → **Networking** → **Generate Domain**
→ `https://<サービス名>.up.railway.app` が発行されます。

### 6. ヘルスチェックと再起動ポリシー

同じ **Settings** → **Deploy** で:

- **Healthcheck Path**: `/api/health`
- **Restart Policy**: `Always`

### 7. リソース (ffmpeg マージは CPU/RAM を使います)

**Settings** → **Resources** で、目安として **1 vCPU / 1GB RAM 以上** を割り当ててください。
デフォルトの最小構成だと、長い動画のマージで OOM / タイムアウトになることがあります。

### 8. 開いて使う

発行されたドメインにアクセス → ①に `AUTH_TOKEN` の値を貼り付け → ②に URL を入れて
**ダウンロード開始**。トークンは `localStorage` に保存されるので、次回からは ②だけで OK です。

---

## 💾 (任意) Volume を付けてストレージを拡張する

コンテナのディスクは小さく、**再デプロイで消える** エフェメラル領域です。
本アプリは「取得後すぐ削除」する設計なので通常は Volume 不要ですが、
大きな動画を扱いたい場合は:

1. プロジェクトキャンバスを右クリック → **Add Volume** → 対象サービスを選択
2. **Mount Path** を `/app/downloads` にする (`DOWNLOAD_DIR` の既定値と同じ)
3. サービス変数に **`RAILWAY_RUN_UID=0`** を追加する ★忘れがち
   → Volume は root 所有でマウントされますが、イメージは非 root (`appuser`) で動くため、
     これを設定しないと書き込めません。
   (未設定の場合、アプリは起動時に検知して対処法をログに出したうえで停止します)

> Volume はネットワークストレージのため、ローカルディスクより IO が遅いです。
> 一時ファイルの書き込みが重くなるので、大容量化が目的でなければ無しを推奨します。

---

## 🐳 ローカルで動かす

### Docker (本番と同じ環境)

```bash
docker build -t my-video-dl .

docker run --rm -p 8080:8080 \
  -e AUTH_TOKEN=local-secret \
  -e BLOCK_PRIVATE_HOSTS=0 \
  my-video-dl
# → http://localhost:8080  (トークン: local-secret)
```

ダウンロード先を永続化して中身を確認したい場合:

```bash
docker run --rm -p 8080:8080 \
  -e AUTH_TOKEN=local-secret \
  -v "$PWD/downloads:/app/downloads" \
  my-video-dl
```

### Docker なし

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# ffmpeg が必要 (マージに使用)
#   macOS : brew install ffmpeg
#   Debian: sudo apt install ffmpeg

export AUTH_TOKEN=local-secret
export DOWNLOAD_DIR=/tmp/downloads
python main.py            # → http://localhost:8080
```

### テスト

```bash
pip install httpx                      # TestClient に必要
python tests/smoke_test.py             # 認証/SSRF/進捗/自動削除/リーパー
# ffmpeg を使うテスト (事前にテスト用メディアを生成しておく)
python tests/ffmpeg_test.py            # マージ/MP3/ZIP/抽出元許可リスト
```

---

## 🔗 対応サイト: iPlayerHLS / StreamHG (自動解決)

**`iplayerhls.com` と `streamhg.com` は、動画ページの URL を貼るだけでダウンロードできます。**

これらのサイトは yt-dlp に専用 extractor がありませんが、本アプリが
**リゾルバ層** (`resolvers.py`) で「動画ページ → 実際の `.m3u8`」を自動変換し、
必要な `Referer` も自動で付与します。

### 対応している URL 形式

| 形式 | 例 |
|---|---|
| 埋め込みプレイヤー | `https://iplayerhls.com/e/<file_code>` |
| 埋め込み (`.html` 付き) | `https://iplayerhls.com/e/<file_code>.html` |
| 視聴ページ | `https://iplayerhls.com/<file_code>` / `.html` |
| ダウンロードページ | `https://iplayerhls.com/d/<file_code>` |
| ファイルページ | `https://iplayerhls.com/f/<file_code>` |
| StreamHG 本体 | `https://streamhg.com/e/<file_code>` など同上 |

対応ホスト: `iplayerhls.com` / `streamhg.com` / `vidshared.com` (`www.` 付きも可)

### 仕組み

```
貼られた URL
   ↓ ① file_code を抽出し /e/<code>.html (プレイヤーページ) へ正規化
   ↓ ② ページを取得 (リダイレクトは都度 SSRF 再検査)
   ↓ ③ 失効ページなら分かりやすいエラーに
   ↓ ④ .m3u8 (無ければ .mp4) を優先順位つきパターンで抽出
   ↓ ⑤ 抽出した URL を validate_url で再検査 (悪意あるページ対策)
   ↓ ⑥ Referer にプレイヤーページ URL を自動設定
yt-dlp (generic extractor) が HLS として取得 → ffmpeg で mp4 に多重化
```

**無効化**: `ENABLE_SITE_RESOLVERS=0`

**ミラードメインの追加**: StreamHG 系はドメインが増えがちなので、コードを変更せずに
環境変数で対象ホストを追加できます (`www.` は自動で付与されます)。

```
RESOLVER_EXTRA_HOSTS=newmirror.example,another.example
```

> 💡 起動ログに `リゾルバ: enabled=True 対象ホスト=[...]` と表示されるので、
> 追加が反映されているか確認できます。

### 調査で判明したこと (2026-10 時点)

| 項目 | 内容 |
|---|---|
| `iplayerhls.com` の正体 | **StreamHG と同一プラットフォーム** (`<title>StreamHG</title>`、共通の `/HG1/` テーマ、`t.me/streamhgofficial` へのリンク、`xupload.js` = XFileShare 系) |
| yt-dlp の対応 | **extractor 無し** (XFileShare 系は現行版から削除済み) |
| `iplayerhls.com` の動画ページ | **HTTP 200 で取得可能** (ブロックなし) → リゾルバで解析できる |
| `streamhg.com` の動画ページ | **全て 403 Forbidden** (nginx + Cloudflare) → 直接は解析不可 |
| 公式 API | `streamhgapi.com/api/file/direct_link?key=<APIキー>&file_code=<ID>&hls=1` が `hls_direct` を返すが、**アカウント所有者の API キー必須** |
| ファイルの寿命 | 無料アカウントの非アクティブファイルは **120 日で削除** → 「File is no longer available」ページになる |

> **注意**: 公開リンクの多くは既に失効しています。ダウンロードに失敗したら、
> まずリンクが生きているか (ブラウザで再生できるか) を確認してください。

> ⚠️ 他者のコンテンツのダウンロードは、サイトの利用規約・著作権・居住国の法令に
> 照らしてご自身の責任で行ってください。

---

## 📡 ドメインロックされたプレイヤー / HLS 直リンク (汎用)

`yt-dlp` に専用 extractor が無い動画ホスティング (StreamHG, StreamWish 系の
XFileShare ホストなど) でも、次の 2 つの方法で取得できる場合があります。

### 方法1: `.m3u8` 直リンクをそのまま貼る (推奨・追加設定なし)

これらのホストは **HLS 配信** (`index.m3u8` + 多数の `.ts` セグメント) を使っています。
本アプリは **m3u8 の直接指定に対応済み** で、ffmpeg が mp4 へ多重化します。

1. 動画をブラウザで再生する
2. **F12** (DevTools) → **Network** タブ → 絞り込み欄に `m3u8`
3. 見つかった `....m3u8` の URL をコピー
4. 本アプリの ② に貼り付けて **ダウンロード開始**

> 検証済み: 4 セグメントの HLS を取得 → mp4 に多重化 → 送出 → 自動削除まで成功。

### 方法2: `Referer` を指定する (403 Forbidden になるとき)

StreamHG などはプレイヤーが **ドメインロック** されており、Referer が無いと
m3u8 も `.ts` セグメントも **403** で拒否されます。

UI の **「詳細設定 — Referer / User-Agent / 直接 m3u8 リンク」** を開き、
**動画ページを表示していた URL** を Referer に入れてください。
サーバ側で yt-dlp の `http_headers` に設定され、**マニフェストと全セグメントの
リクエストに自動的に付与** されます。

```bash
curl -X POST https://<your-app>.up.railway.app/api/download \
  -H "X-API-Key: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://.../index.m3u8","referer":"https://<動画ページのURL>"}'
```

**セキュリティ**: `referer` / `user_agent` は改行 (`\r\n`) などの制御文字を含むと
**400 で拒否** します (ヘッダインジェクション対策)。スキームも http/https のみ許可。

### StreamHG (`streamhg.com`) についての調査結果

| 項目 | 結果 |
|---|---|
| yt-dlp の専用 extractor | **無し** (XFileShare 系 extractor は現行版から削除済み) |
| 動画ページ (`/e/<id>`, `/<id>`, `/f/<id>`, `/d/<id>`, `/v/<id>`) | **全て 403 Forbidden** (nginx + Cloudflare、ドメインロック) |
| 公式 API | `https://streamhgapi.com/api/file/direct_link?key=<APIキー>&file_code=<ID>&hls=1` が `hls_direct` (m3u8) を返す。**全エンドポイントがアカウント所有者の API キー必須** = 自分のファイル専用 |
| ToS | 「Hotlinking is not allowed」と明記 |

→ **自分でアップロードした動画**なら公式 API 連携を追加実装できます
(`STREAMHG_API_KEY` を使う方式。ご要望があれば対応します)。
→ **他人がアップロードした動画**はページが 403 のため、上記「方法1 + 方法2」が
現実的な手段です。専用の extractor を書くには **実在する動画 URL のサンプル** が
必要です (それが無いと解析も動作確認もできません)。

> ⚠️ 他者のコンテンツのダウンロードは、サイトの利用規約・著作権・居住国の法令に
> 照らしてご自身の責任で行ってください。

---

## 🔌 API リファレンス

すべての `/api/*` (health 除く) に認証が必要です。

| Method | Path | 説明 |
|---|---|---|
| `GET` | `/` | UI (HTML)。トークン不要 |
| `GET` | `/api/health` | 死活・空き容量・ffmpeg 有無。**認証不要** (Railway の healthcheck 用) |
| `GET` | `/api/presets` | 品質プリセット等の UI 設定 |
| `POST` | `/api/download` | ジョブ作成 → `202` と `job` を返す |
| `GET` | `/api/status/{job_id}` | 進捗ポーリング用 |
| `GET` | `/api/stream/{job_id}` | SSE 版の進捗ストリーム |
| `GET` | `/api/download/{job_id}` | ファイル取得。**送出後にサーバ上から自動削除** |
| `GET` | `/api/jobs?limit=25` | ジョブ一覧 (新しい順) |
| `DELETE` | `/api/jobs/{job_id}` | ジョブと生成物の即時破棄 |
| `POST` | `/api/info` | DL せずにメタ情報だけ確認 |
| `GET` | `/docs` | 自動生成 API ドキュメント (Swagger UI) |

### `POST /api/download`

```json
{ "url": "https://www.youtube.com/watch?v=...", "quality": "best", "playlist": false }
```

| フィールド | 必須 | 説明 |
|---|---|---|
| `url` | ● | 動画ページ URL / `.m3u8` 直リンク |
| `quality` | | `best` / `1080` / `720` / `480` / `audio`(MP3) |
| `playlist` | | `true` で再生リスト一括 (`ALLOW_PLAYLIST=1` 時のみ) |
| `referer` | | ドメインロックされたプレイヤーが 403 のときに指定 |
| `user_agent` | | User-Agent の上書き |

```bash
curl -X POST https://<your-app>.up.railway.app/api/download \
  -H "X-API-Key: $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","quality":"720"}'
```

### 進捗の確認

```bash
curl -H "X-API-Key: $AUTH_TOKEN" \
  https://<your-app>.up.railway.app/api/status/<job_id>
```

```json
{"job":{"id":"...","state":"downloading","percent":42.7,"stage":"ダウンロード中",
        "speed":"3.21 MB/s","eta":"0:12","title":"...","filename":null,
        "download_path":null,"expires_in":null}}
```

`state`: `queued` → `downloading` → `processing`(ffmpeg) → `finished` / `error`

### ファイル取得 (curl 例)

```bash
# ヘッダを使う場合
curl -L -H "X-API-Key: $AUTH_TOKEN" -o video.mp4 \
  https://<your-app>.up.railway.app/api/download/<job_id>

# ブラウザ直リンクと同じ形 (クエリでトークン)
curl -L -o video.mp4 \
  "https://<your-app>.up.railway.app/api/download/<job_id>?token=$AUTH_TOKEN"
```

> 取得に成功すると **サーバ上のファイルは即座に削除** されます。
> 2 回目は `410 Gone` になります (仕様です)。

---

## 🔒 セキュリティメモ

**実装済みの対策**

- **SSRF**: `ALLOWED_SCHEMES` (既定 `http,https`) 以外のスキーム (`file://`, `gopher://` 等) を拒否。
  さらに `localhost` / `127.0.0.1` / `[::1]` / `169.254.169.254`(クラウドのメタデータ) /
  `10.x` / `172.16-31.x` / `192.168.x` / `.local` / `.internal` / 10進整数表記 IP を拒否。
  URL 内の `user:pass@` も拒否。
- **パストラバーサル**: `job_id` は UUID 形式を正規表現で厳密検証。
  実ファイル名は常にサーバ生成の `{job_id}....` で、ユーザー入力はパスに入りません。
  送出前にも `DOWNLOAD_DIR` 配下であることを `resolve()` して再確認します。
- **ファイル名**: `Content-Disposition` と ZIP 内エントリ名は `safe_filename()` で
  ディレクトリ区切り・`..`・制御文字を除去。
- **認証**: `hmac.compare_digest` による定時間比較。`AUTH_TOKEN` 未設定なら起動拒否。
- **情報漏えい**: レスポンスにサーバ内絶対パス (`filepath`) は含めません。
- **実行ユーザ**: コンテナは非 root (`appuser`) で起動。
- **抽出元制限**: `ALLOWED_EXTRACTORS` を設定すると、yt-dlp 純正の
  `allowed_extractors` により許可外サイトは **ダウンロードを開始する前に** 弾かれます。

**残るトレードオフ (理解した上で使ってください)**

1. **`?token=` をクエリで受け付けています。** ブラウザが直接開く
   `<a href>` / `location.href` にはカスタムヘッダを付けられないため必要でした。
   クエリのトークンはアクセスログに残り得ます。気になる場合は:
   - Railway のドメインを自分専用にする (既定で公開 URL になります)
   - UI の「リンクをコピー」機能を使わない
   - `main.py` の `require_auth()` からクエリ受理部分を削除する
     (ただし UI の完了後ダウンロードは `fetch` + Blob 方式への書き換えが必要です)
2. **UI 自体は認証なしで表示されます** (トークンを入力する画面なので当然)。
   API はすべて保護されています。
3. **`localStorage` にトークンを保存** します。共有 PC では UI の
   「保存する」チェックを外してください。
4. **ダウンロードはあなたのサーバの IP から行われます。**
   YouTube はデータセンター IP をボット判定することがあり、その場合は
   `COOKIES_FILE` に cookies.txt を渡す必要があります (下記)。

### 年齢制限 / メンバー限定 / ボット判定への対処

1. ブラウザの拡張機能 (例: "Get cookies.txt LOCALLY") で対象サイトの
   `cookies.txt` をエクスポート
2. Railway Volume を `/app/downloads` にマウントし、
   `railway volume files upload ./cookies.txt /app/downloads/cookies.txt` でアップロード
   (またはビルド時にイメージへ含める — 非推奨)
3. 環境変数 `COOKIES_FILE=/app/downloads/cookies.txt` を設定して再デプロイ

---

## 🩺 トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| デプロイが即失敗、ログに `AUTH_TOKEN が設定されていない` | 変数 `AUTH_TOKEN` を設定して再デプロイ |
| ログに `保存先ディレクトリに書き込めません` | Volume マウント時に `RAILWAY_RUN_UID=0` を追加 (または `DOWNLOAD_DIR=/tmp/downloads`) |
| 502 Bad Gateway | `PORT` を手動設定していないか確認 (Railway 注入に任せる)。CMD が `--port "${PORT:-8080}"` になっているか |
| `ffmpeg の処理に失敗しました` | イメージに ffmpeg が入っているか → `/api/health` の `"ffmpeg": true` を確認。RAM 不足のこともあるので Resources を上げる |
| `Sign in to confirm you're not a bot` | YouTube のデータセンター IP 判定。上記の `COOKIES_FILE` を設定 |
| `この動画は視聴できません` | 非公開 / 削除済み / 地域制限 / メンバー限定 |
| 進捗が止まって見える (`processing`) | ffmpeg でマージ中。動画が長いと数分かかります (この間は不定バー表示) |
| 2 回目のダウンロードが `410 Gone` | 仕様です。取得完了時にサーバ上から削除しています。再度ジョブを実行してください |
| 履歴が消えた | ジョブはメモリ管理のため、再デプロイ/再起動で消えます (永続化はしていません) |
| `507` が返る | 空き容量が `MIN_FREE_DISK_MB` 未満。既存ファイルの掃除を待つか Volume を拡張 |
| ダウンロードが遅い / 失敗する | `MAX_CONCURRENT` を 1 に下げる、`quality` を `720`/`480` に下げる |

**yt-dlp の陳腐化**: サイト側の仕様変更で急に失敗するようになったら、
`requirements.txt` の `yt-dlp>=...` を新しい日付バージョンに上げて再デプロイしてください。

---

## ⚖️ 免責

本ツールは **自分が権利を持つ、あるいは利用規約が認めるコンテンツの取得** を想定した
個人利用向けの構成例です。ダウンロードするコンテンツの著作権・各サイトの利用規約・
居住国の法令は、利用者自身の責任で確認してください。
公開サーバとして第三者に開放しないでください (認証付きでも、帯域・法的リスクを
あなた自身が負います)。
