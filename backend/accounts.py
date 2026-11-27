"""
公众号管理模块
搜索、收藏、管理公众号列表
完全使用微信读书官方检索与元数据接口实现
"""

import re
import html
import time
import base64
import threading
import urllib.parse
import requests as req
from flask import Blueprint, jsonify, request

from backend.config import (
    ACCOUNTS_FILE, CONFIG_FILE, load_json, save_json,
    get_proxies_dict, get_default_wechat_ua
)
from backend.account_pool import borrow_weread_session, account_pool, get_weread_session

accounts_bp = Blueprint("accounts", __name__, url_prefix="/api/accounts")


def biz_to_book_id(fakeid_or_biz: str) -> str:
    """将微信 Base64 __biz / fakeid 转换为微信读书官方 bookId (MP_WXS_{uin})"""
    if not fakeid_or_biz:
        return ""
    fid = str(fakeid_or_biz).strip()
    if fid.startswith("MP_"):
        return fid
    raw = urllib.parse.unquote(fid).strip()
    missing_padding = len(raw) % 4
    if missing_padding:
        raw += '=' * (4 - missing_padding)
    try:
        decoded = base64.b64decode(raw).decode('utf-8', errors='ignore').strip()
        if decoded.isdigit():
            return f"MP_WXS_{decoded}"
    except Exception:
        pass
    if fid.isdigit():
        return f"MP_WXS_{fid}"
    return fid


def book_id_to_biz(book_id: str) -> str:
    """将微信读书 MP_WXS_{uin} 转换回 Base64 __biz"""
    if not book_id:
        return ""
    bid = str(book_id).strip()
    if bid.startswith("MP_WXS_"):
        uin = bid[len("MP_WXS_"):]
        if uin.isdigit():
            return base64.b64encode(uin.encode("utf-8")).decode("ascii")
    return bid


def _load_accounts() -> list:
    """加载已收藏的公众号列表"""
    raw_accounts = load_json(ACCOUNTS_FILE, [])
    if not isinstance(raw_accounts, list):
        return []
    # 保证每条账号都有标准化 bookId
    updated = False
    for a in raw_accounts:
        if isinstance(a, dict):
            fid = a.get("fakeid", "")
            if not a.get("bookId"):
                a["bookId"] = biz_to_book_id(fid)
                updated = True
    if updated:
        save_json(ACCOUNTS_FILE, raw_accounts)
    return raw_accounts


def _save_accounts(accounts: list):
    """保存公众号列表"""
    save_json(ACCOUNTS_FILE, accounts)


@accounts_bp.route("", methods=["GET"])
def list_accounts():
    """获取已收藏的公众号列表"""
    accounts = _load_accounts()
    return jsonify({"accounts": accounts, "total": len(accounts)})


