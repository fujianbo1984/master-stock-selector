from fastapi.testclient import TestClient

from master_stock_selector.web.app import create_app
from master_stock_selector.web.users import UserRepository

ARTICLE_PATH = "/a/reading/trading-system-boundaries"
ADAM_ARTICLE_PATH = "/a/reading/adam-grimes-trading-templates"
STOP_LOSS_ARTICLE_PATH = "/a/reading/stop-loss-is-not-a-percentage"
MOVING_AVERAGE_ARTICLE_PATH = "/a/reading/moving-averages-are-not-trading-buttons"
PULLBACK_ARTICLE_PATH = "/a/reading/adam-grimes-pullback-trading"


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


def test_logged_in_user_can_read_article_without_account_nav_entry(tmp_path):
    client = TestClient(_app(tmp_path))
    login = client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    account = client.get("/account/password")
    assert f'href="{ARTICLE_PATH}"' not in account.text
    assert "交易体系随笔" not in account.text

    article = client.get(ARTICLE_PATH)
    assert article.status_code == 200
    assert 'class="account-settings-sidebar"' not in article.text
    assert "少学一种形态，多建立一道边界" in article.text
    assert "这不是我的交易" in article.text
    assert "本文记录个人交易研究方向的整理过程" in article.text
    assert 'aria-current="page"' not in article.text
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
    assert f'href="{ADAM_ARTICLE_PATH}"' not in account.text
    assert "交易模板图解" not in account.text

    article = client.get(ADAM_ARTICLE_PATH)
    assert article.status_code == 200
    assert 'class="account-settings-sidebar"' not in article.text
    assert "八张示意图读懂Adam Grimes的交易模板" in article.text
    assert "失败测试" in article.text
    assert "Anti" in article.text
    assert "/static/reading/adam-trading-templates/00103.jpg" in article.text
    assert article.text.count('<figure class="reading-figure">') == 8
    assert 'aria-current="page"' not in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"

    image = client.get("/static/reading/adam-trading-templates/00103.jpg")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"


def test_stop_loss_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(
        STOP_LOSS_ARTICLE_PATH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={STOP_LOSS_ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_stop_loss_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(STOP_LOSS_ARTICLE_PATH)
    assert article.status_code == 200
    assert "止损不是一个百分比" in article.text
    assert "Minervini" in article.text
    assert "结构失效位决定止损价格" in article.text
    assert "本文是对交易书籍的个人研究与体系整理" in article.text
    assert article.text.count("<h2") == 9
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


def test_moving_average_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(
        MOVING_AVERAGE_ARTICLE_PATH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={MOVING_AVERAGE_ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_moving_average_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(MOVING_AVERAGE_ARTICLE_PATH)
    assert article.status_code == 200
    assert "均线不是买卖按钮" in article.text
    assert "长期均线决定股票有没有资格" in article.text
    assert "本文是对相关交易书籍与均线方法的分析整理" in article.text
    assert article.text.count("<h2") == 9
    assert f'href="{STOP_LOSS_ARTICLE_PATH}"' in article.text
    assert "上一篇" in article.text
    assert f'href="{PULLBACK_ARTICLE_PATH}"' in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


def test_article_navigation_follows_publication_dates(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    oldest = client.get(ARTICLE_PATH)
    assert "已经是第一篇" in oldest.text
    assert f'href="{ADAM_ARTICLE_PATH}"' in oldest.text

    middle = client.get(ADAM_ARTICLE_PATH)
    assert f'href="{ARTICLE_PATH}"' in middle.text
    assert f'href="{STOP_LOSS_ARTICLE_PATH}"' in middle.text

    stop_loss = client.get(STOP_LOSS_ARTICLE_PATH)
    assert f'href="{ADAM_ARTICLE_PATH}"' in stop_loss.text
    assert f'href="{MOVING_AVERAGE_ARTICLE_PATH}"' in stop_loss.text

    moving_average = client.get(MOVING_AVERAGE_ARTICLE_PATH)
    assert f'href="{STOP_LOSS_ARTICLE_PATH}"' in moving_average.text
    assert f'href="{PULLBACK_ARTICLE_PATH}"' in moving_average.text

    newest = client.get(PULLBACK_ARTICLE_PATH)
    assert f'href="{MOVING_AVERAGE_ARTICLE_PATH}"' in newest.text
    assert "已经是最新一篇" in newest.text


def test_adam_grimes_pullback_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(
        PULLBACK_ARTICLE_PATH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={PULLBACK_ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_adam_grimes_pullback_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(PULLBACK_ARTICLE_PATH)
    assert article.status_code == 200
    assert "Adam Grimes 如何交易回调" in article.text
    assert "先找到一段真实而非高潮式的推动" in article.text
    assert "允许承担的单笔风险" in article.text
    assert "风险提示" in article.text
    assert article.text.count("<h2") == 11
    assert f'href="{MOVING_AVERAGE_ARTICLE_PATH}"' in article.text
    assert "已经是最新一篇" in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"
