# Ameblo to Ghost Exporter

`requests` と `BeautifulSoup4` で Ameblo の公開記事を取得し、Ghost CMS の Import JSON を生成するツールです。

対象ブログ:

```text
https://ameblo.jp/susie8fa7/
```

## セットアップ

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## dry-run

デフォルトは dry-run として最大10記事だけ取得します。

```bash
python ameblo_to_ghost.py
```

出力:

```text
output/ghost-import.json
output/content/images/YYYY/MM/
output/images_manifest.csv
logs/errors.csv
fetched_urls.json
```

## 全件取得

公開済み記事を全件取得する場合:

```bash
python ameblo_to_ghost.py --full
```

アクセス間隔はデフォルトで1〜3秒です。変更する場合:

```bash
python ameblo_to_ghost.py --full --min-delay 1.5 --max-delay 3.5
```

通信失敗時は `429`, `500`, `502`, `503`, `504` を対象に自動リトライします。それでも失敗したURLは `logs/errors.csv` に記録されます。

## 年別取得

指定した年の記事だけを月別アーカイブから取得する場合:

```bash
python ameblo_to_ghost.py --year 2026 --remove-duplicate-noscript-images
```

`--year` 指定時は `archive-YYYYMM.html` を使い、12月から1月へ新しい月順に処理します。存在しない月や記事がない月はスキップします。`--limit` 未指定ならその年の全件を対象にし、`--limit` を指定した場合だけ上限件数を適用します。

既存Ghostユーザーへ紐付け、`data.users` をImport JSONに含めない場合:

```bash
python ameblo_to_ghost.py --year 2026 --remove-duplicate-noscript-images --no-users
```

特定月だけ取得する場合:

```bash
python ameblo_to_ghost.py --year 2026 --month 6 --remove-duplicate-noscript-images
```

`--full` と `--year` を同時に指定した場合は、`--year` の範囲を優先します。

## 再開

取得済みURLは `fetched_urls.json` に保存されます。同じURLは次回実行時にスキップされます。

キャッシュを無視して取り直したい場合:

```bash
python ameblo_to_ghost.py --refresh
```

完全にやり直したい場合は `fetched_urls.json` を退避または削除してください。

## 画像

本文内画像は `output/content/images/YYYY/MM/` に保存され、本文HTML内の `img src` は `/content/images/YYYY/MM/filename.ext` のようなGhost向けパスへ書き換えます。

画像は必ずダウンロードを試みます。元URLと保存先の対応は `output/images_manifest.csv` に出力されます。

本文内の通常 `<img>` は削除しません。Ameblo由来の `<noscript><img ...></noscript>` 重複だけを削除したい場合:

```bash
python ameblo_to_ghost.py --remove-duplicate-noscript-images
```

このオプションを指定した場合、本文内の通常 `<img>` は残し、`noscript` 要素を削除します。処理後に本文HTMLへ `noscript` が残っている場合はエラーにします。

`--remove-feature-image-from-body` は互換用の非推奨エイリアスです。現在は本文画像を削除せず、`--remove-duplicate-noscript-images` と同じ挙動になります。

## タイトル確認

タイトル候補と採用タイトルを確認したい場合:

```bash
python ameblo_to_ghost.py --debug-title
```

`.skinArticleTitle`、`og:title`、JSON-LD `headline`、`h1`、`soup.title` などの候補を比較し、短縮タイトルではなく最も完全そうなタイトルを採用します。

## Ghost Import JSON

生成するJSONは次の方針です。

- Ghost 5.x Import JSON を前提に `posts`, `tags`, `posts_tags`, `posts_authors` を出力
- デフォルトでは固定authorとして `Susie8FA7` / `susie8fa7` の `users` を1件作成
- `--no-users` 指定時は `data.users` を出力せず、既存GhostユーザーID `6a23714bfcc3c70001503a30` を `posts[].primary_author_id` と `posts_authors[].author_id` に設定
- `users[].id` / `posts[].primary_author_id` / `posts_authors[].author_id` は固定IDを使用し、Importごとに変わらない
- すべての記事を `posts_authors` で `Susie8FA7` に紐付け
- `posts[].primary_author_id` に `Susie8FA7` の author id を設定
- `posts[].html` にはAmeblo記事全体ではなく本文領域のみを保存
- 本文抽出は `#entryBody`, `.articleText`, `[data-uranus-component="entryBody"]`, `.skin-entryBody`, `.entryBody`, `.entry-body` を優先
- `article`, `.js-entryWrapper`, `.skinArticle` 全体やAmeblo側の記事タイトル・記事ヘッダーは本文HTMLに含めない
- `posts[].html` 内の画像パスは `/content/images/YYYY/MM/filename.ext`
- `mobiledoc` と `lexical` は出力しない
- Ghost画像カードやMobiledoc/Lexicalのimageノードは生成せず、本文HTML内の通常の `<img>` だけを利用
- AmebloのOGPカード系画像ブロックは本文HTMLから除去
- `posts[].status` は `published`
- `posts[].visibility` は `public`
- `posts[].published_at` は Ameblo の投稿日をISO形式で保存
- `posts[].feature_image` は本文中の通常画像を優先し、`/content/images/YYYY/MM/filename.ext` 形式で保存。画像がない場合は `null`
- `posts[].custom_excerpt` は空文字列。Amebloテーマ名はGhostタグとして扱う
- `posts[].canonical_url` に元記事URLを保存
- Amebloのテーマ名を Ghost の primary tag として必ず先頭に紐付け、`posts_tags.sort_order` は `0` にする
- Amebloハッシュタグは記事HTML内の候補に加えて `https://rapi.blogtag.ameba.jp/hashtag/api/v2/article/tag/{blog_id}/{article_id}` から取得し、テーマの後ろに追加tagsとして紐付ける
- Amebloテーマは `theme-\d+.html` または発見済みのテーマURLセットだけを正とし、月別/年別アーカイブはタグ化しない
- インポート日時を表すタグは生成しない
- `tags[].slug` はタグ名から決定論的に生成し、Importごとに安定
- `posts[].slug` も記事URLまたはタイトルから決定論的に生成し、ランダム値は使わない
- 日本語タグでASCII slugを作れない場合は `ameblo-theme-<hash>` 形式の安定slugを使用
- `--remove-duplicate-noscript-images` 指定時は、Ameblo画像ブロック内の重複表示原因になる `noscript` を削除し、通常の本文 `<img>` と `posts[].feature_image` は維持する
- 本文末尾に `<hr>` と元記事リンクを自動付加

## 注意

- robots.txt と Ameba利用規約に配慮し、短時間に大量アクセスしないでください。
- コメントは取得しません。
- 広告やサイドバーは後工程で消す前提のため、本文候補領域はできるだけ保持します。
- AmebloのHTML構造が変わると、セレクタ調整が必要になる場合があります。