@accounts_bp.route("/search", methods=["POST"])
def search_accounts():
    """
    搜索公众号：
    1. 若输入为文章 URL 或 biz，通过解析/WeRead接口直达提取；
    2. 若输入为公众号名称/关键字，通过微信读书官方公众号专属检索 (scope=2) 提取。
    """
    data = request.get_json() or {}
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "请输入搜索关键字或公众号文章链接"}), 400

    try:
        acc_id, vid, token, headers = borrow_weread_session()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    results = []
    seen_ids = set()

    s = get_weread_session()
    from backend.weread_signer import (
        sanitize_apk_headers,
        pace_weread_request,
        get_rate_limit_status,
        mark_rate_limited,
        activate_terminal_session,
    )
    from backend.waterwall import detect_waterwall, waterwall_hit, record_failure, is_waterwall_active, waterwall_snapshot
    headers = sanitize_apk_headers(headers)

    # ── 模式 1：链接 / biz 解析 ──────────────────────────
    if keyword.startswith("http://") or keyword.startswith("https://") or keyword.startswith("biz:"):
        clean_url = keyword.replace("biz:", "").strip()
        book_id = None
        fakeid = None

        # 尝试从 URL 中直接提取 __biz
        parsed = urllib.parse.urlparse(clean_url)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("__biz") and qs.get("__biz")[0]:
            fakeid = qs.get("__biz")[0].strip()
            book_id = biz_to_book_id(fakeid)

        # 若未从 URL 提取到，调用微信读书 /mp/getreviewid
        if not book_id:
            try:
                res = s.post(
                    "https://i.weread.qq.com/mp/getreviewid",
                    json={"urls": [clean_url]},
                    headers=headers,
                    timeout=10,
                ).json()
                book_id = res.get("bookId")
                if not book_id and res.get("reviewIds"):
                    book_id = res["reviewIds"][0].get("bookId")
            except Exception:
                pass

        # 若仍未得到，尝试请求文章网页源码提取 __biz
        if not book_id:
            try:
                page_resp = req.get(clean_url, headers={"User-Agent": get_default_wechat_ua()}, timeout=10)
                m = (re.search(r'__biz=([A-Za-z0-9+/=]{10,})', page_resp.text) or
                     re.search(r'var\s+biz\s*=\s*"([A-Za-z0-9+/=]{10,})"', page_resp.text))
                if m:
                    fakeid = m.group(1).strip()
                    book_id = biz_to_book_id(fakeid)
            except Exception:
                pass

        if not book_id:
            return jsonify({"error": "未能从该链接解析出微信公众号信息，请确认链接是否有效"}), 400

        # 获取该公众号详情元数据
        try:
            info_resp = s.get(f"https://i.weread.qq.com/book/info?bookId={book_id}", headers=headers, timeout=10).json()
            title = info_resp.get("title") or "微信公众号"
            cover = info_resp.get("cover") or ""
            intro = info_resp.get("intro") or ""
            author = info_resp.get("author") or "公众号"
            fid = fakeid or book_id_to_biz(book_id) or book_id
            return jsonify({
                "results": [{
                    "fakeid": fid,
                    "bookId": book_id,
                    "nickname": title,
                    "alias": fid,
                    "round_head_img": cover,
                    "signature": intro,
                    "service_type": 1,
                }]
            })
        except Exception as e:
            return jsonify({"error": f"获取公众号元数据失败: {e}"}), 500

    # ── 模式 2：名称关键字搜索 (微信读书官方 scope=2 检索) ──
    try:
        throttled_now, left_now = get_rate_limit_status()
        if throttled_now:
            return jsonify({
                "results": [],
                "total": 0,
                "throttled": True,
                "errcode": -2014,
                "cooldown_left": left_now,
                "error": f"触发频控冷却中，请 {left_now} 秒后再试",
            })

        encoded_kw = urllib.parse.quote(keyword)
        url_mp = f"https://i.weread.qq.com/store/search?keyword={encoded_kw}&scope=2&v=2&count=20"

        # 智能重试循环 (支持 401 单飞自动续期、-2014 冷却与 -2041/-2012 处理)
        resp = None
        error_msg = ""
        for attempt in range(3):
            if is_waterwall_active():
                error_msg = "触发微信读书防水墙风控，工具已暂停请求，请在页面完成滑块验证后重试"
                break
            throttled_loop, left_loop = get_rate_limit_status()
            if throttled_loop:
                error_msg = f"触发频控冷却中，请 {left_loop} 秒后再试"
                break
            pace_weread_request(url_mp)
            resp = s.get(url_mp, headers=headers, timeout=10)
            ww = detect_waterwall(resp.status_code, resp.text)
            if ww:
                record_failure(url_mp, resp.status_code, resp.headers, resp.text, ww)
                waterwall_hit(url_mp, resp.status_code, ww)
                error_msg = "触发微信读书防水墙风控，工具已暂停请求，请在页面完成滑块验证后重试"
                break
            if resp.status_code == 200:
                try:
                    j_ok = resp.json()
                    if isinstance(j_ok, dict) and j_ok.get("errcode") == -2014:
                        cd = mark_rate_limited(url_mp)
                        record_failure(url_mp, resp.status_code, resp.headers, resp.text, "-2014")
                        account_pool.report(acc_id, status_code=200, ret=-2014, error_msg=f"频控 -2014，冷却 {cd}s")
                        error_msg = f"触发频控冷却中，请 {cd} 秒后再试"
                        resp = None
                        break
                except Exception:
                    pass
                break
            elif resp.status_code == 401:
                account_pool.report(acc_id, status_code=401, expected_access_token=token)
                acc_id, vid, token, headers = borrow_weread_session()
                headers = sanitize_apk_headers(headers)
            elif resp.status_code in (429, 499):
                err_text = resp.text
                if "-2014" in err_text or resp.status_code == 429:
                    cd = mark_rate_limited(url_mp)
                    record_failure(url_mp, resp.status_code, resp.headers, resp.text, "-2014")
                    account_pool.report(acc_id, status_code=resp.status_code, ret=-2014, error_msg=f"频控 -2014，冷却 {cd}s")
                    error_msg = f"触发频控冷却中，请 {cd} 秒后再试"
                    break
                elif "-2012" in err_text:
                    time.sleep(0.5)
                    continue
                elif "-2041" in err_text:
                    if activate_terminal_session(headers, session=s):
                        continue
                    record_failure(url_mp, resp.status_code, resp.headers, resp.text, "-2041")
                    waterwall_hit(url_mp, resp.status_code, "-2041")
                    error_msg = "触发微信读书风控（-2041），请在页面完成滑块验证后重试"
                    break
                else:
                    error_msg = f"微信读书接口异常（HTTP {resp.status_code}）：{resp.text[:100]}"
                    break
            else:
                error_msg = f"微信读书接口异常（HTTP {resp.status_code}）：{resp.text[:100]}"
                break

        if resp and resp.status_code == 200:
            books = resp.json().get("books", [])
            for b in books:
                b_info = b.get("bookInfo", {})
                b_id = b_info.get("bookId", "")
                if not b_id or b_id in seen_ids:
                    continue
                seen_ids.add(b_id)
                fid = book_id_to_biz(b_id) or b_id
                results.append({
                    "fakeid": fid,
                    "bookId": b_id,
                    "nickname": b_info.get("title", ""),
                    "alias": fid,
                    "round_head_img": b_info.get("cover", ""),
                    "signature": b_info.get("intro", ""),
                    "service_type": 1,
                })

        # 若 scope=2 无结果且未处于防水墙/频控熔断，拉取通用搜索作为补充
        if not results and not is_waterwall_active() and not get_rate_limit_status()[0]:
            url_gen = f"https://i.weread.qq.com/store/search?keyword={encoded_kw}&v=2&count=20"
            pace_weread_request(url_gen)
            resp_gen = s.get(url_gen, headers=headers, timeout=10)
            if resp_gen.status_code == 200:
                for b in resp_gen.json().get("books", []):
                    b_info = b.get("bookInfo", {})
                    b_id = b_info.get("bookId", "")
                    is_mp = b_id.startswith("MP_") or b_info.get("type") == 3 or b_info.get("author") == "公众号"
                    if is_mp and b_id not in seen_ids:
                        seen_ids.add(b_id)
                        fid = book_id_to_biz(b_id) or b_id
                        results.append({
                            "fakeid": fid,
                            "bookId": b_id,
                            "nickname": b_info.get("title", ""),
                            "alias": fid,
                            "round_head_img": b_info.get("cover", ""),
                            "signature": b_info.get("intro", ""),
                            "service_type": 1,
                        })
            elif not error_msg:
                error_msg = f"微信读书接口异常（HTTP {resp_gen.status_code}）：{resp_gen.text[:100]}"

        payload = {"results": results, "total": len(results)}
        if error_msg and not results:
            payload["error"] = error_msg
            # 防水墙激活时附带状态快照，前端 api.js 检测到 need_verify 会自动拉起滑块验证
            if is_waterwall_active():
                payload["need_verify"] = True
                payload["waterwall"] = waterwall_snapshot()
        return jsonify(payload)

    except Exception as e:
        return jsonify({"error": f"搜索公众号失败: {str(e)}"}), 500


