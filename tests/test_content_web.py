from fastapi.testclient import TestClient

from master_stock_selector.web.app import create_app
from master_stock_selector.web.users import UserRepository

ARTICLE_PATH = "/a/reading/trading-system-boundaries"
ADAM_ARTICLE_PATH = "/a/reading/adam-grimes-trading-templates"
STOP_LOSS_ARTICLE_PATH = "/a/reading/stop-loss-is-not-a-percentage"
MOVING_AVERAGE_ARTICLE_PATH = "/a/reading/moving-averages-are-not-trading-buttons"
PULLBACK_ARTICLE_PATH = "/a/reading/adam-grimes-pullback-trading"
BROOKS_PULLBACK_ARTICLE_PATH = "/a/reading/al-brooks-pullback-trading"
COMBINED_PULLBACK_ARTICLE_PATH = "/a/reading/adam-and-brooks-pullback-execution"
SUPPORT_RESISTANCE_ARTICLE_PATH = "/a/reading/support-resistance-price-boundaries"
BROOKS_BREAKOUT_ARTICLE_PATH = "/a/reading/al-brooks-breakout-trading"
ADAM_BREAKOUT_ARTICLE_PATH = "/a/reading/adam-grimes-breakout-trading"
READING_ARCHIVE_PATH = "/a/reading"


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


def test_logged_in_user_can_read_support_resistance_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(SUPPORT_RESISTANCE_ARTICLE_PATH)
    assert article.status_code == 200
    assert "支撑与阻力不是一条线" in article.text
    assert "Weinstein" in article.text
    assert "支撑与阻力是在特定时间框架和市场状态下" in article.text
    assert article.text.count("<h2") == 10
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


def test_logged_in_user_can_read_al_brooks_breakout_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(BROOKS_BREAKOUT_ARTICLE_PATH)
    assert article.status_code == 200
    assert "突破不是穿过一条线" in article.text
    assert "突破可以失败；失败突破也可以失败" in article.text
    assert "把breakout先理解成“突破尝试”" in article.text
    assert article.text.count("<h2") == 9
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


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
    image_path = f"{ADAM_ARTICLE_PATH}/assets/images/00103.jpg"
    assert image_path in article.text
    assert article.text.count('<figure class="reading-figure">') == 8
    assert 'aria-current="page"' not in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"

    image = client.get(image_path)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == "private, no-store"


def test_reading_archive_requires_login_and_lists_all_articles(tmp_path):
    client = TestClient(_app(tmp_path))
    anonymous = client.get(READING_ARCHIVE_PATH, follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == f"/login?next={READING_ARCHIVE_PATH}"

    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303
    archive = client.get(READING_ARCHIVE_PATH)
    assert archive.status_code == 200
    assert archive.text.count('class="reading-tags"') == 10
    assert f'href="{ADAM_BREAKOUT_ARTICLE_PATH}"' in archive.text
    assert f'href="{BROOKS_BREAKOUT_ARTICLE_PATH}"' in archive.text
    assert f'href="{SUPPORT_RESISTANCE_ARTICLE_PATH}"' in archive.text
    assert f'href="{ARTICLE_PATH}"' in archive.text
    assert archive.headers["cache-control"] == "private, no-store"
    assert archive.headers["x-robots-tag"] == "noindex, nofollow"


def test_reading_asset_rejects_missing_article_and_path_escape(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    missing = client.get("/a/reading/not-an-article/assets/image.jpg")
    assert missing.status_code == 404
    escaped = client.get(f"{ADAM_ARTICLE_PATH}/assets/../../../../owner.py")
    assert escaped.status_code == 404


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
    assert f'href="{BROOKS_PULLBACK_ARTICLE_PATH}"' in newest.text

    newest = client.get(BROOKS_PULLBACK_ARTICLE_PATH)
    assert f'href="{PULLBACK_ARTICLE_PATH}"' in newest.text
    assert f'href="{COMBINED_PULLBACK_ARTICLE_PATH}"' in newest.text

    newest = client.get(COMBINED_PULLBACK_ARTICLE_PATH)
    assert f'href="{BROOKS_PULLBACK_ARTICLE_PATH}"' in newest.text
    assert f'href="{SUPPORT_RESISTANCE_ARTICLE_PATH}"' in newest.text

    newest = client.get(SUPPORT_RESISTANCE_ARTICLE_PATH)
    assert f'href="{COMBINED_PULLBACK_ARTICLE_PATH}"' in newest.text
    assert f'href="{BROOKS_BREAKOUT_ARTICLE_PATH}"' in newest.text

    newest = client.get(BROOKS_BREAKOUT_ARTICLE_PATH)
    assert f'href="{SUPPORT_RESISTANCE_ARTICLE_PATH}"' in newest.text
    assert f'href="{ADAM_BREAKOUT_ARTICLE_PATH}"' in newest.text

    newest = client.get(ADAM_BREAKOUT_ARTICLE_PATH)
    assert f'href="{BROOKS_BREAKOUT_ARTICLE_PATH}"' in newest.text
    assert "已经是最新一篇" in newest.text
    assert "真正要交易的不是越线，而是失衡" in newest.text
    assert f"{ADAM_BREAKOUT_ARTICLE_PATH}/assets/images/fig-5-1-drys-breakout.jpg" in newest.text


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
    assert f'href="{BROOKS_PULLBACK_ARTICLE_PATH}"' in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


def test_al_brooks_pullback_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(
        BROOKS_PULLBACK_ARTICLE_PATH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={BROOKS_PULLBACK_ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_al_brooks_pullback_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(BROOKS_PULLBACK_ARTICLE_PATH)
    assert article.status_code == 200
    assert "Al Brooks 如何交易回调" in article.text
    assert "先确定市场由哪一方控制" in article.text
    assert "交易者方程" in article.text
    assert "风险提示" in article.text
    assert article.text.count("<h2") == 14
    assert f'href="{PULLBACK_ARTICLE_PATH}"' in article.text
    assert f'href="{COMBINED_PULLBACK_ARTICLE_PATH}"' in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"


def test_combined_pullback_article_requires_login(tmp_path):
    response = TestClient(_app(tmp_path)).get(
        COMBINED_PULLBACK_ARTICLE_PATH,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={COMBINED_PULLBACK_ARTICLE_PATH}"
    assert response.headers["cache-control"] == "private, no-store"


def test_logged_in_user_can_read_combined_pullback_article(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post(
        "/login",
        data={"username": "reader", "password": "Reader-password-123"},
        follow_redirects=False,
    ).status_code == 303

    article = client.get(COMBINED_PULLBACK_ARTICLE_PATH)
    assert article.status_code == 200
    assert "Adam 与 Brooks 的回调交易思路" in article.text
    assert "Adam 擅长筛选回调，Brooks 擅长执行回调" in article.text
    assert "这笔交易为什么值得做，以及现在为什么可以进场" in article.text
    assert f'href="{BROOKS_PULLBACK_ARTICLE_PATH}"' in article.text
    assert f'href="{SUPPORT_RESISTANCE_ARTICLE_PATH}"' in article.text
    assert article.headers["cache-control"] == "private, no-store"
    assert article.headers["x-robots-tag"] == "noindex, nofollow"
