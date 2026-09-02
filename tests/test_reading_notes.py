from __future__ import annotations

import base64
import re

from fastapi.testclient import TestClient

from master_stock_selector.cli import main
from master_stock_selector.web.app import create_app
from master_stock_selector.web.reading_notes import ReadingConflict
from master_stock_selector.web.users import UserRepository
from master_stock_selector.web.users_cli import migrate_static_reading_articles


def _payload(*, title: str = "私有感想", body: str = "## 一、边界\n\n只有我可见。") -> dict:
    return {
        "slug": "private-reading-note",
        "title": title,
        "summary": "一篇用于验证私有与共享边界的阅读感想。",
        "tags": ["风险管理", "交易边界"],
        "body_markdown": body,
    }


def _app(tmp_path):
    user_database = tmp_path / "users.sqlite3"
    users = UserRepository(user_database)
    users.migrate_schema()
    owner_id = users.create_user("站长用户", "Owner-password-123", "站长")
    reader_id = users.create_user("reader", "Reader-password-123", "读者")
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=tmp_path / "watchlist.sqlite3",
        user_database=user_database,
        secure_cookies=False,
        site_owner_username="站长用户",
    )
    return app, app.state.user_repository, owner_id, reader_id


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_private_draft_submission_and_frozen_version_boundary(tmp_path) -> None:
    _, users, _, reader_id = _app(tmp_path)

    draft = users.reading_notes.save(reader_id, _payload())
    assert draft["revision"] == 1
    assert draft["current_version_id"] is None
    assert users.reading_notes.shared_notes(reader_id) == []

    submitted = users.reading_notes.submit(reader_id, draft["note_id"], 1)
    assert submitted["version_number"] == 1
    assert users.reading_notes.shared_notes(reader_id) == []

    changed = users.reading_notes.save(
        reader_id,
        _payload(title="修改后的草稿", body="新草稿，不应改变旧版本。"),
        note_id=draft["note_id"],
        expected_revision=1,
    )
    assert changed["revision"] == 2
    versions = users.reading_notes.versions(reader_id, draft["note_id"])
    assert versions[0]["title"] == "私有感想"
    assert versions[0]["body_markdown"].endswith("只有我可见。")

    try:
        users.reading_notes.save(
            reader_id,
            _payload(),
            note_id=draft["note_id"],
            expected_revision=1,
        )
    except ReadingConflict:
        pass
    else:
        raise AssertionError("过期修订号必须被拒绝")


def test_shared_note_uses_shanghai_display_time_across_utc_date_boundary(tmp_path) -> None:
    _, users, owner_id, _ = _app(tmp_path)
    note = users.reading_notes.save(owner_id, _payload())
    version = users.reading_notes.submit(owner_id, note["note_id"], 1)
    share = users.reading_notes.share(owner_id, note["note_id"], version["version_id"])

    with users.connect() as connection:
        connection.execute(
            "UPDATE user_reading_note_share SET shared_at=? WHERE share_id=?",
            ("2026-08-25 23:31:26", share["share_id"]),
        )

    shared = users.reading_notes.shared_by_id(owner_id, share["share_id"])
    assert shared is not None
    assert shared["shared_at"] == "2026-08-26 07:31:26"
    assert shared["published_on"] == "2026-08-26"
    assert shared["updated_on"] == "2026-08-26 07:31:26"


def test_agent_api_keeps_all_reading_operations_owner_only(tmp_path) -> None:
    app, users, owner_id, reader_id = _app(tmp_path)
    client = TestClient(app)
    scopes = ("reading:read", "reading:write", "reading:share")
    reader_token, _ = users.create_api_token(reader_id, "reader", scopes=scopes)
    owner_token, _ = users.create_api_token(owner_id, "owner", scopes=scopes)

    blocked_responses = (
        client.get("/api/v1/reading", headers=_headers(reader_token)),
        client.post(
            "/api/v1/reading/validate",
            headers=_headers(reader_token),
            json=_payload(),
        ),
        client.post(
            "/api/v1/reading",
            headers=_headers(reader_token),
            json=_payload(),
        ),
    )
    for response in blocked_responses:
        assert response.status_code == 403
        assert response.json()["detail"] == "阅读感想功能暂未对其他用户开放"
    assert users.reading_notes.shared_notes(reader_id) == []

    owner_saved = client.post(
        "/api/v1/reading",
        headers=_headers(owner_token),
        json=_payload(title="站长的私有感想", body="共享前只有站长可见。"),
    ).json()["note"]
    owner_version = client.post(
        f"/api/v1/reading/{owner_saved['note_id']}/submit",
        headers=_headers(owner_token),
        json={"expected_revision": 1},
    ).json()["version"]
    shared = client.post(
        f"/api/v1/reading/{owner_saved['note_id']}/share",
        headers=_headers(owner_token),
        json={"version_id": owner_version["version_id"], "statement": "明确共享。"},
    )
    assert shared.status_code == 200
    assert shared.json()["share"]["title"] == "站长的私有感想"