@accounts_bp.route("", methods=["POST"])
def add_account():
    """收藏/添加公众号"""
    data = request.get_json() or {}
    fakeid = data.get("fakeid", "").strip()
    book_id = data.get("bookId", "").strip() or biz_to_book_id(fakeid)
    nickname = data.get("nickname", "").strip()

    if not fakeid and not book_id:
        return jsonify({"error": "缺少公众号标识 (fakeid 或 bookId)"}), 400
    if not nickname:
        return jsonify({"error": "缺少公众号名称"}), 400

    if not fakeid:
        fakeid = book_id_to_biz(book_id) or book_id

    accounts = _load_accounts()

    # 查重：避免重复添加
    for a in accounts:
        if a.get("fakeid") == fakeid or (book_id and a.get("bookId") == book_id):
            return jsonify({"message": "该公众号已在收藏列表中", "account": a})

    new_account = {
        "fakeid": fakeid,
        "bookId": book_id,
        "nickname": nickname,
        "alias": data.get("alias", fakeid),
        "round_head_img": data.get("round_head_img", ""),
        "signature": data.get("signature", ""),
        "service_type": data.get("service_type", 1),
        "added_time": time.time(),
    }

    accounts.insert(0, new_account)
    _save_accounts(accounts)

    return jsonify({"message": "添加成功", "account": new_account})


