# Ameblo to Ghost Import JSON

Python `requests` + `BeautifulSoup4` でAmebloの公開記事を取得し、Ghost 5.x Import JSONを生成するツールです。

このリリース版では、Amebloアカウント名やGhostユーザーIDなどの個人依存値はコード内に固定していません。実行時にオプションで指定してください。

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShellの場合:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 最小実行例

```bash
python ameblo_to_ghost.py \
  --base-url "https://ameblo.jp/YOUR_AMEBLO_ID/" \
  --year 2026 \
  --author-id "YOUR_GHOST_USER_ID" \
  --author-name "Your Name" \
  --author-slug "your-name" \
  --author-email "your-name@example.invalid" \
  --remove-duplicate-noscript-images
```

既存Ghostユーザーへ紐付け、Import JSONに `data.users` を含めない場合:

```bash
python ameblo_to_ghost.py \
  --base-url "https://ameblo.jp/YOUR_AMEBLO_ID/" \
  --year 2026 \
  --author-id "YOUR_EXISTING_GHOST_USER_ID" \
  --no-users \
  --remove-duplicate-noscript-images
```

## 主なオプション

- `--base-url`: 対象AmebloブログURL。例: `https://ameblo.jp/YOUR_AMEBLO_ID/`
- `--year YYYY`: 指定年の月別アーカイブを12月から1月へ処理
- `--month MM`: `--year` と併用し、指定月だけ取得
- `--limit N`: 取得記事数の上限
- `--full`: 年指定なしで全件探索
- `--refresh`: `fetched_urls.json` のキャッシュを使わず再取得
- `--author-id`: Ghost user id。`posts[].primary_author_id` と `posts_authors[].author_id` に使用
- `--author-name`: `data.users` を出力する場合のユーザー名
- `--author-slug`: `data.users` を出力する場合のユーザーslug
- `--author-email`: `data.users` を出力する場合のメールアドレス
- `--no-users`: `data.users` を出力せず、既存GhostユーザーIDへ紐付け
- `--remove-duplicate-noscript-images`: Ameblo画像ブロック内の重複 `noscript` 画像を削除
- `--debug-title`: タイトル候補と採用タイトルをログ出力

`--remove-feature-image-from-body` は互換用の非推奨エイリアスです。現在は本文画像を削除せず、`--remove-duplicate-noscript-images` と同じ挙動になります。

## 出力

デフォルトでは以下を生成します。

- `output/ghost-import.json`
- `output/content/images/YYYY/MM/`
- `output/images_manifest.csv`
- `logs/errors.csv`
- `fetched_urls.json`

画像はローカルへダウンロードし、本文HTML内の画像パスは `/content/images/YYYY/MM/filename.ext` 形式へ書き換えます。

## Ghost Import JSON

生成するJSONはGhost 5.x Import JSONを前提にしています。

- `posts`
- `tags`
- `posts_tags`
- `posts_authors`
- `users` はデフォルトで出力
- `--no-users` 指定時は `users` を出力しない

記事には以下を設定します。

- `status`: `published`
- `visibility`: `public`
- `primary_author_id`: `--author-id`
- `posts_authors[].author_id`: `--author-id`
- `feature_image`: 本文中の通常画像を優先
- `custom_excerpt`: 空文字列。Amebloテーマ名はGhostタグとして扱う
- `canonical_url`: 元記事URL
- AmebloのOGP/リンクカードは削除せず、カード内URLを通常のテキストリンクへ変換

タグは以下の順で紐付けます。

1. Amebloテーマをprimary tagとして `sort_order = 0`
2. Ameblo hashtag API由来タグ
3. HTML内から検出できたハッシュタグ

タグslugとpost slugは決定論的に生成します。同じ入力なら複数回実行しても同じslugになります。

## Ameblo Hashtag API

Amebloのハッシュタグは記事HTMLに含まれない場合があるため、次のAPIも使います。

```text
https://rapi.blogtag.ameba.jp/hashtag/api/v2/article/tag/{blog_id}/{article_id}
```

`blog_id` は `--base-url` から、`article_id` は `entry-xxxxxxxx.html` から抽出します。

## 注意

- 公開記事のみを対象にしています。
- コメントは取得しません。
- robots.txt とAmeba利用規約に配慮し、短時間に大量アクセスしないでください。
- デフォルトで1〜3秒のアクセス間隔を入れています。
- 通信失敗時は `429`, `500`, `502`, `503`, `504` を対象に自動リトライします。
- それでも失敗したURLや画像は `logs/errors.csv` に記録します。

## 個人情報の扱い

このGitHubリリース用ファイルでは、以下の実値はプレースホルダー化されています。

- Amebloアカウント名
- AmebloブログURL
- Ghost user id
- Ghost author name
- Ghost author slug
- Ghost author email

公開前に `fetched_urls.json`, `output/`, `logs/`, `work/` などの生成物を含めないようにしてください。