def test_owner_share_appears_for_logged_in_reader_and_draft_changes_do_not_leak(tmp_path) -> None:
    app, users, owner_id, _ = _app(tmp_path)
    note = users.reading_notes.save(
        owner_id,
        _payload(title="已共享的冻结标题", body="这是已共享的冻结正文。"),
    )
    version = users.reading_notes.submit(owner_id, note["note_id"], 1)
    share = users.reading_notes.share(owner_id, note["note_id"], version["version_id"])
    users.reading_notes.save(
        owner_id,
        _payload(title="未共享的新草稿", body="这个内容必须保持私有。"),
        note_id=note["note_id"],
        expected_revision=1,
    )

    client = TestClient(app)
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303
    archive = client.get("/a/reading")
    assert archive.status_code == 200
    assert "已共享的冻结标题" in archive.text
    assert "未共享的新草稿" not in archive.text

    page = client.get(share["href"])
    assert page.status_code == 200
    assert "这是已共享的冻结正文" in page.text
    assert "这个内容必须保持私有" not in page.text
    activity = client.get("/a/owner")
    assert activity.status_code == 200
    assert "已共享的冻结标题" in activity.text
    assert "未共享的新草稿" not in activity.text

    users.reading_notes.revoke(owner_id, note["note_id"])
    assert client.get(share["href"]).status_code == 404


def test_shared_article_navigation_follows_publication_dates(tmp_path) -> None:
    app, users, owner_id, _ = _app(tmp_path)
    shares = []
    for index, title in enumerate(("第一篇", "第二篇", "第三篇"), start=1):
        payload = _payload(title=title, body=f"{title}正文。")
        payload["slug"] = f"shared-note-{index}"
        note = users.reading_notes.save(owner_id, payload)
        version = users.reading_notes.submit(owner_id, note["note_id"], 1)
        shares.append(
            users.reading_notes.share(owner_id, note["note_id"], version["version_id"])
        )

    with users.connect() as connection:
        for index, share in enumerate(shares, start=1):
            connection.execute(
                "UPDATE user_reading_note_share SET shared_at=? WHERE share_id=?",
                (f"2099-08-{index:02d} 00:00:00", share["share_id"]),
            )

    client = TestClient(app)
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    first = client.get(shares[0]["href"])
    assert f'href="{shares[1]["href"]}"' in first.text

    middle = client.get(shares[1]["href"])
    assert f'href="{shares[0]["href"]}"' in middle.text
    assert f'href="{shares[2]["href"]}"' in middle.text

    newest = client.get(shares[2]["href"])
    assert f'href="{shares[1]["href"]}"' in newest.text
    assert "已经是最新一篇" in newest.text


def test_regular_users_do_not_see_or_open_private_reading_workspace(tmp_path) -> None:
    app, _, _, _ = _app(tmp_path)
    client = TestClient(app)
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    workspace = client.get("/a/reading/mine")
    assert workspace.status_code == 404
    assert client.get("/a/reading/mine/new").status_code == 404
    assert 'href="/a/reading/mine"' not in client.get("/a/daily").text

    forbidden = client.post("/a/reading/mine/not-owned/share")
    assert forbidden.status_code == 404