@accounts_bp.route("/<fakeid>", methods=["DELETE"])
def remove_account(fakeid):
    """取消收藏公众号（联动停止其 RSS 定时抓取，避免留下幽灵订阅）"""
    accounts = _load_accounts()
    target_book_id = biz_to_book_id(fakeid)

    before_count = len(accounts)
    accounts = [a for a in accounts if a.get("fakeid") != fakeid and a.get("bookId") != target_book_id]

    if len(accounts) == before_count:
        return jsonify({"error": "未找到该公众号"}), 404

    _save_accounts(accounts)

    rss_unsubscribed = False
    try:
        from backend.rss_scheduler import rss_scheduler
        rss_unsubscribed = rss_scheduler.remove_subscription(fakeid)
    except Exception:
        pass

    return jsonify({"message": "已取消收藏", "rss_unsubscribed": rss_unsubscribed})


@accounts_bp.route("/<fakeid>", methods=["PUT"])
def update_account(fakeid):
    """更新公众号备注信息"""
    data = request.get_json() or {}
    accounts = _load_accounts()
    target_book_id = biz_to_book_id(fakeid)

    target = None
    for a in accounts:
        if a.get("fakeid") == fakeid or a.get("bookId") == target_book_id:
            target = a
            break

    if not target:
        return jsonify({"error": "未找到该公众号"}), 404

    if "nickname" in data:
        target["nickname"] = data["nickname"]
    if "signature" in data:
        target["signature"] = data["signature"]

    _save_accounts(accounts)
    return jsonify({"message": "更新成功", "account": target})


_USER_MPS_CACHE: dict[str, dict] = {}
_USER_MPS_LOCK = threading.Lock()


def _get_cached_user_mps(vid: str) -> list | None:
    key = str(vid or "")
    now = time.time()
    with _USER_MPS_LOCK:
        item = _USER_MPS_CACHE.get(key)
        if not item:
            return None
        if now >= item.get("expire", 0):
            _USER_MPS_CACHE.pop(key, None)
            return None
        return list(item["mps"])


def _set_cached_user_mps(vid: str, mps: list, ttl: int = 120):
    key = str(vid or "")
    with _USER_MPS_LOCK:
        _USER_MPS_CACHE[key] = {"expire": time.time() + ttl, "mps": list(mps)}


