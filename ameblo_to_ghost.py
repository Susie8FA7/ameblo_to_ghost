#!/usr/bin/env python3
"""Export public Ameblo posts to a Ghost import JSON file.

The scraper is intentionally conservative: it uses a visible User-Agent,
waits between requests, keeps a resume file, and starts in dry-run mode.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import html
import json
import random
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://ameblo.jp/YOUR_AMEBLO_ID/"
USER_AGENT = (
    "AmebloToGhostExporter/0.1 "
    "(personal archive; requests+BeautifulSoup4; contact: local-user)"
)
ARTICLE_RE = re.compile(r"/[^/]+/entry-\d+\.html$")
THEME_PATH_RE = re.compile(r"/[^/]+/theme-\d+\.html$")
LISTING_HINT_RE = re.compile(
    r"/[^/]+/(?:$|page-\d+\.html|entrylist(?:-\d+)?\.html|"
    r"theme-\d+\.html|archive[^/]*\.html)"
)
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg)(?:$|\?)", re.I)
GHOST_IMAGE_PREFIX = "/content/images"
AUTHOR_ID = "REPLACE_WITH_EXISTING_GHOST_AUTHOR_ID"
AUTHOR_NAME = "Ameblo Author"
AUTHOR_SLUG = "ameblo-author"
AUTHOR_EMAIL = "ameblo-author@example.invalid"
OGP_CARD_CLASSES = {"ogpCard_root", "ogpCard_wrap", "ogpCard_icon", "ogpCard_image"}
TAG_SLUG_OVERRIDES = {
    "ゲーム": "game",
    "大河ドラマ": "taiga-drama",
    "プログラミング": "programming",
    "ブログ": "blog",
}


@dataclass
class ExportConfig:
    base_url: str
    output_json: Path
    image_dir: Path
    logs_dir: Path
    fetched_file: Path
    dry_run: bool
    limit: int
    min_delay: float
    max_delay: float
    max_listing_pages: int
    max_page_scan: int
    timeout: int
    download_images: bool
    refresh: bool
    diff_only: bool
    remove_feature_image_from_body: bool
    remove_duplicate_noscript_images: bool
    ghost_bookmark_cards: bool
    debug_title: bool
    year: int | None
    month: int | None
    include_users: bool
    author_id: str
    author_name: str
    author_slug: str
    author_email: str


@dataclass
class ArticleData:
    title: str
    url: str
    published_at: str | None
    theme: str | None
    html: str
    links: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    feature_image: str | None = None
    slug: str = ""
    lexical: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ArticleData":
        article_html = sanitize_cached_html(normalize_ghost_image_paths(data["html"]))
        return cls(
            title=data["title"],
            url=data["url"],
            published_at=data.get("published_at"),
            theme=data.get("theme"),
            html=article_html,
            links=list(data.get("links", [])),
            images=list(data.get("images", [])),
            hashtags=list(data.get("hashtags", [])),
            feature_image=normalize_ghost_image_path(
                data.get("feature_image") or first_image_from_html(article_html)
            ),
            slug=data.get("slug", ""),
            lexical=None,
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "theme": self.theme,
            "html": self.html,
            "links": self.links,
            "images": self.images,
            "hashtags": self.hashtags,
            "feature_image": self.feature_image,
            "slug": self.slug,
        }


class AmebloExporter:
    def __init__(self, config: ExportConfig) -> None:
        self.config = config
        self.session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            }
        )
        self.errors_path = self.config.logs_dir / "errors.csv"
        self.images_manifest_path = self.config.output_json.parent / "images_manifest.csv"
        self.fetched_urls = self._load_fetched_urls()
        self.article_theme_hints: dict[str, str] = {}
        self.theme_url_set: set[str] = set()
        self.published_month_hints: dict[str, tuple[int, int] | None] = {}
        self.config.output_json.parent.mkdir(parents=True, exist_ok=True)
        self.config.image_dir.mkdir(parents=True, exist_ok=True)
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_error_log()
        self._ensure_images_manifest()

    def run(self) -> None:
        if self.config.ghost_bookmark_cards:
            print(
                "[bookmark] --ghost-bookmark-cards is enabled; "
                "eligible links will be emitted as Ghost Lexical bookmark nodes."
            )
        article_urls = self.discover_article_urls()
        if self.config.limit and not self.config.year:
            article_urls = article_urls[: self.config.limit]

        posts: list[ArticleData] = []
        skipped_out_of_scope = 0
        pending_fetched_updates: dict[str, dict] = {}
        for index, url in enumerate(article_urls, start=1):
            if self.config.diff_only and url in self.fetched_urls:
                print(f"[skip-existing] {url}")
                continue
            cached = None if self.config.refresh else self.fetched_urls.get(url)
            if cached and cached.get("html"):
                print(f"[cache] already fetched: {url}")
                article = ArticleData.from_dict(cached)
                if self.article_matches_scope(article):
                    posts.append(article)
                    if self.config.limit and len(posts) >= self.config.limit:
                        break
                continue
            print(f"[post {index}/{len(article_urls)}] {url}")
            try:
                article = self.parse_article(url)
                if not self.article_matches_scope(article):
                    print(f"[skip-out-of-scope] {url} published_at={article.published_at}")
                    skipped_out_of_scope += 1
                    self.sleep()
                    continue
                posts.append(article)
                fetched_record = article.to_dict()
                fetched_record["fetched_at"] = datetime.now(timezone.utc).isoformat()
                if self.config.diff_only:
                    pending_fetched_updates[url] = fetched_record
                else:
                    self.fetched_urls[url] = fetched_record
                    self._save_fetched_urls()
                if self.config.limit and len(posts) >= self.config.limit:
                    break
            except Exception as exc:  # noqa: BLE001 - log and continue the export.
                self.log_error(url, "parse_failed", str(exc))
            self.sleep()

        ghost_json = self.build_ghost_import(posts)
        self.config.output_json.write_text(
            json.dumps(ghost_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        if pending_fetched_updates:
            self.fetched_urls.update(pending_fetched_updates)
            self._save_fetched_urls()
        if self.config.diff_only and not posts:
            print("[done] no new articles found for diff-only export")
        if self.config.year:
            print(
                f"[year-scope] output_posts={len(posts)} "
                f"skipped_out_of_scope={skipped_out_of_scope}"
            )
        print(f"[done] wrote {self.config.output_json}")

    def article_matches_scope(self, article: ArticleData) -> bool:
        if not self.config.year:
            return True
        if not article.published_at:
            self.log_error(article.url, "year_filter_failed", "published_at is missing")
            return False
        try:
            published = datetime.fromisoformat(article.published_at.replace("Z", "+00:00"))
        except ValueError:
            self.log_error(article.url, "year_filter_failed", f"invalid published_at: {article.published_at}")
            return False
        if published.year != self.config.year:
            return False
        if self.config.month and published.month != self.config.month:
            return False
        return True

    def discover_article_urls(self) -> list[str]:
        if self.config.year:
            return self.discover_article_urls_by_year()

        queue = [self.config.base_url, urljoin(self.config.base_url, "entrylist.html")]
        seen_listing: set[str] = set()
        article_urls: list[str] = []
        article_seen: set[str] = set()

        while queue and len(seen_listing) < self.config.max_listing_pages:
            listing_url = normalize_url(queue.pop(0))
            if listing_url in seen_listing:
                continue
            if (
                self.config.dry_run
                and len(article_urls) >= self.config.limit
                and not self.is_known_theme_url(listing_url)
                and listing_url != urljoin(self.config.base_url, "entrylist.html")
            ):
                continue
            seen_listing.add(listing_url)
            print(f"[list] {listing_url}")
            try:
                soup = self.fetch_soup(listing_url)
            except Exception as exc:  # noqa: BLE001
                self.log_error(listing_url, "listing_fetch_failed", str(exc))
                continue

            listing_theme = self.extract_listing_theme(soup, listing_url)
            for href in self.extract_hrefs(soup, listing_url):
                normalized = normalize_url(href)
                if not is_same_blog_url(normalized, self.config.base_url):
                    continue
                path = urlparse(normalized).path
                if ARTICLE_RE.search(path):
                    normalized = canonical_article_url(normalized)
                    if listing_theme:
                        self.article_theme_hints.setdefault(normalized, listing_theme)
                    if normalized not in article_seen:
                        article_seen.add(normalized)
                        article_urls.append(normalized)
                elif LISTING_HINT_RE.search(path) and normalized not in seen_listing:
                    if is_numeric_theme_url(normalized):
                        self.theme_url_set.add(normalized)
                    if normalized not in queue:
                        queue.append(normalized)
            if self.config.dry_run and len(article_urls) >= self.config.limit:
                entrylist_url = urljoin(self.config.base_url, "entrylist.html")
                if entrylist_url not in seen_listing:
                    self.sleep()
                    continue
                target_urls = article_urls[: self.config.limit]
                missing_themes = [url for url in target_urls if url not in self.article_theme_hints]
                has_theme_pages = any(self.is_known_theme_url(item) for item in queue)
                if not missing_themes or not has_theme_pages:
                    return target_urls
            self.sleep()

        return article_urls

    def discover_article_urls_by_year(self) -> list[str]:
        months = [self.config.month] if self.config.month else list(range(12, 0, -1))
        seen_listing: set[str] = set()
        article_urls: list[str] = []
        article_seen: set[str] = set()
        scanned_pages: dict[int, list[str]] = {}
        scanned_page_months: dict[int, set[tuple[int, int]]] = {}

        for month in months:
            page = 1
            queued_pages: list[str] = [self.archive_url_for_month_page(month, page)]
            month_urls: list[str] = []
            while queued_pages and len(seen_listing) < self.config.max_listing_pages:
                listing_url = normalize_url(queued_pages.pop(0))
                if listing_url in seen_listing:
                    continue
                seen_listing.add(listing_url)
                print(f"[list] {listing_url}")
                try:
                    soup = self.fetch_soup(listing_url)
                except requests.HTTPError as exc:
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 404:
                        break
                    self.log_error(listing_url, "listing_fetch_failed", str(exc))
                    break
                except Exception as exc:  # noqa: BLE001
                    self.log_error(listing_url, "listing_fetch_failed", str(exc))
                    break

                added_on_page = 0
                for href in self.extract_hrefs(soup, listing_url):
                    normalized = normalize_url(href)
                    if not is_same_blog_url(normalized, self.config.base_url):
                        continue
                    path = urlparse(normalized).path
                    if ARTICLE_RE.search(path):
                        normalized = canonical_article_url(normalized)
                        if normalized not in article_seen:
                            article_seen.add(normalized)
                            article_urls.append(normalized)
                            month_urls.append(normalized)
                            added_on_page += 1
                            if self.config.limit and not self.config.diff_only and len(article_urls) >= self.config.limit:
                                self.discover_theme_urls_from_index()
                                self.populate_theme_hints_for_targets(article_urls)
                                return article_urls
                    elif is_numeric_theme_url(normalized):
                        self.theme_url_set.add(normalized)
                    elif self.is_same_month_archive_url(normalized, month) and normalized not in seen_listing:
                        if normalized not in queued_pages:
                            queued_pages.append(normalized)
                if added_on_page == 0:
                    break
                page += 1
                next_page = self.archive_url_for_month_page(month, page)
                if next_page not in seen_listing and next_page not in queued_pages:
                    queued_pages.append(next_page)
                self.sleep()

            if len(month_urls) == 20:
                print(
                    f"[archive-truncated] {self.config.year}-{month:02d} "
                    "archive returned exactly 20 article URLs; page-N anchor scan enabled"
                )
                self.discover_chronological_supplement_for_month(
                    month, month_urls, article_urls, article_seen, scanned_pages, scanned_page_months
                )
            elif month_urls:
                print(
                    f"[archive-complete] {self.config.year}-{month:02d} "
                    f"archive returned {len(month_urls)} article URLs; no page-N supplement"
                )

        self.discover_theme_urls_from_index()
        self.populate_theme_hints_for_targets(article_urls)
        return article_urls

    def archive_url_for_month_page(self, month: int, page: int) -> str:
        suffix = "" if page == 1 else f"-{page}"
        return urljoin(self.config.base_url, f"archive-{self.config.year}{month:02d}{suffix}.html")

    def is_same_month_archive_url(self, url: str, month: int) -> bool:
        path = urlparse(url).path
        blog_id = re.escape(blog_id_from_base_url(self.config.base_url) or "")
        pattern = rf"/{blog_id}/archive-{self.config.year}{month:02d}(?:-\d+)?\.html$"
        return bool(re.search(pattern, path))

    def discover_chronological_supplement_for_month(
        self,
        month: int,
        anchor_urls: list[str],
        article_urls: list[str],
        article_seen: set[str],
        scanned_pages: dict[int, list[str]],
        scanned_page_months: dict[int, set[tuple[int, int]]],
    ) -> None:
        anchor_set = set(anchor_urls)
        found = self.find_anchor_page(anchor_set, scanned_pages, scanned_page_months)
        if not found:
            print(f"[anchor-page] {self.config.year}-{month:02d} not found within {self.config.max_page_scan}")
            return
        anchor_page, anchor_page_urls = found
        print(f"[anchor-page] {self.config.year}-{month:02d} page={anchor_page}")
        start_page = max(1, anchor_page - 2)
        print(f"[supplement-range] {self.config.year}-{month:02d} page-{start_page}..month-boundary")

        before = len(article_urls)
        pages_scanned = 0
        target_month = (self.config.year, month)
        for page in range(start_page, anchor_page + 1):
            page_urls = anchor_page_urls if page == anchor_page else self.fetch_chronological_page_urls(
                page, scanned_pages, scanned_page_months
            )
            page_months_by_url = self.fetch_published_months_for_urls(page_urls)
            pages_scanned += 1
            self.add_supplement_page_urls(
                [url for url in page_urls if page_months_by_url.get(url) == target_month],
                anchor_set,
                article_urls,
                article_seen,
            )

        page = anchor_page + 1
        while page <= self.config.max_page_scan:
            page_urls = self.fetch_chronological_page_urls(page, scanned_pages, scanned_page_months)
            if not page_urls:
                print(f"[supplement-stop] {self.config.year}-{month:02d} page={page} empty")
                break
            page_months_by_url = self.fetch_published_months_for_urls(page_urls)
            page_months = {item for item in page_months_by_url.values() if item is not None}
            has_target_month = target_month in page_months
            month_summary = ",".join(f"{year}-{item_month:02d}" for year, item_month in sorted(page_months))
            if has_target_month:
                print(
                    f"[supplement-scan] {self.config.year}-{month:02d} "
                    f"page={page} published_months={month_summary or 'unknown'} target_month=yes"
                )
                self.add_supplement_page_urls(
                    [url for url in page_urls if page_months_by_url.get(url) == target_month],
                    anchor_set,
                    article_urls,
                    article_seen,
                )
                pages_scanned += 1
                page += 1
                continue
            if not page_months:
                print(
                    f"[supplement-scan] {self.config.year}-{month:02d} "
                    f"page={page} published_months=unknown target_month=unknown"
                )
                pages_scanned += 1
                print(f"[supplement-stop] {self.config.year}-{month:02d} page={page} published-month-unknown")
                break
            print(
                f"[supplement-stop] {self.config.year}-{month:02d} "
                f"page={page} published_months={month_summary} target_month=no"
            )
            break
        added = len(article_urls) - before
        print(f"[supplement-candidates] {self.config.year}-{month:02d} pages={pages_scanned} added={added}")

    @staticmethod
    def add_supplement_page_urls(
        page_urls: list[str],
        anchor_set: set[str],
        article_urls: list[str],
        article_seen: set[str],
    ) -> None:
        for url in page_urls:
            if url in anchor_set:
                continue
            if url not in article_seen:
                article_seen.add(url)
                article_urls.append(url)

    def fetch_published_months_for_urls(self, urls: list[str]) -> dict[str, tuple[int, int] | None]:
        return {url: self.fetch_article_published_month(url) for url in urls}

    def fetch_article_published_month(self, url: str) -> tuple[int, int] | None:
        if url in self.published_month_hints:
            return self.published_month_hints[url]
        cached = self.fetched_urls.get(url)
        if isinstance(cached, dict) and cached.get("published_at"):
            month = published_month_from_iso(str(cached.get("published_at")))
            self.published_month_hints[url] = month
            return month
        try:
            soup = self.fetch_soup(url)
            published_at = self.extract_published_at(soup)
        except Exception as exc:  # noqa: BLE001
            self.log_error(url, "published_month_fetch_failed", str(exc))
            self.published_month_hints[url] = None
            return None
        month = published_month_from_iso(published_at or "")
        self.published_month_hints[url] = month
        return month

    def find_anchor_page(
        self,
        anchor_urls: set[str],
        scanned_pages: dict[int, list[str]],
        scanned_page_months: dict[int, set[tuple[int, int]]],
    ) -> tuple[int, list[str]] | None:
        last_anchor_page: int | None = None
        last_anchor_page_urls: list[str] = []
        stop_after_pages_without_anchor = 5
        for page in range(1, self.config.max_page_scan + 1):
            page_urls = self.fetch_chronological_page_urls(page, scanned_pages, scanned_page_months)
            if not page_urls:
                continue
            has_anchor = any(url in anchor_urls for url in page_urls)
            if has_anchor:
                last_anchor_page = page
                last_anchor_page_urls = page_urls
            elif last_anchor_page and page - last_anchor_page >= stop_after_pages_without_anchor:
                break
        if last_anchor_page is None:
            return None
        return last_anchor_page, last_anchor_page_urls

    def fetch_chronological_page_urls(
        self,
        page: int,
        scanned_pages: dict[int, list[str]],
        scanned_page_months: dict[int, set[tuple[int, int]]] | None = None,
    ) -> list[str]:
        if page in scanned_pages:
            return scanned_pages[page]
        listing_url = urljoin(self.config.base_url, "" if page == 1 else f"page-{page}.html")
        listing_url = normalize_url(listing_url)
        if page == 1 or page % 25 == 0:
            print(f"[scan-page] {listing_url}")
        try:
            soup = self.fetch_soup(listing_url)
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code != 404:
                self.log_error(listing_url, "listing_fetch_failed", str(exc))
            scanned_pages[page] = []
            return []
        except Exception as exc:  # noqa: BLE001
            self.log_error(listing_url, "listing_fetch_failed", str(exc))
            scanned_pages[page] = []
            return []

        page_urls: list[str] = []
        for href in self.extract_hrefs(soup, listing_url):
            normalized = normalize_url(href)
            if not is_same_blog_url(normalized, self.config.base_url):
                continue
            path = urlparse(normalized).path
            if ARTICLE_RE.search(path):
                page_urls.append(canonical_article_url(normalized))
        unique_page_urls = list(dict.fromkeys(page_urls))
        scanned_pages[page] = unique_page_urls
        if scanned_page_months is not None:
            scanned_page_months[page] = extract_listing_month_markers(soup)
        return unique_page_urls

    def discover_theme_urls_from_index(self) -> None:
        for listing_url in [self.config.base_url, urljoin(self.config.base_url, "entrylist.html")]:
            try:
                soup = self.fetch_soup(listing_url)
            except Exception as exc:  # noqa: BLE001
                self.log_error(listing_url, "theme_index_fetch_failed", str(exc))
                continue
            for href in self.extract_hrefs(soup, listing_url):
                normalized = normalize_url(href)
                if is_same_blog_url(normalized, self.config.base_url) and is_numeric_theme_url(normalized):
                    self.theme_url_set.add(normalized)
            self.sleep()

    def is_same_year_archive_url(self, url: str) -> bool:
        path = urlparse(url).path
        blog_id = re.escape(blog_id_from_base_url(self.config.base_url) or "")
        if self.config.month:
            pattern = rf"/{blog_id}/archive-{self.config.year}{self.config.month:02d}(?:-\d+)?\.html$"
        else:
            pattern = rf"/{blog_id}/archive-{self.config.year}\d{{2}}(?:-\d+)?\.html$"
        return bool(re.search(pattern, path))

    def populate_theme_hints_for_targets(self, article_urls: list[str]) -> None:
        missing = {url for url in article_urls if url not in self.article_theme_hints}
        if not missing:
            return
        theme_queue = list(self.theme_url_set)
        seen_theme: set[str] = set()
        while theme_queue and missing and len(seen_theme) < self.config.max_listing_pages:
            theme_url = normalize_url(theme_queue.pop(0))
            if theme_url in seen_theme or not self.is_known_theme_url(theme_url):
                continue
            seen_theme.add(theme_url)
            print(f"[list] {theme_url}")
            try:
                soup = self.fetch_soup(theme_url)
            except Exception as exc:  # noqa: BLE001
                self.log_error(theme_url, "theme_fetch_failed", str(exc))
                continue
            listing_theme = self.extract_listing_theme(soup, theme_url)
            for href in self.extract_hrefs(soup, theme_url):
                normalized = normalize_url(href)
                if not is_same_blog_url(normalized, self.config.base_url):
                    continue
                path = urlparse(normalized).path
                if ARTICLE_RE.search(path):
                    article_url = canonical_article_url(normalized)
                    if listing_theme and article_url in missing:
                        self.article_theme_hints.setdefault(article_url, listing_theme)
                        missing.discard(article_url)
                elif is_numeric_theme_url(normalized):
                    self.theme_url_set.add(normalized)
                    if normalized not in seen_theme and normalized not in theme_queue:
                        theme_queue.append(normalized)
                elif self.is_theme_pagination_url(normalized, theme_url):
                    self.theme_url_set.add(normalized)
                    if normalized not in seen_theme and normalized not in theme_queue:
                        theme_queue.append(normalized)
            self.sleep()

    @staticmethod
    def is_theme_pagination_url(url: str, theme_url: str) -> bool:
        theme_path = urlparse(theme_url).path
        match = re.search(r"/([^/]+)/(theme-\d+)\.html$", theme_path)
        if not match:
            return False
        blog_id = re.escape(match.group(1))
        theme_id = re.escape(match.group(2))
        return bool(re.search(rf"/{blog_id}/{theme_id}(?:-\d+)?\.html$", urlparse(url).path))

    def parse_article(self, url: str) -> ArticleData:
        soup = self.fetch_soup(url)
        title = self.extract_title(soup, url)
        published_at = self.extract_published_at(soup)
        theme = self.extract_theme(soup)
        if not theme:
            theme = self.article_theme_hints.get(url)
        body = self.extract_body(soup)
        warnings = remove_image_card_blocks(body, convert_ogp_cards=not self.config.ghost_bookmark_cards)
        for warning in warnings:
            self.log_error(url, "warning", warning)
        if self.config.ghost_bookmark_cards:
            for warning in convert_reachable_links_to_bookmark_placeholders(body, self):
                self.log_error(url, "warning", warning)
        api_hashtags = self.fetch_hashtags_from_api(url)
        hashtags = merge_unique(api_hashtags, self.extract_hashtags(soup))
        if theme:
            hashtags = [hashtag for hashtag in hashtags if hashtag != theme]

        links = sorted(set(self.extract_hrefs(body, url)))
        image_urls = sorted(set(self.extract_image_urls(body, url)))
        if self.config.download_images:
            self.rewrite_and_download_images(body, image_urls, published_at, url)
        if self.config.remove_duplicate_noscript_images:
            remove_duplicate_noscript_images(body)

        feature_image_node = find_feature_image_tag(body)
        feature_image = image_src(feature_image_node)
        append_source_link(body, url)
        lexical = None
        if self.config.ghost_bookmark_cards and body.find(attrs={"data-ghost-bookmark-url": True}):
            if body.find("img"):
                for placeholder in body.find_all(attrs={"data-ghost-bookmark-url": True}):
                    placeholder.replace_with(
                        make_link_paragraph(
                            str(placeholder.get("data-ghost-bookmark-url") or ""),
                            str(placeholder.get("data-ghost-bookmark-title") or ""),
                        )
                    )
                body_html = str(body)
            else:
                lexical = build_lexical_from_body(body)
                body_html = ""
        else:
            body_html = str(body)
        if self.config.remove_duplicate_noscript_images:
            if body.find("noscript"):
                message = "noscript remains in body html"
                self.log_error(url, "noscript_remains_in_body", message)
                raise ValueError(message)
        slug = make_slug(url=url, title=title, published_at=published_at)
        return ArticleData(
            title=title,
            url=url,
            published_at=published_at,
            theme=theme,
            html=body_html,
            links=links,
            images=image_urls,
            hashtags=hashtags,
            feature_image=feature_image,
            slug=slug,
            lexical=lexical,
        )

    def fetch_soup(self, url: str) -> BeautifulSoup:
        response = self.session.get(url, timeout=self.config.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return BeautifulSoup(response.text, "lxml")

    def bookmark_url_status(self, url: str) -> tuple[bool, str | None, str | None]:
        if not hasattr(self, "_reachable_url_cache"):
            self._reachable_url_cache = {}
        cache: dict[str, tuple[bool, str | None, str | None]] = self._reachable_url_cache
        if url in cache:
            return cache[url]
        reachable = False
        final_url = None
        reason = None
        try:
            response = self.session.head(url, timeout=self.config.timeout, allow_redirects=True)
            if response.status_code in {403, 405} or response.status_code >= 400:
                response = self.session.get(url, timeout=self.config.timeout, allow_redirects=True, stream=True)
            final_url = response.url
            reachable = 200 <= response.status_code < 400
            if reachable and not is_same_redirect_domain(url, final_url):
                reachable = False
                reason = "redirected_to_different_domain"
            elif not reachable:
                reason = f"http_status_{response.status_code}"
            response.close()
        except requests.RequestException as exc:
            reachable = False
            reason = type(exc).__name__
        cache[url] = (reachable, final_url, reason)
        return cache[url]

    def is_reachable_url(self, url: str) -> bool:
        return self.bookmark_url_status(url)[0]

    def fetch_bookmark_metadata(self, url: str, fallback_title: str = "") -> dict:
        if not hasattr(self, "_bookmark_metadata_cache"):
            self._bookmark_metadata_cache = {}
        cache: dict[str, dict] = self._bookmark_metadata_cache
        if url in cache:
            return cache[url]
        metadata = default_bookmark_metadata(url, fallback_title)
        try:
            response = self.session.get(url, timeout=self.config.timeout, allow_redirects=True)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
        except Exception:  # noqa: BLE001
            cache[url] = metadata
            return metadata
        if not is_same_redirect_domain(url, response.url):
            response.close()
            cache[url] = metadata
            return metadata
        soup = BeautifulSoup(response.text, "lxml")
        title = first_meta_content(soup, ["og:title", "twitter:title"]) or clean_text(
            soup.title.get_text(" ", strip=True) if soup.title else ""
        )
        description = first_meta_content(
            soup,
            ["og:description", "twitter:description", "description"],
        )
        publisher = first_meta_content(soup, ["og:site_name", "application-name"])
        author = first_meta_content(soup, ["author", "article:author"])
        thumbnail = first_meta_content(soup, ["og:image", "twitter:image", "twitter:image:src"])
        icon = find_page_icon(soup, response.url)
        metadata.update(
            {
                "icon": icon,
                "title": title or fallback_title or url,
                "description": description,
                "publisher": publisher,
                "author": author,
                "thumbnail": urljoin(response.url, thumbnail) if thumbnail else None,
            }
        )
        cache[url] = metadata
        return metadata

    def sleep(self) -> None:
        delay = random.uniform(self.config.min_delay, self.config.max_delay)
        time.sleep(delay)

    @staticmethod
    def extract_hrefs(soup: BeautifulSoup | Tag, base_url: str) -> list[str]:
        urls: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "").strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                urls.append(urljoin(base_url, href))
        return urls

    @staticmethod
    def extract_image_urls(soup: BeautifulSoup | Tag, base_url: str) -> list[str]:
        urls: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if src:
                urls.append(urljoin(base_url, src.strip()))
        return urls

    def extract_title(self, soup: BeautifulSoup, url: str = "") -> str:
        candidates = collect_title_candidates(soup)
        if candidates:
            selected = select_best_title(candidates)
            if self.config.debug_title:
                print(f"[debug-title] {url}")
                for source, value in candidates:
                    marker = "*" if value == selected else " "
                    print(f"[debug-title] {marker} {source}: {value}")
            return selected
        raise ValueError("title not found")

    @staticmethod
    def extract_published_at(soup: BeautifulSoup) -> str | None:
        json_ld = find_blogposting_json_ld(soup)
        if json_ld:
            for key in ("datePublished", "dateModified"):
                parsed = parse_datetime(str(json_ld.get(key, "")))
                if parsed:
                    return parsed

        meta_names = [
            ("property", "article:published_time"),
            ("property", "og:updated_time"),
            ("name", "pubdate"),
        ]
        for attr, value in meta_names:
            meta = soup.find("meta", attrs={attr: value})
            if meta and meta.get("content"):
                parsed = parse_datetime(meta["content"])
                if parsed:
                    return parsed

        for time_tag in soup.find_all("time"):
            value = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
            parsed = parse_datetime(value)
            if parsed:
                return parsed

        text = soup.get_text("\n", strip=True)
        match = re.search(
            r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日.*?(\d{1,2})時\s*(\d{1,2})分",
            text,
        )
        if match:
            year, month, day, hour, minute = map(int, match.groups())
            return datetime(year, month, day, hour, minute).isoformat()
        return None

    @staticmethod
    def extract_theme(soup: BeautifulSoup) -> str | None:
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if is_numeric_theme_url(urljoin(BASE_URL, href)):
                value = clean_theme_name(anchor.get_text(" ", strip=True))
                if value:
                    return value
        text = soup.get_text("\n", strip=True)
        match = re.search(r"テーマ[:：]\s*([^\n]+)", text)
        return clean_theme_name(match.group(1)) if match else None

    def extract_listing_theme(self, soup: BeautifulSoup, listing_url: str = "") -> str | None:
        if not self.is_known_theme_url(listing_url):
            return None
        for heading in soup.find_all(["h1", "h2"]):
            value = clean_text(heading.get_text(" ", strip=True))
            match = re.match(r"(.+?)\s*の記事\(", value)
            if match:
                return clean_theme_name(match.group(1))
        return None

    def is_known_theme_url(self, url: str) -> bool:
        normalized = normalize_url(url)
        return normalized in self.theme_url_set or is_numeric_theme_url(normalized)

    @staticmethod
    def extract_hashtags(soup: BeautifulSoup) -> list[str]:
        values: list[str] = []

        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            text = anchor.get_text(" ", strip=True)
            if "hashtag" in href or "/tags/" in href:
                add_hashtag(values, text)
            elif text.startswith("#"):
                add_hashtag(values, text)

        source = str(soup)
        for match in re.finditer(r'"hash_tag_list"\s*:\s*(\[[^\]]*\])', source):
            try:
                items = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            for item in items:
                if isinstance(item, str):
                    add_hashtag(values, item)
                elif isinstance(item, dict):
                    for key in ("name", "tag_name", "hashtag_name", "hash_tag_name", "text"):
                        if item.get(key):
                            add_hashtag(values, str(item[key]))
                            break

        return list(dict.fromkeys(values))

    def fetch_hashtags_from_api(self, article_url: str) -> list[str]:
        blog_id = blog_id_from_base_url(self.config.base_url)
        article_id = article_id_from_url(article_url)
        if not blog_id or not article_id:
            return []
        api_url = f"https://rapi.blogtag.ameba.jp/hashtag/api/v2/article/tag/{blog_id}/{article_id}"
        try:
            response = self.session.get(
                api_url,
                timeout=self.config.timeout,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            items = response.json()
        except Exception as exc:  # noqa: BLE001
            self.log_error(article_url, "hashtag_fetch_failed", str(exc))
            return []

        values: list[str] = []
        if not isinstance(items, list):
            self.log_error(article_url, "hashtag_fetch_failed", "unexpected response shape")
            return values
        for item in items:
            if isinstance(item, dict) and item.get("hashtag"):
                add_hashtag(values, str(item["hashtag"]))
        return list(dict.fromkeys(values))

    @staticmethod
    def extract_body(soup: BeautifulSoup) -> Tag:
        selectors = [
            "#entryBody",
            ".articleText",
            "[data-uranus-component='entryBody']",
            ".skin-entryBody",
            ".entryBody",
            ".entry-body",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node and len(node.get_text(strip=True)) > 20:
                remove_article_chrome(node)
                return node

        wrapper = soup.select_one(".js-entryWrapper, .skinArticle")
        if wrapper:
            clone = copy.deepcopy(wrapper)
            remove_article_chrome(clone)
            if len(clone.get_text(strip=True)) > 20:
                return clone
        raise ValueError("body not found")

    def rewrite_and_download_images(
        self, body: Tag, image_urls: Iterable[str], published_at: str | None, article_url: str
    ) -> None:
        url_map: dict[str, str] = {}
        for image_url in image_urls:
            try:
                ghost_path, local_path = self.download_image(image_url, published_at)
                url_map[image_url] = ghost_path
                self.log_image_manifest(image_url, local_path, article_url, "ok", "")
            except Exception as exc:  # noqa: BLE001
                self.log_error(image_url, "image_download_failed", str(exc))
                self.log_image_manifest(image_url, "", article_url, "failed", str(exc))

        for img in body.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not src:
                continue
            absolute = urljoin(self.config.base_url, src)
            if absolute in url_map:
                img["src"] = url_map[absolute]
                for attr in ("data-src", "data-original", "srcset", "data-srcset"):
                    if attr in img.attrs:
                        del img.attrs[attr]

    def download_image(self, image_url: str, published_at: str | None) -> tuple[str, str]:
        parsed_date = datetime.now()
        if published_at:
            try:
                parsed_date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            except ValueError:
                pass

        relative_dir = Path("content") / "images" / f"{parsed_date:%Y}" / f"{parsed_date:%m}"
        target_dir = self.config.image_dir / f"{parsed_date:%Y}" / f"{parsed_date:%m}"
        target_dir.mkdir(parents=True, exist_ok=True)

        parsed = urlparse(image_url)
        suffix = Path(parsed.path).suffix
        if not suffix or not IMAGE_EXT_RE.search(suffix):
            suffix = ".jpg"
        digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:12]
        filename = f"{digest}{suffix.lower()}"
        target = target_dir / filename
        ghost_path = f"{GHOST_IMAGE_PREFIX}/{parsed_date:%Y}/{parsed_date:%m}/{filename}"
        local_path = (relative_dir / filename).as_posix()
        if target.exists():
            return ghost_path, local_path

        response = self.session.get(image_url, timeout=self.config.timeout)
        response.raise_for_status()
        target.write_bytes(response.content)
        return ghost_path, local_path

    def build_ghost_import(self, articles: list[ArticleData]) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        author_id = self.config.author_id
        users = [
            {
                "id": author_id,
                "name": self.config.author_name,
                "slug": self.config.author_slug,
                "email": self.config.author_email,
                "profile_image": None,
                "cover_image": None,
                "bio": None,
                "website": None,
                "location": None,
                "facebook": None,
                "twitter": None,
                "accessibility": None,
                "status": "active",
                "meta_title": None,
                "meta_description": None,
                "created_at": now,
                "updated_at": now,
            }
        ]
        tags_by_name: dict[str, str] = {}
        posts: list[dict] = []
        tags: list[dict] = []
        posts_tags: list[dict] = []
        posts_authors: list[dict] = []

        def ensure_tag(name: str, description: str) -> str:
            key = tag_identity_key(name)
            if key not in tags_by_name:
                tag_id = uuid.uuid4().hex
                tags_by_name[key] = tag_id
                tags.append(
                    {
                        "id": tag_id,
                        "name": name,
                        "slug": stable_tag_slug(name),
                        "description": description,
                    }
                )
            return tags_by_name[key]

        for article in articles:
            post_id = uuid.uuid4().hex
            published_at = article.published_at or now
            posts.append(
                {
                    "id": post_id,
                    "title": article.title,
                    "slug": article.slug,
                    "html": article.html,
                    "feature_image": normalize_ghost_image_path(article.feature_image),
                    "status": "published",
                    "visibility": "public",
                    "primary_author_id": author_id,
                    "published_at": published_at,
                    "created_at": published_at,
                    "updated_at": now,
                    "custom_excerpt": "",
                    "meta_title": article.title,
                    "meta_description": "",
                    "canonical_url": article.url,
                }
            )
            if article.lexical:
                posts[-1]["lexical"] = article.lexical
            posts_authors.append({"post_id": post_id, "author_id": author_id, "sort_order": 0})
            tag_names: list[tuple[str, str]] = []
            if article.theme:
                tag_names.append((article.theme, "Imported from Ameblo theme"))
            for hashtag in article.hashtags:
                if hashtag != article.theme:
                    tag_names.append((hashtag, "Imported from Ameblo hashtag"))

            seen_post_tag_ids: set[str] = set()
            sort_order = 0
            for tag_name, description in tag_names:
                tag_id = ensure_tag(tag_name, description)
                if tag_id in seen_post_tag_ids:
                    continue
                seen_post_tag_ids.add(tag_id)
                posts_tags.append(
                    {"post_id": post_id, "tag_id": tag_id, "sort_order": sort_order}
                )
                sort_order += 1

        data = {
            "posts": posts,
            "tags": tags,
            "posts_tags": posts_tags,
            "posts_authors": posts_authors,
        }
        if self.config.include_users:
            data["users"] = users

        return {
            "db": [
                {
                    "meta": {
                        "exported_on": int(time.time() * 1000),
                        "version": "5.0.0",
                    },
                    "data": data,
                }
            ]
        }

    def _load_fetched_urls(self) -> dict:
        if self.config.fetched_file.exists():
            return json.loads(self.config.fetched_file.read_text(encoding="utf-8"))
        return {}

    def _save_fetched_urls(self) -> None:
        self.config.fetched_file.write_text(
            json.dumps(self.fetched_urls, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )

    def _ensure_error_log(self) -> None:
        if self.errors_path.exists():
            return
        with self.errors_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "url", "kind", "message"])

    def _ensure_images_manifest(self) -> None:
        if self.images_manifest_path.exists():
            return
        with self.images_manifest_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "article_url", "original_url", "local_path", "status", "message"])

    def log_error(self, url: str, kind: str, message: str) -> None:
        with self.errors_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([datetime.now(timezone.utc).isoformat(), url, kind, message])

    def log_image_manifest(
        self, original_url: str, local_path: str, article_url: str, status: str, message: str
    ) -> None:
        with self.images_manifest_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    article_url,
                    original_url,
                    local_path,
                    status,
                    message,
                ]
            )


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    path = parsed.path or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def canonical_article_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def blog_id_from_base_url(base_url: str) -> str | None:
    path = urlparse(base_url).path.strip("/")
    return path.split("/", 1)[0] if path else None


def article_id_from_url(url: str) -> str | None:
    match = re.search(r"/entry-(\d+)\.html$", urlparse(url).path)
    return match.group(1) if match else None


def merge_unique(*groups: Iterable[str]) -> list[str]:
    values: list[str] = []
    for group in groups:
        for value in group:
            cleaned = clean_text(str(value).lstrip("#"))
            if cleaned and cleaned not in values:
                values.append(cleaned)
    return values


def is_numeric_theme_url(url: str) -> bool:
    return bool(THEME_PATH_RE.search(urlparse(url).path))


def is_same_blog_url(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    return parsed.netloc == base.netloc and parsed.path.startswith(base.path.rstrip("/") + "/")


def clean_text(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def strip_title_wrappers(value: str) -> str:
    value = clean_text(value)
    if value.startswith("『") and value.endswith("』"):
        return value[1:-1].strip()
    return value


def title_text_from_tag(tag: Tag) -> str:
    clone = copy.deepcopy(tag)
    for node in clone.find_all(["svg", "path"]):
        node.decompose()
    for node in clone.find_all(attrs={"aria-label": "リブログ記事"}):
        node.decompose()
    for span in clone.find_all("span"):
        classes = " ".join(span.get("class", []))
        aria = span.get("aria-label", "")
        if "icon" in classes.lower() or aria == "リブログ記事":
            span.decompose()
    return clone.get_text(" ", strip=True)


def collect_title_candidates(soup: BeautifulSoup) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []

    selector_sources = [
        ("skinArticleTitle", ".skinArticleTitle"),
        ("skin-entryTitle", ".skin-entryTitle"),
        ("uranus-entryTitle", "[data-uranus-component='entryTitle']"),
        ("entry-title", ".entry-title"),
    ]
    for source, selector in selector_sources:
        for node in soup.select(selector):
            add_title_candidate(candidates, source, title_text_from_tag(node))

    meta = soup.find("meta", property="og:title")
    if meta:
        add_title_candidate(candidates, "og:title", str(meta.get("content", "")))

    json_ld = find_blogposting_json_ld(soup)
    if json_ld and json_ld.get("headline"):
        add_title_candidate(candidates, "jsonld:headline", str(json_ld["headline"]))

    for heading in soup.find_all("h1"):
        classes = set(heading.get("class", []))
        if "skinTitleArea" in classes:
            continue
        add_title_candidate(candidates, "h1", title_text_from_tag(heading))

    if soup.title:
        title_value = clean_text(soup.title.get_text(" ", strip=True)).split("|", 1)[0]
        add_title_candidate(candidates, "soup.title", title_value)

    return candidates


def add_title_candidate(candidates: list[tuple[str, str]], source: str, value: str) -> None:
    value = strip_title_wrappers(value)
    if not value:
        return
    if value in {candidate for _, candidate in candidates}:
        return
    candidates.append((source, value))


def select_best_title(candidates: list[tuple[str, str]]) -> str:
    values = [value for _, value in candidates]
    non_substrings = [
        value
        for value in values
        if not any(value != other and normalized_title_contains(other, value) for other in values)
    ]
    pool = non_substrings or values
    return max(pool, key=title_score)


def normalized_title_contains(container: str, part: str) -> bool:
    container_norm = normalize_title_for_compare(container)
    part_norm = normalize_title_for_compare(part)
    return bool(part_norm) and part_norm in container_norm


def normalize_title_for_compare(value: str) -> str:
    return re.sub(r"\s+", "", clean_text(value)).lower()


def cjk_internal_space_count(value: str) -> int:
    cjk = r"\u3040-\u30ff\u3400-\u9fff"
    return len(re.findall(rf"(?<=[{cjk}])\s+(?=[{cjk}])", value))


def title_score(value: str) -> tuple[int, int, int, int]:
    # Prefer titles with more meaningful punctuation/series markers, then length.
    marker_count = len(re.findall(r"[【】（）()「」『』]|その\d+", value))
    non_ascii_count = sum(1 for char in value if ord(char) > 127)
    return (marker_count, -cjk_internal_space_count(value), len(value), non_ascii_count)


def clean_theme_name(value: str) -> str | None:
    value = re.sub(r"\(\d+\)$", "", clean_text(value)).strip()
    blocked = {
        "テーマ",
        "テーマ別記事一覧",
        "ブログトップ",
        "記事一覧",
        "画像一覧",
    }
    return value if value and value not in blocked else None


def clean_hashtag_name(value: str) -> str | None:
    value = clean_text(value).lstrip("#").strip()
    value = re.sub(r"\(\d+\)$", "", value).strip()
    blocked = {"ハッシュタグ", "hashtag", "タグ"}
    return value if value and value.lower() not in blocked else None


def add_hashtag(values: list[str], value: str) -> None:
    cleaned = clean_hashtag_name(value)
    if cleaned and cleaned not in values:
        values.append(cleaned)


def append_source_link(body: Tag, article_url: str) -> None:
    fragment = BeautifulSoup(
        (
            "<hr>"
            "<p>元記事: "
            f'<a href="{html.escape(article_url, quote=True)}">'
            f"{html.escape(article_url)}"
            "</a></p>"
        ),
        "lxml",
    )
    nodes = fragment.body.contents if fragment.body else fragment.contents
    for node in list(nodes):
        body.append(node)


def remove_image_card_blocks(body: Tag, convert_ogp_cards: bool = True) -> list[str]:
    warnings = convert_ogp_cards_to_links(body) if convert_ogp_cards else []

    card_class_patterns = (
        "kg-card",
        "kg-image-card",
        "image-card",
        "ghost-image-card",
    )
    for node in body.find_all(class_=True):
        classes = " ".join(node.get("class", [])).lower()
        if any(pattern in classes for pattern in card_class_patterns):
            node.decompose()

    for attr in ("data-lexical-image", "data-mobiledoc-card", "data-kg-card"):
        for node in body.find_all(attrs={attr: True}):
            node.decompose()
    return warnings


def convert_reachable_links_to_bookmark_placeholders(body: Tag, exporter: "AmebloExporter") -> list[str]:
    warnings: list[str] = []
    warnings.extend(convert_ogp_cards_to_bookmark_placeholders(body, exporter))
    warnings.extend(convert_standalone_links_to_bookmark_placeholders(body, exporter))
    prune_duplicate_consecutive_links(body)
    return warnings


def convert_ogp_cards_to_bookmark_placeholders(body: Tag, exporter: "AmebloExporter") -> list[str]:
    warnings: list[str] = []
    candidates = list(
        body.find_all(class_=lambda value: class_list_contains(value, {"ogpCard_root", "ogpCard_wrap"}))
    )
    for card in candidates:
        if not card.parent:
            continue
        if card.find_parent(class_=lambda value: class_list_contains(value, {"ogpCard_root", "ogpCard_wrap"})):
            continue
        href = extract_ogp_card_href(card)
        if not href:
            warnings.append("could not bookmark OGP card: missing href")
            continue
        title = extract_ogp_card_title_or_none(card, href)
        if not title:
            warnings.append(f"could not bookmark OGP card: missing title: {href}")
            card.replace_with(make_link_paragraph(href, href))
            continue
        reachable, final_url, reason = exporter.bookmark_url_status(href)
        if not reachable:
            destination = f" -> {final_url}" if final_url and final_url != href else ""
            warnings.append(f"could not bookmark OGP card: {reason or 'unreachable'}: {href}{destination}")
            card.replace_with(make_link_paragraph(href, title))
            continue
        metadata = extract_ogp_card_metadata(card, href, title)
        card.replace_with(make_bookmark_placeholder(href, metadata))
    return warnings


def convert_standalone_links_to_bookmark_placeholders(body: Tag, exporter: "AmebloExporter") -> list[str]:
    warnings: list[str] = []
    for paragraph in list(body.find_all("p")):
        anchors = paragraph.find_all("a", href=True)
        if len(anchors) != 1:
            continue
        anchor = anchors[0]
        href = str(anchor.get("href") or "").strip()
        title = clean_text(anchor.get_text(" ", strip=True))
        paragraph_text = clean_text(paragraph.get_text(" ", strip=True))
        if not is_bookmarkable_href(href) or not title:
            continue
        if paragraph_text and paragraph_text != title:
            continue
        reachable, final_url, reason = exporter.bookmark_url_status(href)
        if not reachable:
            destination = f" -> {final_url}" if final_url and final_url != href else ""
            warnings.append(f"could not bookmark link: {reason or 'unreachable'}: {href}{destination}")
            continue
        paragraph.replace_with(make_bookmark_placeholder(href, exporter.fetch_bookmark_metadata(href, title)))
    return warnings


def is_bookmarkable_href(href: str) -> bool:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return False
    if IMAGE_EXT_RE.search(parsed.path):
        return False
    return True


def is_same_redirect_domain(original_url: str, final_url: str | None) -> bool:
    original_host = normalized_hostname(original_url)
    final_host = normalized_hostname(final_url or "")
    return bool(original_host and final_host and original_host == final_host)


def normalized_hostname(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def make_bookmark_placeholder(href: str, metadata: dict) -> Tag:
    metadata_value = html.escape(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        quote=True,
    )
    fragment = BeautifulSoup(
        (
            f'<div data-ghost-bookmark-url="{html.escape(href, quote=True)}" '
            f'data-ghost-bookmark-title="{html.escape(str(metadata.get("title") or href), quote=True)}" '
            f'data-ghost-bookmark-metadata="{metadata_value}"></div>'
        ),
        "lxml",
    )
    return fragment.find("div") or fragment


def build_lexical_from_body(body: Tag) -> str:
    children: list[dict] = []
    paragraph_buffer: list[dict] = []
    append_lexical_children_from_container(body, children, paragraph_buffer)
    flush_lexical_paragraph(children, paragraph_buffer)
    return json.dumps(
        {
            "root": {
                "children": children,
                "direction": "ltr",
                "format": "",
                "indent": 0,
                "type": "root",
                "version": 1,
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def append_lexical_children_from_container(
    container: Tag, children: list[dict], paragraph_buffer: list[dict]
) -> None:
    for child in list(container.contents):
        if isinstance(child, NavigableString):
            append_text_node(paragraph_buffer, str(child))
            continue
        if not isinstance(child, Tag):
            continue
        if child.has_attr("data-ghost-bookmark-url"):
            flush_lexical_paragraph(children, paragraph_buffer)
            children.append(make_lexical_bookmark_node_from_placeholder(child))
            continue
        if child.name == "br":
            flush_lexical_paragraph(children, paragraph_buffer)
            continue
        if child.name == "hr":
            flush_lexical_paragraph(children, paragraph_buffer)
            continue
        if child.name in {"p", "div", "section", "article"}:
            if child.find(attrs={"data-ghost-bookmark-url": True}) or child.find(["p", "div"]):
                flush_lexical_paragraph(children, paragraph_buffer)
                append_lexical_children_from_container(child, children, paragraph_buffer)
                flush_lexical_paragraph(children, paragraph_buffer)
            else:
                inline_nodes = inline_lexical_nodes(child)
                if inline_nodes:
                    flush_lexical_paragraph(children, paragraph_buffer)
                    children.append(make_lexical_paragraph(inline_nodes))
            continue
        inline_nodes = inline_lexical_nodes(child)
        if inline_nodes:
            paragraph_buffer.extend(inline_nodes)


def inline_lexical_nodes(node: Tag) -> list[dict]:
    nodes: list[dict] = []
    for child in node.contents:
        if isinstance(child, NavigableString):
            append_text_node(nodes, str(child))
        elif isinstance(child, Tag):
            if child.name == "br":
                append_text_node(nodes, "\n")
            elif child.name == "a" and child.get("href"):
                link_children: list[dict] = []
                for item in child.contents:
                    if isinstance(item, NavigableString):
                        append_text_node(link_children, str(item))
                    elif isinstance(item, Tag):
                        append_text_node(link_children, item.get_text(" ", strip=True))
                if not link_children:
                    append_text_node(link_children, str(child.get("href") or ""))
                nodes.append(make_lexical_link(str(child.get("href") or ""), link_children))
            elif child.name not in {"script", "style", "noscript"}:
                nodes.extend(inline_lexical_nodes(child))
    compact_text_nodes(nodes)
    return nodes


def append_text_node(nodes: list[dict], value: str) -> None:
    if not value:
        return
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not value.strip():
        if nodes and nodes[-1].get("type") == "text" and not str(nodes[-1].get("text", "")).endswith(" "):
            nodes[-1]["text"] = str(nodes[-1].get("text", "")) + " "
        return
    nodes.append(make_lexical_text(clean_inline_text(value)))


def clean_inline_text(value: str) -> str:
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def compact_text_nodes(nodes: list[dict]) -> None:
    compacted: list[dict] = []
    for node in nodes:
        if node.get("type") == "text" and compacted and compacted[-1].get("type") == "text":
            compacted[-1]["text"] = str(compacted[-1].get("text", "")) + str(node.get("text", ""))
        else:
            compacted.append(node)
    nodes[:] = compacted


def flush_lexical_paragraph(children: list[dict], paragraph_buffer: list[dict]) -> None:
    compact_text_nodes(paragraph_buffer)
    has_text = any(clean_text(str(node.get("text", ""))) for node in paragraph_buffer if node.get("type") == "text")
    has_link = any(node.get("type") == "link" for node in paragraph_buffer)
    if paragraph_buffer and (has_text or has_link):
        children.append(make_lexical_paragraph(list(paragraph_buffer)))
    paragraph_buffer.clear()


def make_lexical_paragraph(nodes: list[dict]) -> dict:
    return {
        "children": nodes,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "type": "paragraph",
        "version": 1,
    }


def make_lexical_text(value: str) -> dict:
    return {
        "detail": 0,
        "format": 0,
        "mode": "normal",
        "style": "",
        "text": value,
        "type": "text",
        "version": 1,
    }


def make_lexical_link(url: str, children: list[dict]) -> dict:
    return {
        "children": children,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "type": "link",
        "rel": None,
        "target": None,
        "title": None,
        "url": url,
        "version": 1,
    }


def make_lexical_bookmark_node_from_placeholder(node: Tag) -> dict:
    href = str(node.get("data-ghost-bookmark-url") or "")
    metadata_value = str(node.get("data-ghost-bookmark-metadata") or "")
    metadata = default_bookmark_metadata(href, str(node.get("data-ghost-bookmark-title") or ""))
    if metadata_value:
        try:
            loaded = json.loads(html.unescape(metadata_value))
            if isinstance(loaded, dict):
                metadata.update(loaded)
        except json.JSONDecodeError:
            pass
    return make_lexical_bookmark_node(href, metadata)


def make_lexical_bookmark_node(href: str, metadata: dict) -> dict:
    return {
        "type": "bookmark",
        "version": 1,
        "url": href,
        "metadata": {
            "icon": metadata.get("icon"),
            "title": metadata.get("title") or href,
            "description": metadata.get("description"),
            "author": metadata.get("author"),
            "publisher": metadata.get("publisher"),
            "thumbnail": metadata.get("thumbnail"),
        },
        "caption": "",
    }


def convert_ogp_cards_to_links(body: Tag) -> list[str]:
    warnings: list[str] = []
    candidates = list(
        body.find_all(class_=lambda value: class_list_contains(value, {"ogpCard_root", "ogpCard_wrap"}))
    )
    for card in candidates:
        if not card.parent:
            continue
        if card.find_parent(class_=lambda value: class_list_contains(value, {"ogpCard_root", "ogpCard_wrap"})):
            continue
        href = extract_ogp_card_href(card)
        if not href:
            warnings.append("could not convert OGP card: missing href")
            preserve_ogp_text_as_paragraph(card)
            continue
        title = extract_ogp_card_title(card, href)
        replacement = make_link_paragraph(href, title)
        if is_duplicate_previous_link(card, href):
            card.decompose()
        else:
            card.replace_with(replacement)
    prune_duplicate_consecutive_links(body)
    return warnings


def extract_ogp_card_href(card: Tag) -> str | None:
    for anchor in card.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            if not IMAGE_EXT_RE.search(urlparse(href).path):
                return href
    return None


def preserve_ogp_text_as_paragraph(card: Tag) -> None:
    text = clean_text(card.get_text(" ", strip=True))
    if not text:
        card.decompose()
        return
    paragraph = BeautifulSoup(f"<p>{html.escape(text)}</p>", "lxml").find("p")
    if paragraph:
        card.replace_with(paragraph)


def extract_ogp_card_title(card: Tag, href: str) -> str:
    return extract_ogp_card_title_or_none(card, href) or href


def extract_ogp_card_title_or_none(card: Tag, href: str) -> str | None:
    title_selectors = [
        ".ogpCard_title",
        ".ogpCardTitle",
        ".ogpCard_text",
        ".ogpCard_description",
        ".ogpCard_content",
    ]
    for selector in title_selectors:
        node = card.select_one(selector)
        if node:
            value = clean_text(node.get_text(" ", strip=True))
            if value and value != href:
                return value
    for anchor in card.find_all("a", href=True):
        if str(anchor.get("href") or "").strip() == href:
            value = clean_text(anchor.get_text(" ", strip=True))
            if value and value != href:
                return value
    return None


def extract_ogp_card_metadata(card: Tag, href: str, title: str) -> dict:
    metadata = default_bookmark_metadata(href, title)
    metadata.update(
        {
            "description": first_selector_text(
                card,
                [".ogpCard_description", ".ogpCardDescription", ".ogpCard_summary"],
            ),
            "publisher": first_selector_text(
                card,
                [
                    ".ogpCard_site",
                    ".ogpCardSite",
                    ".ogpCard_urlText",
                    ".ogpCardUrlText",
                    ".ogpCard_url",
                    ".ogpCardUrl",
                ],
            ),
            "thumbnail": first_card_image_url(card),
        }
    )
    return metadata


def default_bookmark_metadata(url: str, title: str = "") -> dict:
    return {
        "icon": None,
        "title": title or url,
        "description": None,
        "publisher": None,
        "author": None,
        "thumbnail": None,
    }


def first_selector_text(node: Tag, selectors: list[str]) -> str | None:
    for selector in selectors:
        candidate = node.select_one(selector)
        if not candidate:
            continue
        value = clean_text(candidate.get_text(" ", strip=True))
        if value:
            return value
    return None


def first_card_image_url(card: Tag) -> str | None:
    for selector in [".ogpCard_image", ".ogpCardImage", "[data-ogp-card-image]"]:
        for img in card.select(selector):
            for attr in ("src", "data-src"):
                value = str(img.get(attr) or "").strip()
                if value and not value.lower().endswith(".svg"):
                    return value
    for img in card.find_all("img"):
        classes = set(img.get("class", []))
        if classes.intersection({"ogpCard_icon", "ogpCardIcon"}):
            continue
        for attr in ("src", "data-src", "data-ogp-card-image"):
            value = str(img.get(attr) or "").strip()
            if value and not value.lower().endswith(".svg"):
                return value
    for node in card.find_all(attrs={"style": True}):
        match = re.search(r"url\((['\"]?)(.*?)\1\)", str(node.get("style") or ""))
        if match and match.group(2):
            return match.group(2)
    return None


def first_meta_content(soup: BeautifulSoup, names: list[str]) -> str | None:
    for name in names:
        selectors = [
            f'meta[property="{name}"]',
            f'meta[name="{name}"]',
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            value = clean_text(str(node.get("content") or ""))
            if value:
                return value
    return None


def find_page_icon(soup: BeautifulSoup, base_url: str) -> str | None:
    rel_candidates = ("icon", "shortcut icon", "apple-touch-icon")
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])).lower()
        if rel in rel_candidates or any(item in rel.split() for item in rel_candidates):
            return urljoin(base_url, str(link.get("href") or ""))
    return urljoin(base_url, "/favicon.ico")


def make_link_paragraph(href: str, text: str) -> Tag:
    fragment = BeautifulSoup(
        f'<p><a href="{html.escape(href, quote=True)}">{html.escape(text)}</a></p>',
        "lxml",
    )
    return fragment.find("p") or fragment


def is_duplicate_previous_link(card: Tag, href: str) -> bool:
    previous = card.find_previous_sibling()
    while isinstance(previous, Tag) and not previous.get_text(strip=True):
        previous = previous.find_previous_sibling()
    if not isinstance(previous, Tag):
        return False
    anchors = previous.find_all("a", href=True)
    return len(anchors) == 1 and str(anchors[0].get("href") or "").strip() == href


def prune_duplicate_consecutive_links(body: Tag) -> None:
    previous_href = None
    for paragraph in list(body.find_all("p")):
        text = paragraph.get_text(" ", strip=True)
        anchors = paragraph.find_all("a", href=True)
        if len(anchors) == 1 and text:
            href = str(anchors[0].get("href") or "").strip()
            if href and href == previous_href:
                paragraph.decompose()
                continue
            previous_href = href
        elif text:
            previous_href = None


def extract_listing_month_markers(soup: BeautifulSoup) -> set[tuple[int, int]]:
    text = soup.get_text("\n", strip=True)
    months: set[tuple[int, int]] = set()
    patterns = [
        r"(20\d{2})[/-](\d{1,2})[/-]\d{1,2}",
        r"(20\d{2})年\s*(\d{1,2})月",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            year = int(match.group(1))
            month = int(match.group(2))
            if 1 <= month <= 12:
                months.add((year, month))
    return months


def remove_article_chrome(body: Tag) -> None:
    chrome_selectors = [
        ".skinArticleHeader",
        ".skinArticleTitle",
        ".skinArticleFooter",
        ".articleHeader",
        ".articleFooter",
        ".entryHeader",
        ".entryFooter",
        ".js-entryHeader",
        ".js-entryFooter",
        ".skinArticleRelation",
        ".skinArticleRanking",
        ".skinArticleAd",
        ".ad",
        ".ads",
        ".ranking",
        ".related",
    ]
    for selector in chrome_selectors:
        for node in body.select(selector):
            node.decompose()

    for heading in body.find_all("h1"):
        heading.decompose()


def remove_duplicate_noscript_images(body: Tag) -> None:
    for noscript in list(body.find_all("noscript")):
        noscript.decompose()
    prune_empty_containers(body)


def remove_first_matching_image(body: Tag, feature_image: str) -> None:
    remove_matching_images(body, feature_image, first_only=True)


def remove_matching_images(body: Tag, feature_image: str, first_only: bool = False) -> None:
    normalized_feature = normalize_image_url(feature_image)
    removed_any = remove_matching_url_nodes(body, normalized_feature, first_only)
    if removed_any:
        prune_empty_containers(body)


def remove_matching_url_nodes(body: Tag, normalized_feature: str, first_only: bool) -> bool:
    removed_any = False
    while True:
        target = find_matching_url_node(body, normalized_feature)
        if not target:
            return removed_any
        remove_url_node_with_context(target)
        removed_any = True
        if first_only:
            return removed_any


def find_matching_url_node(body: Tag, normalized_feature: str) -> Tag | None:
    for image in list(body.find_all("img")):
        for attr in ("src", "data-src"):
            if normalize_image_url(str(image.get(attr) or "")) == normalized_feature:
                return image

    for noscript in list(body.find_all("noscript")):
        if any(normalize_image_url(url) == normalized_feature for url in urls_from_noscript(noscript)):
            return noscript

    for anchor in list(body.find_all("a", href=True)):
        if normalize_image_url(str(anchor.get("href") or "")) == normalized_feature:
            return anchor

    for source in list(body.find_all("source")):
        for url in urls_from_srcset(str(source.get("srcset") or "")):
            if normalize_image_url(url) == normalized_feature:
                return source
    return None


def remove_url_node_with_context(node: Tag) -> None:
    if node.name == "img":
        remove_image_with_context(node)
        return
    if node.name == "noscript":
        remove_media_container_with_context(node)
        return
    if node.name == "source":
        picture = node.find_parent("picture")
        if picture:
            remove_media_container_with_context(picture)
            return
        node.decompose()
        return
    if node.name == "a":
        remove_media_container_with_context(node)
        return
    node.decompose()


def remove_image_with_context(image: Tag) -> None:
    remove_media_container_with_context(image)


def remove_media_container_with_context(node: Tag) -> None:
    removable = image_only_ancestor(node) or node
    removable.decompose()


def image_only_ancestor(node: Tag) -> Tag | None:
    for tag_name in ("p", "figure", "div", "a", "noscript"):
        ancestor = node.find_parent(tag_name)
        if ancestor and contains_only_image_links(ancestor):
            return ancestor
    if node.name in {"p", "figure", "div", "a", "noscript"} and contains_only_image_links(node):
        return node
    return None


def matching_image_urls_in_body(body: Tag, feature_image: str) -> list[str]:
    normalized_feature = normalize_image_url(feature_image)
    matches: list[str] = []
    for url in extract_body_urls(body):
        if normalize_image_url(url) == normalized_feature and url not in matches:
            matches.append(url)
    return matches


def matching_image_tags_in_body(body: Tag, feature_image: str) -> list[Tag]:
    normalized_feature = normalize_image_url(feature_image)
    matches: list[Tag] = []
    for image in body.find_all("img"):
        for attr in ("src", "data-src"):
            if normalize_image_url(str(image.get(attr) or "")) == normalized_feature:
                matches.append(image)
                break
    return matches


def extract_body_urls(body: Tag) -> list[str]:
    urls: list[str] = []
    for image in body.find_all("img"):
        for attr in ("src", "data-src"):
            value = image.get(attr)
            if value:
                urls.append(str(value))
    for anchor in body.find_all("a", href=True):
        urls.append(str(anchor.get("href") or ""))
    for source in body.find_all("source"):
        urls.extend(urls_from_srcset(str(source.get("srcset") or "")))
    for noscript in body.find_all("noscript"):
        urls.extend(urls_from_noscript(noscript))
    return urls


def urls_from_noscript(noscript: Tag) -> list[str]:
    soup = BeautifulSoup(noscript.decode_contents(), "lxml")
    return extract_body_urls(soup)


def urls_from_srcset(value: str) -> list[str]:
    urls: list[str] = []
    for candidate in value.split(","):
        url = candidate.strip().split(" ", 1)[0].strip()
        if url:
            urls.append(url)
    return urls


def contains_only_image_links(node: Tag) -> bool:
    clone = copy.deepcopy(node)
    for media in clone.find_all(["img", "source", "noscript"]):
        media.decompose()
    changed = True
    while changed:
        changed = False
        for child in list(clone.find_all(True)):
            if child.name in {"br", "hr", "picture"}:
                child.decompose()
                changed = True
            elif not child.get_text(strip=True) and not child.find(True):
                child.decompose()
                changed = True
    return not clone.get_text(strip=True) and not clone.find(["img", "video", "iframe", "embed"])


def prune_empty_containers(body: Tag) -> None:
    changed = True
    while changed:
        changed = False
        for node in list(body.find_all(["p", "div", "a", "noscript"])):
            if node.has_attr("data-ghost-bookmark-url"):
                continue
            if node.find(["img", "video", "iframe", "embed"]):
                continue
            if node.get_text(strip=True):
                continue
            if node.find(["br", "hr"]):
                continue
            node.decompose()
            changed = True


def first_image_from_body(body: Tag) -> str | None:
    return image_src(find_feature_image_tag(body))


def find_feature_image_tag(body: Tag) -> Tag | None:
    preferred = [img for img in body.find_all("img") if has_class(img, "PhotoSwipeImage")]
    fallback = list(body.find_all("img"))
    for image in preferred + [img for img in fallback if img not in preferred]:
        if is_feature_image_candidate(image):
            return image
    return None


def image_src(image: Tag | None) -> str | None:
    if not image:
        return None
    return normalize_ghost_image_path(str(image.get("src") or ""))


def first_image_from_html(value: str) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "lxml")
    return first_image_from_body(soup)


def normalize_ghost_image_paths(value: str) -> str:
    value = value.replace('src="images/', f'src="{GHOST_IMAGE_PREFIX}/')
    value = value.replace("src='images/", f"src='{GHOST_IMAGE_PREFIX}/")
    value = value.replace('src="content/images/', f'src="{GHOST_IMAGE_PREFIX}/')
    value = value.replace("src='content/images/", f"src='{GHOST_IMAGE_PREFIX}/")
    return value


def sanitize_cached_html(value: str) -> str:
    soup = BeautifulSoup(value, "lxml")
    root = soup.body or soup
    remove_image_card_blocks(root)
    if soup.body:
        return "".join(str(node) for node in soup.body.contents)
    return str(root)


def normalize_ghost_image_path(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value)
    if value.startswith(GHOST_IMAGE_PREFIX + "/"):
        return value
    if value.startswith("/images/"):
        return GHOST_IMAGE_PREFIX + value[len("/images") :]
    if value.startswith("images/"):
        return f"{GHOST_IMAGE_PREFIX}/{value[len('images/'):]}"
    if value.startswith("content/images/"):
        return f"/{value}"
    return value


def normalize_image_url(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(str(value)).strip()
    if not value:
        return ""
    value = normalize_ghost_image_path(value) or value
    parsed = urlparse(value)
    path = parsed.path or value
    if path.startswith("/images/"):
        path = GHOST_IMAGE_PREFIX + path[len("/images") :]
    elif path.startswith("images/"):
        path = f"{GHOST_IMAGE_PREFIX}/{path[len('images/'):]}"
    elif path.startswith("content/images/"):
        path = f"/{path}"
    return re.sub(r"/+", "/", path).rstrip("/")


def has_class(tag: Tag, class_name: str) -> bool:
    return class_name in tag.get("class", [])


def is_feature_image_candidate(image: Tag) -> bool:
    src = str(image.get("src") or "")
    if not src:
        return False
    path = urlparse(src).path.lower()
    if path.endswith(".svg"):
        return False
    if any(part in path for part in ("favicon", "apple-touch-icon")):
        return False
    if image.find_parent(class_=lambda value: class_list_contains(value, OGP_CARD_CLASSES)):
        return False
    if class_list_contains(image.get("class"), OGP_CARD_CLASSES):
        return False
    width = parse_dimension(image.get("width"))
    height = parse_dimension(image.get("height"))
    if width is None or height is None:
        style = str(image.get("style") or "")
        width = width or parse_css_dimension(style, "width")
        height = height or parse_css_dimension(style, "height")
    if (width is not None and width < 150) or (height is not None and height < 150):
        return False
    if any(token in path for token in ("editor_link", "emoji", "icon", "profile_images")):
        return False
    return True


def class_list_contains(value: object, targets: set[str]) -> bool:
    if not value:
        return False
    if isinstance(value, str):
        classes = value.split()
    else:
        classes = [str(item) for item in value]
    return bool(set(classes) & targets)


def parse_dimension(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def parse_css_dimension(style: str, name: str) -> int | None:
    match = re.search(rf"{name}\s*:\s*(\d+)px", style, re.I)
    return int(match.group(1)) if match else None


def find_blogposting_json_ld(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for item in iter_json_ld_items(data):
            if item.get("@type") == "BlogPosting":
                return item
    return None


def iter_json_ld_items(data: object) -> Iterable[dict]:
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item
        yield data
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item


def parse_datetime(value: str) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def published_month_from_iso(value: str) -> tuple[int, int] | None:
    value = clean_text(value)
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.year, parsed.month
    except ValueError:
        pass
    match = re.search(r"(20\d{2})[/-](\d{1,2})[/-]\d{1,2}", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*\d{1,2}日", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def make_slug(url: str, title: str, published_at: str | None) -> str:
    entry_match = re.search(r"entry-(\d+)\.html", url)
    if entry_match:
        return f"ameblo-{entry_match.group(1)}"
    prefix = ""
    if published_at:
        try:
            prefix = datetime.fromisoformat(published_at.replace("Z", "+00:00")).strftime("%Y%m%d")
        except ValueError:
            prefix = ""
    base = slugify(title)
    slug = "-".join(part for part in [prefix, base] if part)[:180]
    if slug:
        return slug
    digest_source = url or title
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:12]
    return f"ameblo-{digest}"


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return normalized


def stable_tag_slug(value: str) -> str:
    if value in TAG_SLUG_OVERRIDES:
        return TAG_SLUG_OVERRIDES[value]
    if any(ord(char) > 127 for char in value):
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
        return f"ameblo-theme-{digest}"
    ascii_slug = slugify(value)
    if ascii_slug:
        return ascii_slug
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"ameblo-theme-{digest}"


def tag_identity_key(value: str) -> str:
    return unicodedata.normalize("NFKC", clean_text(value)).casefold()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export public Ameblo posts to Ghost JSON.")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--author-id", default=AUTHOR_ID, help="Ghost user id used as post author.")
    parser.add_argument("--author-name", default=AUTHOR_NAME, help="Author name for data.users.")
    parser.add_argument("--author-slug", default=AUTHOR_SLUG, help="Author slug for data.users.")
    parser.add_argument("--author-email", default=AUTHOR_EMAIL, help="Author email for data.users.")
    parser.add_argument("--output", help="Output Ghost import JSON path.")
    parser.add_argument("--output-dir", help="Output directory for ghost-import.json and images_manifest.csv.")
    parser.add_argument("--image-dir", help="Directory for downloaded images.")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--fetched-file", default="fetched_urls.json")
    parser.add_argument("--limit", type=int, help="Maximum posts to fetch. Defaults to 10 in dry-run mode.")
    parser.add_argument("--full", action="store_true", help="Fetch all discovered posts.")
    parser.add_argument("--year", type=int, help="Fetch posts from the specified year using monthly archives.")
    parser.add_argument("--month", type=int, help="Fetch only this month with --year, from 1 to 12.")
    parser.add_argument("--min-delay", type=float, default=1.0)
    parser.add_argument("--max-delay", type=float, default=3.0)
    parser.add_argument("--max-listing-pages", type=int, default=500)
    parser.add_argument(
        "--max-page-scan",
        type=int,
        default=500,
        help="Maximum page-N.html number to scan when supplementing year/month archives.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--refresh", action="store_true", help="Refetch even when URL exists in fetched_urls.json.")
    parser.add_argument(
        "--diff-only",
        action="store_true",
        help="Skip URLs already present in fetched_urls.json and export only newly discovered articles.",
    )
    parser.add_argument(
        "--remove-feature-image-from-body",
        action="store_true",
        help=(
            "Deprecated alias for --remove-duplicate-noscript-images. "
            "Does not remove the feature image from body."
        ),
    )
    parser.add_argument(
        "--remove-duplicate-noscript-images",
        action="store_true",
        help="Remove Ameblo noscript image duplicates while keeping normal body img tags.",
    )
    parser.add_argument(
        "--ghost-bookmark-cards",
        action="store_true",
        help=(
            "Convert eligible standalone URL/OGP links to Ghost Lexical bookmark nodes. "
            "Posts with bookmark nodes are emitted through posts[].lexical only."
        ),
    )
    parser.add_argument(
        "--debug-title",
        action="store_true",
        help="Print title candidates and the selected title for each fetched article.",
    )
    parser.add_argument(
        "--no-users",
        action="store_true",
        help="Do not output data.users; use AUTHOR_ID as an existing Ghost user id.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.month and not args.year:
        raise SystemExit("--month requires --year")
    if args.month and not 1 <= args.month <= 12:
        raise SystemExit("--month must be between 1 and 12")
    if args.diff_only and args.refresh:
        raise SystemExit("--diff-only cannot be used with --refresh")
    if args.year and args.full:
        print("[info] --year is set; ignoring --full and using the year archive scope.")
    if args.remove_feature_image_from_body:
        print(
            "[warn] --remove-feature-image-from-body is deprecated; "
            "using --remove-duplicate-noscript-images behavior instead."
        )
    dry_run = not args.full and not args.year
    if args.year or args.full:
        limit = args.limit or 0
    else:
        limit = args.limit or 10
    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif args.year:
        output_dir = Path(f"output{args.year}Diff" if args.diff_only else f"output{args.year}")
    else:
        output_dir = Path("outputDiff" if args.diff_only else "output")
    output_json = Path(args.output) if args.output else output_dir / "ghost-import.json"
    image_dir = Path(args.image_dir) if args.image_dir else output_dir / "content" / "images"
    config = ExportConfig(
        base_url=args.base_url,
        output_json=output_json,
        image_dir=image_dir,
        logs_dir=Path(args.logs_dir),
        fetched_file=Path(args.fetched_file),
        dry_run=dry_run,
        limit=limit,
        min_delay=args.min_delay,
        max_delay=max(args.min_delay, args.max_delay),
        max_listing_pages=args.max_listing_pages,
        max_page_scan=args.max_page_scan,
        timeout=args.timeout,
        download_images=True,
        refresh=args.refresh,
        diff_only=args.diff_only,
        remove_feature_image_from_body=args.remove_feature_image_from_body,
        remove_duplicate_noscript_images=(
            args.remove_duplicate_noscript_images or args.remove_feature_image_from_body
        ),
        ghost_bookmark_cards=args.ghost_bookmark_cards,
        debug_title=args.debug_title,
        year=args.year,
        month=args.month,
        include_users=not args.no_users,
        author_id=args.author_id,
        author_name=args.author_name,
        author_slug=args.author_slug,
        author_email=args.author_email,
    )
    AmebloExporter(config).run()


if __name__ == "__main__":
    main()