def test_owner_website_save_and_submit_stay_private_until_shared(tmp_path) -> None:
    app, users, owner_id, _ = _app(tmp_path)
    client = TestClient(app)
    client.post(
        "/login",
        data={"username": "站长用户", "password": "Owner-password-123"},
    )
    assert 'href="/a/reading/mine"' not in client.get("/a/daily").text
    token_page = client.get("/account/tokens")
    assert 'name="reading:read" checked' in token_page.text
    assert 'href="/a/reading/mine"' in token_page.text
    editor = client.get("/a/reading/mine/new")
    assert editor.status_code == 200
    assert "站长写作台" in editor.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', editor.text)
    assert csrf is not None
    saved = client.post(
        "/a/reading/mine/save",
        data={
            "csrf_token": csrf.group(1),
            "title": "站长的私有感想",
            "slug": "owner-private-note",
            "summary": "这篇感想保存和提交后仍然不能自动进入站长动态。",
            "tags": "私有, 阅读",
            "body_markdown": "## 私有正文\n\n不会自动共享。",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    note_id = saved.headers["location"].split("/")[-1].split("?")[0]
    note = users.reading_notes.get(owner_id, note_id)
    assert note is not None
    submitted = client.post(
        f"/a/reading/mine/{note_id}/submit",
        data={"csrf_token": csrf.group(1), "expected_revision": note["revision"]},
        follow_redirects=False,
    )
    assert submitted.status_code == 303
    assert len(users.reading_notes.versions(owner_id, note_id)) == 1
    assert users.reading_notes.shared_notes(owner_id) == []
    assert "站长的私有感想" not in client.get("/a/reading").text


def test_shared_version_keeps_and_serves_its_own_image_assets(tmp_path) -> None:
    app, users, owner_id, _ = _app(tmp_path)
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    payload = _payload(body="![图表](images/pixel.png)\n\n冻结版本图片。")
    payload["assets"] = {
        "images/pixel.png": {
            "media_type": "image/png",
            "data": base64.b64encode(pixel).decode("ascii"),
        }
    }
    note = users.reading_notes.save(owner_id, payload)
    version = users.reading_notes.submit(owner_id, note["note_id"], 1)
    share = users.reading_notes.share(owner_id, note["note_id"], version["version_id"])

    client = TestClient(app)
    client.post("/login", data={"username": "reader", "password": "Reader-password-123"})
    asset_path = f"{share['href']}/assets/images/pixel.png"
    page = client.get(share["href"])
    assert asset_path in page.text
    image = client.get(asset_path)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == pixel


def test_cli_validates_markdown_directory_and_collects_images(tmp_path, capsys) -> None:
    article = tmp_path / "cli-reading-note"
    images = article / "images"
    images.mkdir(parents=True)
    images.joinpath("pixel.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    article.joinpath("index.md").write_text(
        """+++
title = "CLI 阅读感想"
summary = "通过 CLI 检查并收集图片的私有阅读感想。"
tags = ["CLI", "阅读"]
+++

![图表](images/pixel.png)
""",
        encoding="utf-8",
    )

    assert main(["agent", "reading", "validate", str(article)]) == 0
    output = capsys.readouterr().out
    assert '"status": "READY"' in output
    assert '"slug": "cli-reading-note"' in output


def test_static_articles_migrate_to_owner_versions_and_shares_idempotently(tmp_path) -> None:
    database = tmp_path / "users.sqlite3"
    users = UserRepository(database)
    users.migrate_schema()
    owner_id = users.create_user("站长用户", "Owner-password-123", "站长")

    preview = migrate_static_reading_articles(
        repository=users,
        username="站长用户",
        apply=False,
        backup=None,
    )
    assert preview["status"] == "dry-run"
    assert preview["article_count"] == 11
    assert preview["summary"] == {
        "CREATE": 11,
        "UPDATE": 0,
        "SHARE": 0,
        "SKIP": 0,
        "CONFLICT": 0,
    }

    backup = tmp_path / "before-reading-import.sqlite3"
    migrated = migrate_static_reading_articles(
        repository=users,
        username="站长用户",
        apply=True,
        backup=backup,
    )
    assert migrated["status"] == "migrated"
    assert backup.is_file()
    assert len(users.reading_notes.list_notes(owner_id)) == 11
    assert len(users.reading_notes.shared_notes(owner_id)) == 11
    assert all(
        len(users.reading_notes.versions(owner_id, note["note_id"])) == 1
        for note in users.reading_notes.list_notes(owner_id)
    )

    repeated = migrate_static_reading_articles(
        repository=users,
        username="站长用户",
        apply=False,
        backup=None,
    )
    assert repeated["summary"]["SKIP"] == 11
    assert repeated["summary"]["CREATE"] == 0


def test_static_article_import_refuses_non_migration_slug_conflict(tmp_path) -> None:
    users = UserRepository(tmp_path / "users.sqlite3")
    users.migrate_schema()
    owner_id = users.create_user("站长用户", "Owner-password-123", "站长")
    users.reading_notes.save(
        owner_id,
        {
            **_payload(title="手工创建的同名草稿"),
            "slug": "mark-minervini-breakout-trading",
        },
    )

    preview = migrate_static_reading_articles(
        repository=users,
        username="站长用户",
        apply=False,
        backup=None,
    )

    assert preview["status"] == "blocked"
    assert preview["summary"]["CONFLICT"] == 1
    assert len(users.reading_notes.list_notes(owner_id)) == 1
