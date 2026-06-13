# Ameblo to Ghost Exporter

`requests` と `BeautifulSoup4` で Ameblo の公開記事を取得し、Ghost CMS の Import JSON を生成するツールです。

対象ブログは `--base-url` で指定できます。

```text
https://ameblo.jp/YOUR_AMEBLO_ID/
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

`--year` 指定時は `archive-YYYYMM.html` を使い、12月から1月へ新しい月順に処理します。投稿数が多い月は `archive-YYYYMM-2.html`, `archive-YYYYMM-3.html` のような2ページ目以降も巡回します。

Ameblo側で月別アーカイブの2ページ目が存在しない場合に備えて、通常の時系列一覧 `page-N.html` も補完巡回します。Amebloの月別アーカイブは20件で打ち切られ、古い同月記事が `page-N.html` 側にしか出ない場合があります。そのため、`archive-YYYYMM.html` で指定年月の記事URLがちょうど20件見つかった月だけ、取得済みの指定年月記事URLをアンカーにして `page-N.html` を軽量探索します。

アンカーが含まれる `page-N.html` が見つかったら、その周辺 `N-2` から `N+2` だけを補完候補にします。この探索段階では記事本文を解析せず、一覧HTML内のリンクだけを見ます。候補記事は本文解析後に `published_at` で年/月を確認し、指定範囲外の記事は出力しません。

`page-N.html` の探索上限は `--max-page-scan` で変更できます。デフォルトは500です。

```bash
python ameblo_to_ghost.py --year 2020 --diff-only --max-page-scan 500 --remove-duplicate-noscript-images
```

存在しない月や記事がない月はスキップします。`--limit` 未指定ならその年の全件を対象にし、`--limit` を指定した場合だけ上限件数を適用します。

年別取得のデフォルト出力先は `outputYYYY/` です。例として `--year 2026` の場合は `output2026/ghost-import.json` と `output2026/content/images/YYYY/MM/` に出力します。出力先を変えたい場合は `--output-dir` を指定します。

```bash
python ameblo_to_ghost.py --year 2026 --output-dir output2026Retry --remove-duplicate-noscript-images
```

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

## 差分出力

過去に取得済みの記事を除外し、新しく見つかった記事だけをGhost Import JSONに出力したい場合:

```bash
python ameblo_to_ghost.py --year 2020 --diff-only --remove-duplicate-noscript-images --no-users
```

`--diff-only` 指定時は `fetched_urls.json` に存在するURLを完全にスキップし、`[skip-existing]` とログ出力します。新規URLだけを取得・解析し、`fetched_urls.json` に追加します。`--year` と併用した場合のデフォルト出力先は `outputYYYYDiff/` です。新規記事が0件でも、空のGhost Import JSONを出力します。

`--diff-only` と `--refresh` は同時指定できません。差分出力先を明示したい場合は `--output-dir` を使います。

```bash
python ameblo_to_ghost.py --year 2020 --diff-only --output-dir output2020Diff --remove-duplicate-noscript-images --no-users
```

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

## Ghost Bookmark Card変換

AmebloのOGP/リンクカードや、本文中で単独段落として置かれているURLリンクを、GhostのBookmark cardとして出力できます。

```bash
python ameblo_to_ghost.py --year 2026 --ghost-bookmark-cards --remove-duplicate-noscript-images
```

`--ghost-bookmark-cards` は、Ghost Export JSONから確認した `type: "bookmark"` のLexicalノード形式で、到達確認できたURLリンク/OGPカードをBookmark cardとして出力します。Bookmark化した記事は `posts[].lexical` 側に段落ノードとBookmarkノードだけを出し、同じリンクを `posts[].html` に二重出力しません。通常本文をLexical内のHTMLブロックとして入れないため、Ghostエディタ上に不要な `<>` HTMLブロックが出ない構成です。

リンク先タイトルを取得できない場合やURLへ到達できない場合は、無理にBookmark card化せず、元の通常リンクとして保持します。

## Ghost Import JSON

生成するJSONは次の方針です。

- Ghost 5.x Import JSON を前提に `posts`, `tags`, `posts_tags`, `posts_authors` を出力
- デフォルトでは `--author-id`, `--author-name`, `--author-slug`, `--author-email` の値で `users` を1件作成
- `--no-users` 指定時は `data.users` を出力せず、`--author-id` の既存GhostユーザーIDを `posts[].primary_author_id` と `posts_authors[].author_id` に設定
- `users[].id` / `posts[].primary_author_id` / `posts_authors[].author_id` は固定IDを使用し、Importごとに変わらない
- すべての記事を `posts_authors` で指定authorに紐付け
- `posts[].primary_author_id` に指定author id を設定
- `posts[].html` にはAmeblo記事全体ではなく本文領域のみを保存
- 本文抽出は `#entryBody`, `.articleText`, `[data-uranus-component="entryBody"]`, `.skin-entryBody`, `.entryBody`, `.entry-body` を優先
- `article`, `.js-entryWrapper`, `.skinArticle` 全体やAmeblo側の記事タイトル・記事ヘッダーは本文HTMLに含めない
- `posts[].html` 内の画像パスは `/content/images/YYYY/MM/filename.ext`
- 通常は `mobiledoc` と `lexical` は出力しない
- `--ghost-bookmark-cards` 指定時のみ、Bookmark card化できた記事に `posts[].lexical` を出力する
- Ghost画像カードやMobiledoc/Lexicalのimageノードは生成せず、本文HTML内の通常の `<img>` だけを利用
- AmebloのOGP/リンクカードは削除せず、カード内URLを通常のテキストリンクへ変換
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
- `--ghost-bookmark-cards` 指定時は、到達確認できたURLリンク/OGPカードをGhost Lexical Bookmark cardへ変換する。Bookmark化した記事では `posts[].html` を空にし、同一リンクのHTML/lexical二重出力を避ける
- 本文末尾に `<hr>` と元記事リンクを自動付加

## 注意

- robots.txt と Ameba利用規約に配慮し、短時間に大量アクセスしないでください。
- コメントは取得しません。
- 広告やサイドバーは後工程で消す前提のため、本文候補領域はできるだけ保持します。
- AmebloのHTML構造が変わると、セレクタ調整が必要になる場合があります。
