import threading
import time

import pytest

import backend.articles as articles_mod
import backend.config as config_mod
import backend.rss_scheduler as rss_mod
import backend.weread_signer as signer
from backend.rss_scheduler import RssScheduler


class _RateLimitedResponse:
    status_code = 200
    headers = {}
    text = '{"errcode":-2014,"errmsg":"请求频率过高"}'

    def json(self):
        return {"errcode": -2014, "errmsg": "请求频率过高"}


class _Session:
    def get(self, *args, **kwargs):
        return _RateLimitedResponse()


def test_fetch_articles_page_raises_on_weread_rate_limit(monkeypatch):
    signer.clear_rate_limit()
    monkeypatch.setattr(articles_mod, "biz_to_book_id", lambda fakeid: "MP_WXS_123")
    monkeypatch.setattr(articles_mod, "get_weread_session", lambda: _Session())
    monkeypatch.setattr(articles_mod, "borrow_weread_session", lambda: ("acc_1", "vid_1", "tok_1", {}))
    monkeypatch.setattr(articles_mod, "get_cached_chapters", lambda *args, **kwargs: None)
    monkeypatch.setattr(articles_mod.account_pool, "report", lambda *args, **kwargs: None)
    monkeypatch.setattr(signer, "pace_weread_request", lambda url: None)

    try:
        with pytest.raises(signer.WeReadRateLimitError) as exc_info:
            articles_mod._fetch_articles_page("fakeid", begin=0, count=10)

        assert exc_info.value.errcode == -2014
        assert exc_info.value.cooldown_left > 0
    finally:
        signer.clear_rate_limit()


def test_rss_fetch_marks_rate_limited_batch_as_failed(monkeypatch):
    signer.clear_rate_limit()
    sub = {
        "fakeid": "fakeid_1",
        "nickname": "每日经济新闻",
        "last_fetch_count": 3,
        "total_articles": 8,
        "interval_minutes": 360,
    }
    saved_subscriptions = []

    scheduler = RssScheduler.__new__(RssScheduler)
    scheduler._lock = threading.Lock()
    scheduler._history_lock = threading.Lock()
    scheduler.get_articles = lambda nickname: []
    scheduler.get_subscriptions = lambda: [sub]
    scheduler._save_subscriptions = lambda subs: saved_subscriptions.extend(subs)
    scheduler._save_articles = lambda nickname, articles: None

    monkeypatch.setattr(rss_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        articles_mod,
        "_fetch_articles_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            signer.WeReadRateLimitError("触发微信读书频控 (-2014)，冷却 15 秒，本轮抓取失败", cooldown_left=15)
        ),
    )
    monkeypatch.setattr(config_mod, "load_json", lambda path, default=None: default)
    monkeypatch.setattr(config_mod, "save_json", lambda path, data: None)
    monkeypatch.setattr(config_mod, "get_settings", lambda: {"rss_upload_enabled": False})

    before = time.time()
    scheduler._fetch_for_account(sub)
    after = time.time()

    assert sub["last_fetch_count"] == 0
    assert sub["total_articles"] == 8
    assert "触发微信读书频控 (-2014)" in sub["last_error"]
    # 失败后固定 10 分钟重试，忽略 interval_minutes=360 的配置
    assert before + 600 <= sub["next_fetch_time"] <= after + 600
    assert saved_subscriptions
    signer.clear_rate_limit()


def test_rss_fetch_marks_existing_cooldown_as_failed(monkeypatch):
    signer.clear_rate_limit()
    cooldown_left = signer.mark_rate_limited("https://i.weread.qq.com/mp/chapters", cooldown=30)
    sub = {"fakeid": "fakeid_1", "nickname": "每日经济新闻", "interval_minutes": 360}
    saved_subscriptions = []

    scheduler = RssScheduler.__new__(RssScheduler)
    scheduler._lock = threading.Lock()
    scheduler._history_lock = threading.Lock()
    scheduler.get_subscriptions = lambda: [sub]
    scheduler._save_subscriptions = lambda subs: saved_subscriptions.extend(subs)

    monkeypatch.setattr(rss_mod.time, "sleep", lambda seconds: None)

    try:
        before = time.time()
        scheduler._fetch_for_account(sub)
        after = time.time()

        assert sub["last_fetch_count"] == 0
        assert "触发微信读书频控 (-2014)" in sub["last_error"]
        assert str(cooldown_left) in sub["last_error"]
        # 失败后固定 10 分钟重试，忽略 interval_minutes=360 的配置
        assert before + 600 <= sub["next_fetch_time"] <= after + 600
        assert saved_subscriptions
    finally:
        signer.clear_rate_limit()


def test_rss_fetch_success_keeps_configured_interval(monkeypatch):
    signer.clear_rate_limit()
    sub = {"fakeid": "fakeid_1", "nickname": "每日经济新闻", "interval_minutes": 360}

    scheduler = RssScheduler.__new__(RssScheduler)
    scheduler._lock = threading.Lock()
    scheduler._history_lock = threading.Lock()
    scheduler.get_articles = lambda nickname: []
    scheduler.get_subscriptions = lambda: [sub]
    scheduler._save_subscriptions = lambda subs: None
    scheduler._save_articles = lambda nickname, articles: None

    monkeypatch.setattr(rss_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(articles_mod, "_fetch_articles_page", lambda *args, **kwargs: ([], 0, False))
    monkeypatch.setattr(config_mod, "load_json", lambda path, default=None: default)
    monkeypatch.setattr(config_mod, "save_json", lambda path, data: None)
    monkeypatch.setattr(config_mod, "get_settings", lambda: {"rss_upload_enabled": False})

    before = time.time()
    scheduler._fetch_for_account(sub)
    after = time.time()

    assert sub["last_error"] is None
    # 成功时仍按 interval_minutes=360 的 ±25% 抖动区间重排
    assert before + 270 * 60 <= sub["next_fetch_time"] <= after + 450 * 60
