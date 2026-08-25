from __future__ import annotations

import base64
import mimetypes
import re
import tomllib
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import markdown  # type: ignore[import-untyped]

READING_CONTENT_DIR = Path(__file__).with_name("content") / "reading"
REQUIRED_FIELDS = {
    "title",
    "heading",
    "deck",
    "eyebrow",
    "subtitle",
    "summary",
    "published_on",
    "tags",
    "status",
}
VALID_STATUSES = {"draft", "published"}
_RELATIVE_IMAGE_SOURCE = re.compile(
    r'(<img\b[^>]*?\bsrc=["\'])(?!/|[a-z][a-z0-9+.-]*:|//)([^"\']+)(["\'])',
    re.IGNORECASE,
)


class ArticleFormatError(ValueError):
    """Raised when a repository-managed article is malformed."""


def _split_front_matter(source: str, path: Path) -> tuple[dict[str, Any], str]:
    if not source.startswith("+++\n"):
        raise ArticleFormatError(f"{path}: missing TOML front matter")
    closing = source.find("\n+++\n", 4)
    if closing < 0:
        raise ArticleFormatError(f"{path}: unterminated TOML front matter")
    try:
        metadata = tomllib.loads(source[4:closing])
    except tomllib.TOMLDecodeError as exc:
        raise ArticleFormatError(f"{path}: invalid TOML front matter: {exc}") from exc
    return metadata, source[closing + 5 :].strip()


def _load_article(path: Path) -> dict[str, Any]:
    metadata, body = _split_front_matter(path.read_text(encoding="utf-8"), path)
    missing = sorted(REQUIRED_FIELDS - metadata.keys())
    if missing:
        raise ArticleFormatError(f"{path}: missing fields: {', '.join(missing)}")

    slug = path.parent.name
    published_on = str(metadata["published_on"])
    try:
        date.fromisoformat(published_on)
    except ValueError as exc:
        raise ArticleFormatError(f"{path}: invalid published_on {published_on!r}") from exc

    status = str(metadata["status"])
    if status not in VALID_STATUSES:
        raise ArticleFormatError(f"{path}: status must be draft or published")
    tags = metadata["tags"]
    if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) for tag in tags):
        raise ArticleFormatError(f"{path}: tags must be a non-empty string array")
    if not body:
        raise ArticleFormatError(f"{path}: article body is empty")

    body_html = markdown.markdown(
        body,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )
    for match in _RELATIVE_IMAGE_SOURCE.finditer(body_html):
        relative_source = unquote(urlsplit(match.group(2)).path)
        asset = (path.parent / relative_source).resolve()
        article_dir = path.parent.resolve()
        if not asset.is_relative_to(article_dir) or not asset.is_file():
            raise ArticleFormatError(f"{path}: missing or unsafe image {match.group(2)!r}")
    body_html = _RELATIVE_IMAGE_SOURCE.sub(
        rf'\1/a/reading/{slug}/assets/\2\3',
        body_html,
    )
    return {
        **metadata,
        "published_on": published_on,
        "updated_on": published_on,
        "tags": tuple(tags),
        "slug": slug,
        "href": f"/a/reading/{slug}",
        "body_html": body_html,
        "body_markdown": body,
        "source_path": path,
    }


def repository_article_payload(article: dict[str, Any]) -> dict[str, Any]:
    """Convert a bundled article into the user-reading API payload."""
    source_path = Path(article["source_path"])
    article_dir = source_path.parent.resolve()
    assets: dict[str, dict[str, str]] = {}
    for asset in sorted(article_dir.rglob("*")):
        if not asset.is_file() or asset == source_path:
            continue
        relative = asset.relative_to(article_dir).as_posix()
        media_type = mimetypes.guess_type(asset.name)[0] or ""
        assets[relative] = {
            "media_type": media_type,
            "data": base64.b64encode(asset.read_bytes()).decode("ascii"),
        }
    source_reference = source_path.relative_to(READING_CONTENT_DIR).as_posix()
    return {
        "slug": str(article["slug"]),
        "title": str(article["title"]),
        "summary": str(article["summary"]),
        "tags": list(article["tags"]),
        "body_markdown": str(article["body_markdown"]),
        "assets": assets,
        "metadata": {
            key: article[key]
            for key in ("heading", "deck", "eyebrow", "subtitle")
        }
        | {
            "repository_import": {
                "source": source_reference,
                "published_on": str(article["published_on"]),
                "status": str(article["status"]),
            }
        },
    }


@lru_cache(maxsize=1)
def all_articles() -> tuple[dict[str, Any], ...]:
    articles = tuple(
        sorted(
            (_load_article(path) for path in READING_CONTENT_DIR.glob("*/index.md")),
            key=lambda article: (article["published_on"], article["slug"]),
            reverse=True,
        )
    )
    slugs = [str(article["slug"]) for article in articles]
    if len(slugs) != len(set(slugs)):
        raise ArticleFormatError("duplicate reading article slug")
    return articles


def published_articles() -> tuple[dict[str, Any], ...]:
    return tuple(article for article in all_articles() if article["status"] == "published")


def published_article_summaries() -> tuple[dict[str, Any], ...]:
    fields = ("title", "summary", "href", "updated_on", "tags", "slug")
    return tuple({field: article[field] for field in fields} for article in published_articles())


def published_article(slug: str) -> dict[str, Any] | None:
    return next((article for article in published_articles() if article["slug"] == slug), None)
