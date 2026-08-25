from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from datetime import date
from typing import Any
from uuid import uuid4

import markdown  # type: ignore[import-untyped]

from .users import UserRepository

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_RELATIVE_IMAGE_SOURCE = re.compile(
    r'(<img\b[^>]*?\bsrc=["\'])(?!/|[a-z][a-z0-9+.-]*:|//)([^"\']+)(["\'])',
    re.IGNORECASE,
)
_ALLOWED_IMAGE_TYPES = {
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}


class ReadingConflict(ValueError):
    pass


class ReadingNoteService:
    """Private reading drafts, immutable submitted versions, and explicit owner shares."""

    def __init__(self, repository: UserRepository):
        self.repository = repository

    def list_notes(self, user_id: str) -> list[dict[str, Any]]:
        with self.repository.connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT n.*, v.version_number AS submitted_version_number,
                          v.submitted_at
                   FROM user_reading_note n
                   LEFT JOIN user_reading_note_version v
                     ON v.version_id=n.current_version_id
                   WHERE n.user_id=?
                   ORDER BY n.updated_at DESC, n.note_id DESC""",
                (user_id,),
            ).fetchall()
        return [self._draft(row) for row in rows]

    def get(self, user_id: str, note_id: str) -> dict[str, Any] | None:
        with self.repository.connect(read_only=True) as connection:
            row = connection.execute(
                """SELECT n.*, v.version_number AS submitted_version_number,
                          v.submitted_at
                   FROM user_reading_note n
                   LEFT JOIN user_reading_note_version v
                     ON v.version_id=n.current_version_id
                   WHERE n.user_id=? AND n.note_id=?""",
                (user_id, note_id),
            ).fetchone()
        return self._draft(row) if row else None

    def versions(self, user_id: str, note_id: str) -> list[dict[str, Any]]:
        with self.repository.connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT v.* FROM user_reading_note_version v
                   JOIN user_reading_note n ON n.note_id=v.note_id
                   WHERE n.user_id=? AND n.note_id=?
                   ORDER BY v.version_number DESC""",
                (user_id, note_id),
            ).fetchall()
        return [self._version(row) for row in rows]

    def save(
        self,
        user_id: str,
        payload: dict[str, Any],
        *,
        note_id: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if note_id and "assets" not in payload:
            with self.repository.connect(read_only=True) as read_connection:
                existing = read_connection.execute(
                    "SELECT assets_json FROM user_reading_note WHERE user_id=? AND note_id=?",
                    (user_id, note_id),
                ).fetchone()
            if existing is None:
                raise ValueError("阅读感想不存在")
            payload = {**payload, "assets": json.loads(str(existing["assets_json"]))}
        normalized = self.validate_payload(payload)
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if note_id:
                current = connection.execute(
                    "SELECT * FROM user_reading_note WHERE user_id=? AND note_id=?",
                    (user_id, note_id),
                ).fetchone()
                if current is None:
                    raise ValueError("阅读感想不存在")
                if expected_revision is None or int(current["revision"]) != expected_revision:
                    raise ReadingConflict("草稿已被更新，请刷新后重试")
                conflict = connection.execute(
                    "SELECT 1 FROM user_reading_note WHERE user_id=? AND slug=? AND note_id<>?",
                    (user_id, normalized["slug"], note_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("你已使用该 slug")
                connection.execute(
                    """UPDATE user_reading_note
                       SET slug=?, title=?, summary=?, tags_json=?, body_markdown=?,
                           metadata_json=?, assets_json=?, revision=revision+1,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE user_id=? AND note_id=?""",
                    (
                        normalized["slug"],
                        normalized["title"],
                        normalized["summary"],
                        json.dumps(normalized["tags"], ensure_ascii=False),
                        normalized["body_markdown"],
                        json.dumps(normalized["metadata"], ensure_ascii=False, sort_keys=True),
                        json.dumps(normalized["assets"], ensure_ascii=False, sort_keys=True),
                        user_id,
                        note_id,
                    ),
                )
            else:
                conflict = connection.execute(
                    "SELECT 1 FROM user_reading_note WHERE user_id=? AND slug=?",
                    (user_id, normalized["slug"]),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("你已使用该 slug")
                note_id = uuid4().hex
                connection.execute(
                    """INSERT INTO user_reading_note(
                           note_id, user_id, slug, title, summary, tags_json,
                           body_markdown, metadata_json, assets_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        note_id,
                        user_id,
                        normalized["slug"],
                        normalized["title"],
                        normalized["summary"],
                        json.dumps(normalized["tags"], ensure_ascii=False),
                        normalized["body_markdown"],
                        json.dumps(normalized["metadata"], ensure_ascii=False, sort_keys=True),
                        json.dumps(normalized["assets"], ensure_ascii=False, sort_keys=True),
                    ),
                )
            row = connection.execute(
                "SELECT * FROM user_reading_note WHERE note_id=?", (note_id,)
            ).fetchone()
        assert row is not None
        return self._draft(row)

    def submit(self, user_id: str, note_id: str, expected_revision: int) -> dict[str, Any]:
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            note = connection.execute(
                "SELECT * FROM user_reading_note WHERE user_id=? AND note_id=?",
                (user_id, note_id),
            ).fetchone()
            if note is None:
                raise ValueError("阅读感想不存在")
            if int(note["revision"]) != expected_revision:
                raise ReadingConflict("草稿已被更新，请刷新后重试")
            latest = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) FROM user_reading_note_version "
                "WHERE note_id=?",
                (note_id,),
            ).fetchone()
            version_number = int(latest[0]) + 1
            version_id = uuid4().hex
            content_hash = self._content_hash(note)
            connection.execute(
                """INSERT INTO user_reading_note_version(
                       version_id, note_id, version_number, slug, title, summary,
                       tags_json, body_markdown, metadata_json, assets_json, content_hash
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    note_id,
                    version_number,
                    note["slug"],
                    note["title"],
                    note["summary"],
                    note["tags_json"],
                    note["body_markdown"],
                    note["metadata_json"],
                    note["assets_json"],
                    content_hash,
                ),
            )
            connection.execute(
                """UPDATE user_reading_note
                   SET current_version_id=?, updated_at=CURRENT_TIMESTAMP
                   WHERE note_id=?""",
                (version_id, note_id),
            )
            row = connection.execute(
                "SELECT * FROM user_reading_note_version WHERE version_id=?", (version_id,)
            ).fetchone()
        assert row is not None
        return self._version(row)

    def share(
        self,
        owner_id: str,
        note_id: str,
        version_id: str,
        statement: str = "",
    ) -> dict[str, Any]:
        statement = statement.strip()
        if len(statement) > 300:
            raise ValueError("共享说明不能超过300字")
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute(
                """SELECT v.* FROM user_reading_note_version v
                   JOIN user_reading_note n ON n.note_id=v.note_id
                   WHERE n.user_id=? AND n.note_id=? AND v.version_id=?""",
                (owner_id, note_id, version_id),
            ).fetchone()
            if version is None:
                raise ValueError("只能共享自己已提交的版本")
            active = connection.execute(
                """SELECT share_id, version_id FROM user_reading_note_share
                   WHERE note_id=? AND status='ACTIVE'""",
                (note_id,),
            ).fetchone()
            if active is not None and str(active["version_id"]) == version_id:
                raise ValueError("该版本已在共享")
            if active is not None:
                connection.execute(
                    """UPDATE user_reading_note_share SET status='SUPERSEDED'
                       WHERE share_id=?""",
                    (active["share_id"],),
                )
            share_id = uuid4().hex
            connection.execute(
                """INSERT INTO user_reading_note_share(
                       share_id, note_id, version_id, statement, status
                   ) VALUES (?, ?, ?, ?, 'ACTIVE')""",
                (share_id, note_id, version_id, statement),
            )
            row = self._share_row(connection, owner_id, share_id)
        assert row is not None
        return self._shared(row)

    def revoke(self, owner_id: str, note_id: str) -> int:
        with self.repository.connect() as connection:
            cursor = connection.execute(
                """UPDATE user_reading_note_share
                   SET status='REVOKED', revoked_at=CURRENT_TIMESTAMP
                   WHERE note_id IN (
                       SELECT note_id FROM user_reading_note WHERE user_id=? AND note_id=?
                   ) AND status IN ('ACTIVE','SUPERSEDED')""",
                (owner_id, note_id),
            )
        return int(cursor.rowcount)

    def active_share_for_note(self, owner_id: str, note_id: str) -> dict[str, Any] | None:
        with self.repository.connect(read_only=True) as connection:
            row = connection.execute(
                """SELECT s.*, v.*, s.status AS share_status,
                          s.shared_at AS share_shared_at
                   FROM user_reading_note_share s
                   JOIN user_reading_note_version v ON v.version_id=s.version_id
                   JOIN user_reading_note n ON n.note_id=s.note_id
                   WHERE n.user_id=? AND n.note_id=? AND s.status='ACTIVE'""",
                (owner_id, note_id),
            ).fetchone()
        return self._shared(row) if row else None

    def shared_notes(self, owner_id: str) -> list[dict[str, Any]]:
        with self.repository.connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT s.*, v.*, s.status AS share_status,
                          s.shared_at AS share_shared_at
                   FROM user_reading_note_share s
                   JOIN user_reading_note_version v ON v.version_id=s.version_id
                   JOIN user_reading_note n ON n.note_id=s.note_id
                   WHERE n.user_id=? AND s.status='ACTIVE'
                   ORDER BY s.shared_at DESC, s.share_id DESC""",
                (owner_id,),
            ).fetchall()
        return [self._shared(row) for row in rows]

    def shared_by_id(self, owner_id: str, share_id: str) -> dict[str, Any] | None:
        with self.repository.connect(read_only=True) as connection:
            row = self._share_row(connection, owner_id, share_id)
        if row is None or str(row["share_status"]) == "REVOKED":
            return None
        return self._shared(row)

    def shared_asset(
        self, owner_id: str, share_id: str, asset_path: str
    ) -> tuple[bytes, str] | None:
        with self.repository.connect(read_only=True) as connection:
            row = self._share_row(connection, owner_id, share_id)
        if row is None or str(row["share_status"]) == "REVOKED":
            return None
        assets = json.loads(str(row["assets_json"]))
        asset = assets.get(asset_path) if isinstance(assets, dict) else None
        if not isinstance(asset, dict):
            return None
        try:
            data = base64.b64decode(str(asset["data"]), validate=True)
        except (KeyError, ValueError):
            return None
        return data, str(asset.get("media_type") or "application/octet-stream")

    @staticmethod
    def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        slug = str(payload.get("slug") or "").strip().lower()
        summary = str(payload.get("summary") or "").strip()
        body = str(payload.get("body_markdown") or payload.get("body") or "").strip()
        raw_tags = payload.get("tags", [])
        if not title or len(title) > 160:
            raise ValueError("标题必填且不能超过160字")
        if not _SLUG.fullmatch(slug) or len(slug) > 100:
            raise ValueError("slug 只能使用小写字母、数字和单个连字符")
        if not summary or len(summary) > 500:
            raise ValueError("摘要必填且不能超过500字")
        if not body or len(body) > 200_000:
            raise ValueError("正文必填且不能超过200000字")
        if not isinstance(raw_tags, list):
            raise ValueError("tags 必须是字符串数组")
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        if not tags or len(tags) > 10 or any(len(tag) > 30 for tag in tags):
            raise ValueError("请提供1至10个标签，每个不超过30字")
        image_sources = [*_IMAGE.findall(body)] + [
            match.group(2) for match in _RELATIVE_IMAGE_SOURCE.finditer(body)
        ]
        if any(source.startswith("http://") for source in image_sources):
            raise ValueError("外部图片必须使用 HTTPS")
        relative_images = [
            source
            for source in image_sources
            if not source.startswith(("https://", "/"))
        ]
        raw_assets = payload.get("assets", {})
        if not isinstance(raw_assets, dict) or len(raw_assets) > 50:
            raise ValueError("assets 必须是不超过50项的对象")
        assets: dict[str, dict[str, str]] = {}
        total_bytes = 0
        for raw_path, raw_asset in raw_assets.items():
            asset_path = str(raw_path)
            if (
                not asset_path
                or asset_path.startswith(("/", "."))
                or "\\" in asset_path
                or ".." in asset_path.split("/")
                or not isinstance(raw_asset, dict)
            ):
                raise ValueError("图片路径不安全")
            media_type = str(raw_asset.get("media_type") or "").lower()
            if media_type not in _ALLOWED_IMAGE_TYPES:
                raise ValueError("图片只支持 JPEG、PNG、GIF 和 WebP")
            try:
                decoded = base64.b64decode(str(raw_asset.get("data") or ""), validate=True)
            except ValueError as exc:
                raise ValueError(f"图片 {asset_path} 不是有效 Base64") from exc
            signatures = _ALLOWED_IMAGE_TYPES[media_type]
            if not decoded.startswith(signatures) or (
                media_type == "image/webp" and decoded[8:12] != b"WEBP"
            ):
                raise ValueError(f"图片 {asset_path} 内容与类型不匹配")
            total_bytes += len(decoded)
            if total_bytes > 20 * 1024 * 1024:
                raise ValueError("单篇阅读感想的图片总大小不能超过20 MB")
            assets[asset_path] = {
                "media_type": media_type,
                "data": base64.b64encode(decoded).decode("ascii"),
            }
        missing_assets = sorted(set(relative_images) - assets.keys())
        if missing_assets:
            raise ValueError("缺少正文引用的图片：" + ", ".join(missing_assets))
        raw_metadata = payload.get("metadata")
        metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        return {
            "title": title,
            "slug": slug,
            "summary": summary,
            "tags": tags,
            "body_markdown": body,
            "metadata": metadata,
            "assets": assets,
        }

    @staticmethod
    def _share_row(
        connection: sqlite3.Connection, owner_id: str, share_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT s.*, v.*, s.status AS share_status,
                      s.shared_at AS share_shared_at
               FROM user_reading_note_share s
               JOIN user_reading_note_version v ON v.version_id=s.version_id
               JOIN user_reading_note n ON n.note_id=s.note_id
               WHERE n.user_id=? AND s.share_id=?""",
            (owner_id, share_id),
        ).fetchone()

    @staticmethod
    def _content_hash(row: Any) -> str:
        canonical = json.dumps(
            {
                key: row[key]
                for key in (
                    "slug", "title", "summary", "tags_json", "body_markdown",
                    "metadata_json", "assets_json"
                )
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def payload_content_hash(cls, payload: dict[str, Any]) -> str:
        normalized = cls.validate_payload(payload)
        return cls._content_hash(
            {
                "slug": normalized["slug"],
                "title": normalized["title"],
                "summary": normalized["summary"],
                "tags_json": json.dumps(normalized["tags"], ensure_ascii=False),
                "body_markdown": normalized["body_markdown"],
                "metadata_json": json.dumps(
                    normalized["metadata"], ensure_ascii=False, sort_keys=True
                ),
                "assets_json": json.dumps(
                    normalized["assets"], ensure_ascii=False, sort_keys=True
                ),
            }
        )

    @staticmethod
    def _draft(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["tags"] = json.loads(str(value.pop("tags_json")))
        value["metadata"] = json.loads(str(value.pop("metadata_json")))
        assets = json.loads(str(value.pop("assets_json", "{}")))
        value["asset_paths"] = sorted(assets) if isinstance(assets, dict) else []
        value["href"] = f"/a/reading/mine/{value['note_id']}"
        return value

    @staticmethod
    def _version(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["tags"] = json.loads(str(value.pop("tags_json")))
        value["metadata"] = json.loads(str(value.pop("metadata_json")))
        assets = json.loads(str(value.pop("assets_json", "{}")))
        value["asset_paths"] = sorted(assets) if isinstance(assets, dict) else []
        return value

    @classmethod
    def _shared(cls, row: Any) -> dict[str, Any]:
        value = cls._version(row)
        value["shared_at"] = str(value.pop("share_shared_at", value.get("shared_at", "")))
        value["published_on"] = value["shared_at"][:10] or date.today().isoformat()
        value["updated_on"] = value["shared_at"] or value["published_on"]
        value["href"] = f"/a/owner/reading/{value['share_id']}"
        value["heading"] = str(value["metadata"].get("heading") or value["title"])
        value["deck"] = str(value["metadata"].get("deck") or value["summary"])
        value["eyebrow"] = str(value["metadata"].get("eyebrow") or "阅读感想")
        value["subtitle"] = str(value["metadata"].get("subtitle") or "站长共享")
        value["body_html"] = markdown.markdown(
            str(value["body_markdown"]), extensions=["extra", "sane_lists"], output_format="html5"
        )
        value["body_html"] = _RELATIVE_IMAGE_SOURCE.sub(
            rf'\1/a/owner/reading/{value["share_id"]}/assets/\2\3',
            value["body_html"],
        )
        return value
