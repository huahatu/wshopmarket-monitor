#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weverse Shop 商品到貨/開賣通知程式（支援同時監控多個商品頁面，含單一商品/無選項商品）
=====================================
"""

import os
import re
import json
import time
import logging
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 基本設定：想追蹤的商品頁面清單
# ------------------------------------------------------------------

load_dotenv()

# 每一個項目：
#   url        -> 商品頁面網址
#   label      -> 顯示名稱
#   products   -> 要追蹤的商品名稱清單（is_single=True 時，放一個你自己取的名字即可）
#   is_single  -> True 代表這是「沒有款式選項、只有一個購買按鈕」的單一商品（僅限 Weverse）
#   site       -> "weverse"（預設，可省略）或 "muji"，決定用哪一套判斷邏輯
MONITORED_PAGES = [
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/3/sales/43782",
        "label": "商品頁 43782",
        "products": [
            "CHOI YONG MEONG",
            "HWANG CHOON",
            "BAMGEUT",
            "DA-GO-NYANG",
            "HHM NYA RING",
        ],
    },
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/3/sales/61334",
        "label": "商品頁 61334",
        "products": [
            "CHOI YONG MEONG",
            "HWANG CHOON",
            "BAMGEUT",
            "DA-GO-NYANG",
            "HHM NYA RING",
        ],
    },
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/255/sales/51616",
        "label": "商品頁 51616（單一商品）",
        "is_single": True,
        "products": ["缽專"],
    },
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/255/sales/51617",
        "label": "商品頁 51617（單一商品）",
        "is_single": True,
        "products": ["球專"],
    },
    {
        "url": "https://shop.weverse.io/zh-tw/shop/KRW/artists/255/sales/59124",
        "label": "商品頁 59124（單一商品）",
        "is_single": True,
        "products": ["綠綠專"],
    },
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/255/sales/63956",
        "label": "商品頁 63956（單一商品）",
        "is_single": True,
        "products": ["手燈"],
    },
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/255/sales/60324",
        "label": "商品頁 60324（單一商品）",
        "is_single": True,
        "products": ["手鍊"],
    },
    {
        "url": "https://www.muji.com/jp/ja/store/cmdty/detail/4550584920738",
        "label": "MUJI 商品 4550584920738",
        "site": "muji",
        "products": ["MUJI 商品"],
    },
]


CHECK_INTERVAL_SECONDS = 15 * 60  # 15 分鐘

TAIWAN_TZ = timezone(timedelta(hours=8))  # 通知訊息裡的時間一律顯示台灣時間（UTC+8）

STATE_FILE = Path(__file__).parent / "state.json"
LOG_FILE = Path(__file__).parent / "monitor.log"

SOLD_OUT_KEYWORDS = ["sold out", "품절", "매진", "售罄", "已售完", "暫無庫存", "缺貨"]
AVAILABLE_KEYWORDS = ["add to cart", "purchase", "buy now", "購買", "加入購物車", "立即購買"]
CONTEXT_WINDOW = 300

# ------------------------------------------------------------------
# 通知管道設定
# ------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
# 如果想讓 MUJI 商品的通知發到「另一個」Discord 頻道，設定這個；沒填就跟其他通知共用同一個頻道
DISCORD_WEBHOOK_URL_MUJI = os.getenv("DISCORD_WEBHOOK_URL_MUJI", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
GMAIL_TO = os.getenv("GMAIL_TO", "").strip()

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("weverse_monitor")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_page_html(url: str, timeout: int = 30, retries: int = 2) -> str:
    """
    抓取網頁原始碼。加了重試機制：第一次失敗（逾時、連線問題等）不會馬上放棄，
    會再等幾秒重試，最多重試 retries 次，減少偶發的網路問題或網站暫時回應慢
    造成誤判成「抓不到」的機率。
    """
    last_exception = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exception = e
            if attempt < retries:
                log.warning(
                    "抓取網頁失敗（第 %d/%d 次嘗試），5 秒後重試：%s",
                    attempt + 1, retries + 1, e,
                )
                time.sleep(5)
    raise last_exception


# ------------------------------------------------------------------
# 判斷各商品的可購買狀態
# ------------------------------------------------------------------

def extract_sale_id_from_url(url: str):
    match = re.search(r"/sales/(\d+)", url)
    return match.group(1) if match else None


def check_availability(html: str, product_names, expected_sale_id=None, is_single=False) -> dict:
    """
    判斷順序：
    1. 按鈕 disabled 屬性（最準確，網站自己算好的最終結果）
    2. __NEXT_DATA__ JSON（按鈕找不到時的備援，即使是「沒有選項」的單一商品也支援）
    3. 文字關鍵字比對（最後手段）
    """
    button_results, missing_names = _check_availability_via_buttons_only(html, product_names, is_single)

    if not missing_names:
        return button_results

    log.warning("按鈕判斷找不到「%s」，嘗試改用 __NEXT_DATA__ JSON 補齊", missing_names)
    json_results = _check_availability_via_next_data(html, missing_names, expected_sale_id, is_single)
    if json_results:
        button_results.update(json_results)
        missing_names = [n for n in missing_names if n not in json_results]

    if missing_names:
        log.warning("__NEXT_DATA__ 也沒有「%s」的資料，改用文字關鍵字比對（最後手段，可能不準確）", missing_names)
        for name in missing_names:
            button_results[name] = _check_availability_by_keyword_fallback(html, name, is_single)

    return button_results


def _check_availability_via_next_data(html: str, product_names, expected_sale_id=None, is_single=False):
    """
    解析 __NEXT_DATA__ JSON。

    重要：候選資料的篩選條件用「有沒有 status/saleId 欄位」而不是「有沒有 options 陣列」，
    因為單一商品（沒有款式選項）的資料本來就不會有 options 陣列，如果篩選條件寫死要求
    options 存在，會導致單一商品永遠找不到候選資料、JSON 備援形同虛設。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return None

        next_data = json.loads(script.string)
        queries = next_data["props"]["pageProps"]["$dehydratedState"]["queries"]

        candidates = []
        for q in queries:
            key = q.get("queryKey") or []
            if key and isinstance(key[0], str) and key[0].startswith("GET:/api/v1/sales/"):
                data = (q.get("state") or {}).get("data") or {}
                # 用 status 欄位是否存在來判斷「這是不是商品詳細資料」，
                # 不要求一定要有 options（單一商品沒有 options 是正常的）
                if "status" not in data:
                    continue
                option_block = data.get("option") or {}
                query_sale_id = None
                if len(key) > 1 and isinstance(key[1], dict):
                    query_sale_id = key[1].get("saleId")
                data_sale_id = data.get("saleId")
                candidates.append(
                    (str(query_sale_id) if query_sale_id is not None else None,
                     str(data_sale_id) if data_sale_id is not None else None,
                     option_block.get("options") or [],
                     data.get("status"))
                )

        if not candidates:
            return None

        options = None
        overall_status = None
        if expected_sale_id is not None:
            for query_sale_id, data_sale_id, opts, status in candidates:
                if expected_sale_id in (query_sale_id, data_sale_id):
                    options = opts
                    overall_status = status
                    break
            if overall_status is None:
                log.warning(
                    "__NEXT_DATA__ 裡找到 %d 筆商品資料，但沒有一筆的商品編號跟網址（%s）相符，"
                    "改用文字關鍵字比對備援，避免抓到別的商品的庫存狀態",
                    len(candidates), expected_sale_id,
                )
                return None
        else:
            options = candidates[0][2]
            overall_status = candidates[0][3]

        # 商品整體如果不是「販售中」，一律視為全部不可購買
        if overall_status != "SALE":
            log.info("商品整體狀態為「%s」（不是 SALE），視為不可購買", overall_status)
            return {name: False for name in product_names}

        if is_single:
            # 單一商品：如果有拿到選項資料（有些單一商品其實內部仍有一個預設選項），
            # 用該選項自己的 isSoldOut 判斷，比只看整體狀態更保守可靠；
            # 如果完全沒有選項資料，才單純依賴整體狀態 = SALE 判斷為有貨。
            if options:
                is_available = any(not opt.get("isSoldOut", True) for opt in options)
            else:
                is_available = True
            return {name: is_available for name in product_names}

        name_to_status = {}
        for opt in options:
            opt_name = (opt.get("saleOptionName") or "").strip().lower()
            is_sold_out = opt.get("isSoldOut", True)
            if opt_name:
                name_to_status[opt_name] = not is_sold_out

        results = {}
        for name in product_names:
            key = name.strip().lower()
            if key in name_to_status:
                results[name] = name_to_status[key]

        return results
    except Exception as e:
        log.warning("解析 __NEXT_DATA__ 時發生例外（改用備援）：%s", e)
        return None