def invalidate_user_mps_cache(vid: str = None):
    with _USER_MPS_LOCK:
        if vid is None:
            _USER_MPS_CACHE.clear()
        else:
            _USER_MPS_CACHE.pop(str(vid), None)


@accounts_bp.route("/user-mps", methods=["GET"])
def get_user_subscribed_mps():
    """获取当前登录微信读书用户已关注/书架中的公众号列表 (带内存缓存与 -2014 短路保护)"""
    from backend.weread_signer import (
        sanitize_apk_headers,
        pace_weread_request,
        get_rate_limit_status,
        mark_rate_limited,
        activate_terminal_session,
    )
    from backend.waterwall import detect_waterwall, waterwall_hit, record_failure, waterwall_snapshot, is_waterwall_active

    force = request.args.get("force", "0") in ("1", "true", "yes")
    try:
        acc_id, vid, token, headers = borrow_weread_session()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    if not force:
        cached = _get_cached_user_mps(vid)
        if cached is not None:
            resp_data = {"mps": cached, "total": len(cached), "cached": True}
            snap = waterwall_snapshot()
            if is_waterwall_active() or snap.get("active"):
                resp_data["need_verify"] = True
                resp_data["waterwall"] = snap
            return jsonify(resp_data)

    headers = sanitize_apk_headers(headers)
    s = get_weread_session()
    mps = []
    seen_ids = set()
    throttled = False
    cooldown_left = 0

    throttled_now, left_now = get_rate_limit_status()
    if throttled_now:
        return jsonify({
            "mps": [],
            "total": 0,
            "cached": False,
            "throttled": True,
            "errcode": -2014,
            "cooldown_left": left_now,
            "error": f"触发频控冷却中，请 {left_now} 秒后再试",
        })

    def _safe_get(u):
        nonlocal acc_id, vid, token, headers, throttled, cooldown_left
        for att in range(2):
            if throttled or is_waterwall_active():
                return None
            try:
                pace_weread_request(u)
                r = s.get(u, headers=headers, timeout=10)
                # 优先检测防水墙
                ww = detect_waterwall(r.status_code, r.text)
                if ww:
                    record_failure(u, r.status_code, r.headers, r.text, ww)
                    waterwall_hit(u, r.status_code, ww)
                    return None

                if r.status_code == 401:
                    account_pool.report(acc_id, status_code=401, expected_access_token=token)
                    invalidate_user_mps_cache(vid)
                    acc_id, vid, token, headers = borrow_weread_session()
                    headers = sanitize_apk_headers(headers)
                    time.sleep(0.5)
                    continue

                if r.status_code in (429, 499):
                    # -2014 频控: 对齐官方策略，登记冷却窗口，不盲目重放且不误弹防水墙
                    if "-2014" in r.text or r.status_code == 429:
                        throttled = True
                        cooldown_left = mark_rate_limited(u)
                        record_failure(u, r.status_code, r.headers, r.text, "-2014")
                        account_pool.report(acc_id, status_code=r.status_code, ret=-2014, error_msg=f"触发频控 (-2014)，冷却 {cooldown_left}s")
                        return None
                    # -2012 超时: 0.5s 重试
                    elif "-2012" in r.text:
                        time.sleep(0.5)
                        try:
                            pace_weread_request(u)
                            r_to = s.get(u, headers=headers, timeout=10)
                            if r_to.status_code == 200:
                                return r_to
                        except Exception:
                            pass
                        return None
                    # -2041 在线态激活
                    elif "-2041" in r.text:
                        activate_terminal_session(headers, session=s)
                        try:
                            pace_weread_request(u)
                            r_sync = s.get(u, headers=headers, timeout=10)
                            if r_sync.status_code == 200:
                                return r_sync
                        except Exception:
                            pass
                        record_failure(u, r.status_code, r.headers, r.text, "-2041")
                        waterwall_hit(u, r.status_code, "-2041")
                        return None

                if r.status_code == 200:
                    try:
                        j = r.json()
                        if isinstance(j, dict) and j.get("errcode") == -2014:
                            throttled = True
                            cooldown_left = mark_rate_limited(u)
                            record_failure(u, r.status_code, r.headers, r.text, "-2014")
                            account_pool.report(acc_id, status_code=200, ret=-2014, error_msg=f"触发频控 (-2014)，冷却 {cooldown_left}s")
                            return None
                    except Exception:
                        pass
                    return r
                return None
            except Exception as ex:
                logger.debug("[user-mps] 请求 %s 异常: %s", u, ex)
                time.sleep(0.5)
        return None

    # 1. 微信读书官方 /mp/list (listType=0, 1) 必须带 synckey=0
    for lt in (0, 1):
        if throttled or is_waterwall_active():
            break
        r = _safe_get(f"https://i.weread.qq.com/mp/list?listType={lt}&count=50&synckey=0")
        if r:
            items = r.json().get("lists", []) or r.json().get("items", []) or []
            for it in items:
                b_info = it.get("bookInfo", it)
                bid = b_info.get("bookId") or it.get("bookId") or ""
                if not bid or bid in seen_ids:
                    continue
                seen_ids.add(bid)
                title = it.get("account") or b_info.get("title") or it.get("title") or ""
                cover = it.get("mpavatar") or it.get("thumbUrl") or b_info.get("cover") or ""
                intro = it.get("title", "")
                fid = book_id_to_biz(bid) or bid
                mps.append({
                    "fakeid": fid,
                    "bookId": bid,
                    "nickname": title,
                    "alias": fid,
                    "round_head_img": cover,
                    "signature": f"最新：{intro}" if intro and intro != title else "",
                    "service_type": 1,
                })

    # 2. 从官方书架接口 /shelf/sync 补充已加入书架的公众号 (对齐官方 BaseShelfService 参数)
    if not throttled and not is_waterwall_active():
        r_shelf = _safe_get("https://i.weread.qq.com/shelf/sync?synckey=0&teenmode=0&album=1&onlyBookid=0&localBookCount=0")
        if r_shelf:
            for b in r_shelf.json().get("books", []):
                bid = b.get("bookId", "")
                if (bid.startswith("MP_") or "MP_WXS" in bid) and bid not in seen_ids:
                    seen_ids.add(bid)
                    title = b.get("title", "")
                    cover = b.get("cover", "")
                    intro = b.get("intro", "")
                    fid = book_id_to_biz(bid) or bid
                    mps.append({
                        "fakeid": fid,
                        "bookId": bid,
                        "nickname": title,
                        "alias": fid,
                        "round_head_img": cover,
                        "signature": intro,
                        "service_type": 1,
                    })

    # 已移除非官方客户端接口 /subscription/books，避免额外风控请求
    if mps and not throttled and not is_waterwall_active():
        _set_cached_user_mps(vid, mps, ttl=120)

    snap = waterwall_snapshot()
    resp_data = {"mps": mps, "total": len(mps), "cached": False}
    if throttled:
        resp_data["throttled"] = True
        resp_data["errcode"] = -2014
        resp_data["cooldown_left"] = cooldown_left
        resp_data["error"] = f"触发微信读书频控 (-2014)，请 {cooldown_left} 秒后重试"
    if is_waterwall_active() or snap.get("active"):
        resp_data["need_verify"] = True
        resp_data["waterwall"] = snap
    return jsonify(resp_data)


