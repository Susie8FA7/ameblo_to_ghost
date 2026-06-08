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
- `--diff-only`: `fetched_urls.json` に存在しない新規記事だけを出力
- `--output-dir DIR`: `ghost-import.json` と `images_manifest.csv` の出力先ディレクトリ
- `--max-page-scan N`: 年別/月別補完時に探索する `page-N.html` の上限。デフォルトは500
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

`--year YYYY` 指定時のデフォルト出力先は `outputYYYY/` です。`--diff-only` と併用した場合は `outputYYYYDiff/` へ出力します。

画像はローカルへダウンロードし、本文HTML内の画像パスは `/content/images/YYYY/MM/filename.ext` 形式へ書き換えます。

## 年別取得と差分出力

指定年だけ取得する場合:

```bash
python ameblo_to_ghost.py \
  --base-url "https://ameblo.jp/YOUR_AMEBLO_ID/" \
  --year 2026 \
  --author-id "YOUR_EXISTING_GHOST_USER_ID" \
  --no-users \
  --remove-duplicate-noscript-images
```

過去に取得済みの記事を除外し、新しく見つかった記事だけを出力する場合:

```bash
python ameblo_to_ghost.py \
  --base-url "https://ameblo.jp/YOUR_AMEBLO_ID/" \
  --year 2026 \
  --diff-only \
  --author-id "YOUR_EXISTING_GHOST_USER_ID" \
  --no-users \
  --remove-duplicate-noscript-images
```

`--diff-only` は `--refresh` と併用できません。差分実行中は、`ghost-import.json` の書き出し完了後にだけ `fetched_urls.json` を更新します。中断やタイムアウトでJSONに出ていないURLだけキャッシュが進む状態を避けます。

## page-N補完

Amebloの月別アーカイブ `archive-YYYYMM.html` は20件で打ち切られ、古い同月記事が `page-N.html` 側にしか出ない場合があります。

このツールは、`archive-YYYYMM.html` で指定年月の記事URLがちょうど20件見つかった月だけ、取得済みの指定年月記事URLをアンカーとして `page-N.html` を軽量探索します。アンカーが含まれるページが見つかったら、その周辺 `N-2` から `N+2` だけを補完候補にします。

探索段階では本文をparseせず、一覧HTML内のリンクだけを見ます。候補記事は本文解析後に `published_at` で年/月を確認し、指定範囲外の記事は出力しません。

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
- 生成するソースコードとREADMEはLF改行を前提にしています。

## History

### Ver. 1.5.0

#### Amebloの月別アーカイブ (archive-YYYYMM.html) は、記事が20件を超える月でも20件しか返さない場合があることを確認しました。

#### 実際に2020年12月の記事移行時、月別アーカイブから取得できなかった記事が複数存在し、それらは通常の時系列ページ (page-N.html) にのみ掲載されていました。

#### この問題に対応するため、以下を実装しました。
- archive-YYYYMM.html が20件ちょうどの場合のみ補完探索を実施
- 既知記事URLをアンカーとして page-N を探索
- アンカーを含むページ周辺のみ補完対象とする軽量探索方式を採用
- --max-page-scan オプションを追加
- --diff-only 実行時の fetched_urls.json 更新タイミングを改善

#### 実運用で確認した事例
- 2020年12月: 6記事を追加発見
- 2023年4月: 1記事を追加発見
- 2016年の記事が page-200 以降に存在するケースを確認

#### そのため、Amebloから完全移行を行う場合は、年別取得時の page-N 補完機能を有効にすることを推奨します。

## 個人情報の扱い

このGitHubリリース用ファイルでは、以下の実値はプレースホルダー化されています。

- Amebloアカウント名
- AmebloブログURL
- Ghost user id
- Ghost author name
- Ghost author slug
- Ghost author email

公開前に `fetched_urls.json`, `output/`, `logs/`, `work/` などの生成物を含めないようにしてください。