def _check_availability_via_buttons_only(html: str, product_names, is_single=False):
    """
    主要判斷方式：讀取 <button> 的 disabled 屬性。
    回傳 (results, missing_names)。
    """
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    missing_names = []

    if is_single:
        # 單一商品：找「文字裡包含購買關鍵字」的按鈕（涵蓋中英文），
        # 只要找到任何一個符合的按鈕就採用它的 disabled 狀態。
        buy_button_found = False
        is_available = False
        for btn in soup.find_all("button"):
            text = btn.get_text(strip=True).lower()
            if any(kw in text for kw in AVAILABLE_KEYWORDS):
                buy_button_found = True
                is_available = not btn.has_attr("disabled")
                break

        if buy_button_found:
            for name in product_names:
                results[name] = is_available
        else:
            missing_names.extend(product_names)
        return results, missing_names

    # 一般多款式商品：依商品名稱比對按鈕文字
    text_to_buttons: dict[str, list] = {}
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True)
        if text:
            text_to_buttons.setdefault(text.lower(), []).append(btn)

    for name in product_names:
        matches = text_to_buttons.get(name.strip().lower())
        if matches:
            is_available = any(not btn.has_attr("disabled") for btn in matches)
            results[name] = is_available
        else:
            missing_names.append(name)

    return results, missing_names


def _check_availability_by_keyword_fallback(html: str, name: str, is_single: bool = False) -> bool:
    """最後手段的文字關鍵字判斷"""
    lower_html = html.lower()

    if is_single:
        # 單一商品沒有特定名稱可以定位查找範圍，改成用整個頁面判斷
        has_sold_out = any(kw in lower_html for kw in SOLD_OUT_KEYWORDS)
        has_available_word = any(kw in lower_html for kw in AVAILABLE_KEYWORDS)
        return (not has_sold_out) and has_available_word

    idx = lower_html.find(name.lower())
    if idx == -1:
        log.warning("在頁面上完全找不到商品名稱：%s", name)
        return False

    start = max(0, idx - 50)
    end = min(len(lower_html), idx + CONTEXT_WINDOW)
    context = lower_html[start:end]

    has_sold_out = any(kw in context for kw in SOLD_OUT_KEYWORDS)
    has_available_word = any(kw in context for kw in AVAILABLE_KEYWORDS)
    return (not has_sold_out) and has_available_word


# ------------------------------------------------------------------
# MUJI 專用判斷邏輯
# ------------------------------------------------------------------

# MUJI 商品頁的「加入購物車」按鈕，只有文字完全是這個才代表真的能買。
# 注意：不能只看按鈕「能不能點」，因為缺貨時常常會出現另一顆同樣可以點擊、
# 但其實是「訂閱補貨通知」的按鈕（文字是「再入荷の通知を受け取る」），
# 如果只看 disabled 屬性會誤判成有貨。
MUJI_ADD_TO_CART_TEXT = "カートに入れる"

# MUJI 網頁原始碼裡，Next.js 會把資料內嵌成類似
#   "productDetailCartButton":{"__typename":"ProductDetailCartActionButton","text":"在庫なし","type":"NoStock"}
# 這樣的文字直接寫在 <script> 標籤裡（伺服器端就已經算好的最終結果，不需要等瀏覽器執行
# JavaScript 才會出現）。這個規則式比對用來抓出這段資料裡的 text 欄位。
_MUJI_CART_BUTTON_PATTERN = re.compile(
    r'"productDetailCartButton"\s*:\s*\{[^}]*?"text"\s*:\s*"([^"]*)"[^}]*?"type"\s*:\s*"([^"]*)"'
)


def check_muji_availability(html: str):
    """
    回傳 True / False / None：
      True  -> 確定有貨
      False -> 確定沒貨（在庫なし、再入荷の通知を受け取る等）
      None  -> 完全找不到判斷依據（可能網站改版了）

    判斷順序：
    1. 【最可靠】解析網頁原始碼裡內嵌的 productDetailCartButton 資料，這是伺服器端
       已經算好的最終結果，不受「畫面還沒載入完成」影響。
    2. 【備援】上面找不到時，才退回讀取 <button id="products_cart"> 的文字內容跟
       disabled 屬性（有可能受頁面載入時機影響，可靠度較低）。

    ⚠️ 提醒：這個判斷只能反映「網站當下算出的狀態」，跟「結帳當下是否真的還有貨」
    中間仍然可能有極短暫的時間差，熱門商品仍然可能發生「查詢時有貨、結帳時沒貨」的
    情況，這是任何監控方式都無法完全避免的限制。
    """
    json_result = _check_muji_via_embedded_json(html)
    if json_result is not None:
        return json_result

    log.warning("找不到 MUJI 內嵌的 productDetailCartButton 資料，改用按鈕 DOM 判斷備援")
    return _check_muji_via_buttons(html)


def _check_muji_via_embedded_json(html: str):
    """從網頁原始碼裡直接用規則比對抓出 productDetailCartButton 的 text 欄位"""
    try:
        # 這段資料在原始碼裡的引號有時候會被跳脫成 \"，統一還原成一般引號再比對
        cleaned = html.replace('\\"', '"')
        match = _MUJI_CART_BUTTON_PATTERN.search(cleaned)
        if not match:
            return None

        raw_text = match.group(1)
        try:
            # 網頁原始碼裡的中日文字通常會被編碼成 \uXXXX，這裡解碼還原成正常文字
            decoded_text = raw_text.encode().decode("unicode_escape")
        except Exception:
            decoded_text = raw_text

        return decoded_text.strip() == MUJI_ADD_TO_CART_TEXT
    except Exception as e:
        log.warning("解析 MUJI 內嵌 JSON 時發生例外（改用備援）：%s", e)
        return None


def _check_muji_via_buttons(html: str):
    """備援：讀取 <button id="products_cart"> 的文字內容跟 disabled 屬性"""
    soup = BeautifulSoup(html, "html.parser")

    buttons = soup.find_all("button", id="products_cart")
    if not buttons:
        buttons = [
            b for b in soup.find_all("button")
            if MUJI_ADD_TO_CART_TEXT in b.get_text() or "在庫なし" in b.get_text()
        ]

    if not buttons:
        return None

    for btn in buttons:
        text = btn.get_text(strip=True)
        if text == MUJI_ADD_TO_CART_TEXT and not btn.has_attr("disabled"):
            return True

    return False


# ------------------------------------------------------------------
# 狀態儲存
# ------------------------------------------------------------------

def load_previous_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("讀取 state.json 失敗，視為空白狀態重新開始")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# 通知函式（保留錯誤 log，方便之後除錯，不再整段吞掉例外）
# ------------------------------------------------------------------