# ── RSS 订阅路由保持向后兼容 ──────────────────────────

@accounts_bp.route("/<fakeid>/rss-subscribe", methods=["POST"])
def rss_subscribe(fakeid):
    from backend.rss_scheduler import rss_scheduler
    data = request.get_json(silent=True) or {}
    interval = data.get("interval_minutes", 60)
    sub = rss_scheduler.add_subscription(fakeid, interval_minutes=interval)
    if not sub:
        return jsonify({"error": "添加订阅失败，请确认公众号已收藏"}), 400
    return jsonify({"message": "已启用 RSS 自动抓取", "subscription": sub})


@accounts_bp.route("/<fakeid>/rss-subscribe", methods=["DELETE"])
def rss_unsubscribe(fakeid):
    from backend.rss_scheduler import rss_scheduler
    ok = rss_scheduler.remove_subscription(fakeid)
    if not ok:
        return jsonify({"error": "未找到该订阅"}), 404
    return jsonify({"message": "已停用 RSS 自动抓取"})


@accounts_bp.route("/rss-subscriptions", methods=["GET"])
def rss_subscriptions():
    from backend.rss_scheduler import rss_scheduler
    return jsonify(rss_scheduler.list_subscriptions())


@accounts_bp.route("/<fakeid>/rss-force-upload", methods=["POST"])
def rss_force_upload(fakeid):
    from backend.rss_scheduler import rss_scheduler
    res = rss_scheduler.force_upload_account(fakeid)
    return jsonify(res)


