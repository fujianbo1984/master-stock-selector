from fastapi.testclient import TestClient

from master_stock_selector.web.app import create_app
from master_stock_selector.web.users import UserRepository

ARTICLE_PATH = "/a/reading/trading-system-boundaries"
ADAM_ARTICLE_PATH = "/a/reading/adam-grimes-trading-templates"


def _app(tmp_path):
    UserRepository(tmp_path / "users.sqlite3").migrate_schema()
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=tmp_path / "master_watchlist.sqlite3",
        secure_cookies=False,
    )
    app.state.user_repository.create_user(
        "reader",
        "Reader-password-123",
        "研读用户",
    )
    return app


def test_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(ARTICLE_PATH, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_article_from_secondary_account_nav(tmp_path):
    client = TestClient(_app(tmp_path))
    login = client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    account = client.get("/account/password")
    assert f'href="{ARTICLE_PATH}"' in account.text
    assert "交易体系随笔" in account.text

    article = client.get(ARTICLE_PATH)
    assert article.status_code == 200
    assert "少学一种形态，多建立一道边界" in article.text
    assert "这不是我的交易" in article.text
    assert "本文记录个人交易研究方向的整理过程" in article.text
    assert 'aria-current="page"' in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


def test_adam_grimes_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(ADAM_ARTICLE_PATH, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={ADAM_ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_adam_grimes_article_with_images(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    account = client.get("/account/password")
    assert f'href="{ADAM_ARTICLE_PATH}"' in account.text
    assert "交易模板图解" in account.text

    article = client.get(ADAM_ARTICLE_PATH)
    assert article.status_code == 200
    assert "八张示意图读懂Adam Grimes的交易模板" in article.text
    assert "失败测试" in article.text
    assert "Anti" in article.text
    assert "/static/reading/adam-trading-templates/00103.jpg" in article.text
    assert article.text.count('<figure class="reading-figure">') == 8
    assert 'aria-current="page"' in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"

    image = client.get("/static/reading/adam-trading-templates/00103.jpg")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