def notify_discord(message: str, webhook_url: str = None) -> None:
    url = webhook_url or DISCORD_WEBHOOK_URL
    if not url:
        return
    try:
        resp = requests.post(url, json={"content": message}, timeout=10)
        if resp.status_code >= 300:
            log.error("Discord 通知失敗：%s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Discord 通知發生例外：%s", e)


def notify_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        if resp.status_code >= 300:
            log.error("Telegram 通知失敗：%s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram 通知發生例外：%s", e)


def notify_gmail(subject: str, message: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not GMAIL_TO:
        return
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_TO
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [GMAIL_TO], msg.as_string())
    except Exception as e:
        log.error("Gmail 通知發生例外：%s", e)


def send_to_all_channels(message: str, subject: str = "Weverse 商品開賣通知", discord_webhook_url: str = None) -> None:
    """
    discord_webhook_url：想指定用某個特定 Discord 頻道時傳入，
    不傳的話就用預設的 DISCORD_WEBHOOK_URL。
    Telegram / Gmail 目前維持共用同一組設定（沒有分頻道的需求）。
    """
    notify_discord(message, webhook_url=discord_webhook_url)
    notify_telegram(message)
    notify_gmail(subject, message)


def get_discord_webhook_for_page(page: dict) -> str:
    """依照商品頁面是哪個網站，決定要用哪一組 Discord Webhook（沒設定專屬的就用預設那組）"""
    if page.get("site") == "muji" and DISCORD_WEBHOOK_URL_MUJI:
        return DISCORD_WEBHOOK_URL_MUJI
    return DISCORD_WEBHOOK_URL


def build_status_message(page: dict, current_state: dict, newly_available: list, header: str = None) -> str:
    title = header if header else f"📦 {page['label']}"
    lines = [title, ""]
    for name in page["products"]:
        is_available = current_state.get(name, False)
        status_text = "✅ 有貨" if is_available else "❌ 缺貨"
        lines.append(f"{status_text}：{name}")
    if newly_available:
        lines.append("")
        for name in newly_available:
            lines.append(f"🎉 {name} 補貨了！")
    lines.append("")
    lines.append(page["url"])
    lines.append(datetime.now(TAIWAN_TZ).strftime("偵測時間：%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def get_current_state_for_page(page: dict, html: str) -> dict:
    """依照 page["site"] 分流到對應網站的判斷邏輯，統一回傳 {商品名稱: True/False}"""
    site = page.get("site", "weverse")

    if site == "muji":
        product_name = page["products"][0]
        is_available = check_muji_availability(html)
        if is_available is None:
            log.warning("[%s] 找不到 MUJI 加入購物車按鈕，可能網站改版了，暫時視為不可購買", page["label"])
            is_available = False
        return {product_name: is_available}

    return check_availability(
        html, page["products"],
        expected_sale_id=extract_sale_id_from_url(page["url"]),
        is_single=page.get("is_single", False),
    )


def check_one_page(page: dict, all_previous_state: dict) -> dict:
    url = page["url"]
    label = page["label"]

    try:
        html = fetch_page_html(url)
    except Exception as e:
        log.error("抓取網頁失敗（%s / %s）：%s", label, url, e)
        return all_previous_state.get(url, {})

    current_state = get_current_state_for_page(page, html)

    is_first_run_for_page = url not in all_previous_state
    previous_state_for_page = all_previous_state.get(url, {})

    newly_available = []
    for name, is_available in current_state.items():
        was_available = previous_state_for_page.get(name, False)
        status_text = "可購買" if is_available else "不可購買/售完"
        log.info("[%s] %-20s -> %s", label, name, status_text)

        if is_available and not was_available:
            newly_available.append(name)

    if is_first_run_for_page:
        log.info("[%s] 第一次檢查這個頁面，記錄目前狀態作為基準值，不發送補貨通知", label)
    elif newly_available:
        message = build_status_message(page, current_state, newly_available)
        log.info("[%s] 偵測到補貨，發送通知：%s", label, newly_available)
        send_to_all_channels(message, discord_webhook_url=get_discord_webhook_for_page(page))

    return current_state


def run_once():
    all_previous_state = load_previous_state()
    all_new_state = dict(all_previous_state)
    for page in MONITORED_PAGES:
        all_new_state[page["url"]] = check_one_page(page, all_previous_state)
    save_state(all_new_state)


def run_status_report():
    all_previous_state = load_previous_state()
    all_new_state = dict(all_previous_state)

    for page in MONITORED_PAGES:
        url = page["url"]
        label = page["label"]
        try:
            html = fetch_page_html(url)
        except Exception as e:
            log.error("抓取網頁失敗（%s / %s）：%s", label, url, e)
            continue

        current_state = get_current_state_for_page(page, html)
        all_new_state[url] = current_state

        for name, is_available in current_state.items():
            status_text = "可購買" if is_available else "不可購買/售完"
            log.info("[%s] %-20s -> %s", label, name, status_text)

        message = build_status_message(page, current_state, newly_available=[], header=f"🔍 目前庫存查詢：{label}")
        log.info("[%s] 發送目前庫存狀態查詢結果", label)
        send_to_all_channels(message, subject="Weverse 庫存狀態查詢", discord_webhook_url=get_discord_webhook_for_page(page))

    save_state(all_new_state)


def run_test_notifications():
    message = (
        "✅ 這是一則測試訊息\n\n"
        "如果你在 Discord / Telegram / Gmail 收到這則訊息，代表這個通知管道設定成功。\n"
        f"發送時間：{datetime.now(TAIWAN_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    sent_any = False
    if DISCORD_WEBHOOK_URL:
        log.info("正在發送 Discord 測試訊息...")
        notify_discord(message)
        sent_any = True
    else:
        log.info("未設定 DISCORD_WEBHOOK_URL，略過 Discord 測試")

    if DISCORD_WEBHOOK_URL_MUJI:
        log.info("正在發送 Discord（MUJI 專用頻道）測試訊息...")
        notify_discord(message, webhook_url=DISCORD_WEBHOOK_URL_MUJI)
        sent_any = True
    else:
        log.info("未設定 DISCORD_WEBHOOK_URL_MUJI，略過 MUJI 專用頻道測試")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        log.info("正在發送 Telegram 測試訊息...")
        notify_telegram(message)
        sent_any = True
    else:
        log.info("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過 Telegram 測試")

    if GMAIL_ADDRESS and GMAIL_APP_PASSWORD and GMAIL_TO:
        log.info("正在發送 Gmail 測試信...")
        notify_gmail("Weverse 監控程式 - 測試信", message)
        sent_any = True
    else:
        log.info("未設定 Gmail 相關欄位，略過 Gmail 測試")

    if not sent_any:
        log.warning("三個通知管道都沒有設定任何內容，請檢查 .env 或 GitHub Secrets 是否填寫正確")
    else:
        log.info("測試訊息已發送完畢，請到對應的 App / 信箱確認有沒有收到")


def main():
    import sys

    run_only_once = "--once" in sys.argv
    run_test = "--test" in sys.argv
    run_status = "--status" in sys.argv

    log.info("開始監控 %d 個 Weverse 商品頁面", len(MONITORED_PAGES))
    for page in MONITORED_PAGES:
        log.info("  - %s：%s", page["label"], page["url"])

    if run_test:
        log.info("以 --test 模式執行（只發測試通知，不檢查商品頁面）")
        run_test_notifications()
        return

    if run_status:
        log.info("以 --status 模式執行（直接查詢並回報目前庫存狀態）")
        run_status_report()
        return

    if run_only_once:
        log.info("以 --once 模式執行（只檢查一次）")
        run_once()
        return

    log.info("每 %d 秒（%.1f 分鐘）檢查一次（本機常駐模式）", CHECK_INTERVAL_SECONDS, CHECK_INTERVAL_SECONDS / 60)
    while True:
        run_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