@accounts_bp.route("/rss-upload-log", methods=["GET"])
def rss_upload_log():
    from backend.rss_scheduler import rss_scheduler
    account = request.args.get("account")
    limit = request.args.get("limit", 30, type=int)
    return jsonify({"logs": rss_scheduler.get_upload_logs(account=account, limit=limit)})


# ---------------------------------------------------------------- 导出 Excel

_XLSX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>'
)

_XLSX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_XLSX_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="公众号RSS" sheetId="1" r:id="rId1"/></sheets>'
    '</workbook>'
)

_XLSX_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '</Relationships>'
)


def _xlsx_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{html.escape(str(value), quote=False)}</t></is></c>'


def _build_accounts_xlsx(rows: list) -> bytes:
    """用标准库 zipfile 拼一个最小 xlsx（inlineStr，无 sharedStrings）。"""
    import io
    import zipfile

    xml_rows = []
    for idx, (name, rss_url) in enumerate(rows, start=1):
        xml_rows.append(
            f'<row r="{idx}">{_xlsx_cell(f"A{idx}", name)}{_xlsx_cell(f"B{idx}", rss_url)}</row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols><col min="1" max="1" width="32" customWidth="1"/>'
        '<col min="2" max="2" width="90" customWidth="1"/></cols>'
        f'<sheetData>{"".join(xml_rows)}</sheetData>'
        '</worksheet>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _XLSX_RELS)
        zf.writestr("xl/workbook.xml", _XLSX_WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", _XLSX_WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


@accounts_bp.route("/export-excel", methods=["GET"])
def export_accounts_excel():
    """导出已收藏公众号为 Excel：公众号名称 + RSS 地址两列。"""
    from flask import Response

    from backend.config import get_settings
    from backend.rss_feed import _cfg, _resolve_base_url

    settings = get_settings()
    base_url = _resolve_base_url(request, settings)
    token = str(_cfg(settings, "rss_token", "RSS_TOKEN", "") or "").strip()
    token_query = f"?token={urllib.parse.quote(token)}" if token else ""

    rows = [("公众号名称", "RSS地址")]
    for acc in _load_accounts():
        nickname = str(acc.get("nickname") or "").strip() if isinstance(acc, dict) else ""
        if not nickname:
            continue
        rss_url = f"{base_url}api/articles/rss/{urllib.parse.quote(nickname)}{token_query}"
        rows.append((nickname, rss_url))

    data = _build_accounts_xlsx(rows)
    filename = f"公众号RSS订阅_{time.strftime('%Y%m%d')}.xlsx"
    disposition = (
        f"attachment; filename=accounts_rss.xlsx; "
        f"filename*=UTF-8''{urllib.parse.quote(filename)}"
    )
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )
