#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weverse Shop 商品到貨/開賣通知程式（支援同時監控多個商品頁面）
=====================================

功能：
1. 每隔 CHECK_INTERVAL_SECONDS（預設 900 秒 = 15 分鐘）檢查一次 MONITORED_PAGES
   裡列出的每一個 Weverse 商品頁面。
2. 針對每個頁面裡指定的商品名稱清單，判斷每個商品目前是「SOLD OUT（售完/無法購買）」
   還是「可購買（頁面出現 ADD TO CART 或 PURCHASE 字樣、且沒有 SOLD OUT 字樣）」。
3. 每次通知都會列出「這個頁面目前所有商品的完整庫存狀態」，
   如果有商品是「這次才從不可購買變成可購買」，會額外加一行「XXX 補貨了！」。
4. 第一次執行某個頁面時（還沒有歷史狀態可以比較），只會記錄當下狀態當作基準值，
   不會發送「補貨了」通知（避免把「本來就有貨」誤判成「剛補貨」）。
5. 支援三種通知方式：Discord Webhook、Telegram Bot、Gmail（可以同時開啟多種）。

⚠️ 重要提醒（請務必閱讀）：
Weverse Shop 是用 Next.js 打造的網站，商品頁面裡「選擇款式」的下拉選單
在某些情況下是由瀏覽器端的 JavaScript 動態渲染或動態抓取庫存資料。這支程式預設用
「直接下載網頁原始碼」（requests）的方式去偵測文字，如果實際測試發現抓不到正確的
售罄狀態，代表該區塊需要瀏覽器執行 JavaScript 才會出現，這時請改用本檔案底部
「進階：Playwright 版本」的做法。

使用前請先：
    pip install -r requirements.txt
    cp .env.example .env   # 然後把 .env 內容改成你自己的通知設定
"""

import os
import re
import json
import time
import logging
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 基本設定：想追蹤的商品頁面清單
# ------------------------------------------------------------------

load_dotenv()  # 讀取同目錄下的 .env 檔案

# 每一個項目代表一個要監控的商品頁面：
#   url      -> 商品頁面網址
#   label    -> 這個頁面的顯示名稱（通知訊息裡會用到，方便分辨是哪一個頁面）
#   products -> 這個頁面裡要追蹤的商品名稱清單
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
        "url": "https://shop.weverse.io/en/shop/KRW/artists/3/sales/60590",
        "label": "商品頁 60590",
        "products": [
            "CHOI YONG MEONG",
            "HWANG CHOON",
            "BAMGEUT",
            "DA-GO-NYANG",
            "HHM NYA RING",
        ],
    },
]

CHECK_INTERVAL_SECONDS = 15 * 60  # 15 分鐘

STATE_FILE = Path(__file__).parent / "state.json"
LOG_FILE = Path(__file__).parent / "monitor.log"

# 判斷用的關鍵字（不分大小寫）
SOLD_OUT_KEYWORDS = ["sold out", "품절", "매진"]
AVAILABLE_KEYWORDS = ["add to cart", "purchase", "buy now"]

# 每個商品名稱前後要抓多少字元來判斷狀態（因為名稱旁邊通常會緊跟著狀態文字/按鈕文字）
CONTEXT_WINDOW = 300

# ------------------------------------------------------------------
# 通知管道設定（從 .env 讀取，沒有設定的管道會自動略過）
# ------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

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


# ------------------------------------------------------------------
# 抓取網頁
# ------------------------------------------------------------------

HEADERS = {
    # 模擬一般瀏覽器的 User-Agent，降低被網站擋掉的機率
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_page_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


# ------------------------------------------------------------------
# 判斷各商品的可購買狀態
# ------------------------------------------------------------------

def extract_sale_id_from_url(url: str):
    """從商品網址（.../sales/60590）取出商品編號，取不到就回傳 None"""
    match = re.search(r"/sales/(\d+)", url)
    return match.group(1) if match else None


def check_availability(html: str, product_names, expected_sale_id=None) -> dict:
    """
    回傳格式： {"CHOI YONG MEONG": True/False, ...}
    True 代表判斷為「可購買」，False 代表「不可購買 / 找不到 / 售完」

    判斷方式（依可靠度由高到低，逐層備援）：
    1. 【最可靠】解析網頁裡的 __NEXT_DATA__ JSON 區塊，這是 Weverse 網站自己內部
       使用的資料，裡面每個款式都直接寫著 isSoldOut: true/false，不需要用任何
       文字或 HTML 結構去猜測，準確度最高。這裡會用 expected_sale_id 明確比對
       「這筆資料是不是這個商品本身的」，避免頁面裡如果還夾雜其他商品（例如
       推薦商品區塊）的資料時，不小心抓到別的商品的庫存狀態。
    2. 【次可靠】如果找不到 __NEXT_DATA__ 或格式跟預期不同，改用 <button> 的
       disabled 屬性判斷（賣完的款式按鈕會有 disabled 屬性）。
    3. 【最後手段】如果連按鈕都找不到，退回用文字關鍵字比對（SOLD OUT / ADD TO CART）。

    每往下退一層都會記錄警告，方便你發現網站結構是否有變動。
    """
    next_data_status = _check_availability_via_next_data(html, product_names, expected_sale_id)
    if next_data_status is not None:
        return next_data_status

    log.warning("找不到可用的 __NEXT_DATA__ 商品資料，改用按鈕 disabled 屬性判斷（次可靠備援）")
    return _check_availability_via_buttons(html, product_names)


def _check_availability_via_next_data(html: str, product_names, expected_sale_id=None):
    """
    嘗試從網頁裡的 <script id="__NEXT_DATA__"> JSON 區塊，找到「這個商品自己」的
    option.options 陣列（裡面有 saleOptionName 和 isSoldOut）。
    找不到、格式不符，或商品編號對不上，就回傳 None，讓上層改用備援方式。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return None

        next_data = json.loads(script.string)
        queries = next_data["props"]["pageProps"]["$dehydratedState"]["queries"]

        # 先收集所有「長得像商品資料」的候選項，稍後依 expected_sale_id 精準篩選
        candidates = []
        for q in queries:
            key = q.get("queryKey") or []
            if key and isinstance(key[0], str) and key[0].startswith("GET:/api/v1/sales/"):
                data = (q.get("state") or {}).get("data") or {}
                option_block = data.get("option") or {}
                if option_block.get("options"):
                    query_sale_id = None
                    if len(key) > 1 and isinstance(key[1], dict):
                        query_sale_id = key[1].get("saleId")
                    # data 裡通常也會有 saleId 欄位，雙重確認
                    data_sale_id = data.get("saleId")
                    candidates.append(
                        (str(query_sale_id) if query_sale_id is not None else None,
                         str(data_sale_id) if data_sale_id is not None else None,
                         option_block["options"])
                    )

        if not candidates:
            return None

        options = None
        if expected_sale_id is not None:
            for query_sale_id, data_sale_id, opts in candidates:
                if expected_sale_id in (query_sale_id, data_sale_id):
                    options = opts
                    break
            if options is None:
                log.warning(
                    "__NEXT_DATA__ 裡找到 %d 筆商品資料，但沒有一筆的商品編號跟網址（%s）相符，"
                    "改用按鈕判斷備援，避免抓到別的商品的庫存狀態",
                    len(candidates), expected_sale_id,
                )
                return None
        else:
            # 沒有提供 expected_sale_id 時，退回用第一筆（維持舊行為，但風險較高）
            options = candidates[0][2]

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
            else:
                log.warning("__NEXT_DATA__ 裡找不到商品「%s」，改用按鈕判斷備援", name)
                results[name] = _check_availability_via_buttons(html, [name])[name]

        return results
    except Exception as e:
        log.warning("解析 __NEXT_DATA__ 時發生例外（改用備援）：%s", e)
        return None


def _check_availability_via_buttons(html: str, product_names) -> dict:
    """次可靠備援：讀取 <button> 的 disabled 屬性"""
    soup = BeautifulSoup(html, "html.parser")

    text_to_buttons: dict[str, list] = {}
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True)
        if text:
            text_to_buttons.setdefault(text.lower(), []).append(btn)

    results = {}
    for name in product_names:
        matches = text_to_buttons.get(name.strip().lower())
        if matches:
            is_available = any(not btn.has_attr("disabled") for btn in matches)
            results[name] = is_available
        else:
            log.warning(
                "找不到「%s」對應的按鈕元素，改用文字關鍵字比對備援（最後手段，可能不準確）",
                name,
            )
            results[name] = _check_availability_by_keyword_fallback(html, name)

    return results


def _check_availability_by_keyword_fallback(html: str, name: str) -> bool:
    """備援用的文字關鍵字判斷方式（當找不到對應的按鈕元素時才會用到）"""
    lower_html = html.lower()
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
# 狀態儲存（避免重複通知）
# 格式： { "<頁面網址>": {"<商品名稱>": true/false, ...}, ... }
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
# 通知函式：Discord / Telegram / Gmail
# ------------------------------------------------------------------

def notify_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        if resp.status_code >= 300:
            log.error("Discord 通知失敗：%s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Discord 通知發生例外：%s", e)


def notify_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
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


def send_to_all_channels(message: str, subject: str = "Weverse 商品開賣通知") -> None:
    notify_discord(message)
    notify_telegram(message)
    notify_gmail(subject, message)


def build_status_message(page: dict, current_state: dict, newly_available: list, header: str = None) -> str:
    """組出「完整庫存狀態 + 補貨提醒」的通知內容"""
    title = header if header else f"📦 {page['label']}"
    lines = [title, ""]

    for name in page["products"]:
        is_available = current_state.get(name, False)
        status_text = "✅ 有貨" if is_available else "❌ 缺貨"
        lines.append(f"{status_text }：{name}")

    if newly_available:
        lines.append("")
        for name in newly_available:
            lines.append(f"🎉 {name} 補貨了！")

    lines.append("")
    lines.append(page["url"])
    lines.append(datetime.now().strftime("偵測時間：%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def check_one_page(page: dict, all_previous_state: dict) -> dict:
    """
    檢查單一頁面，回傳這個頁面最新的狀態（{商品名稱: True/False}）。
    如果有需要通知的內容（新補貨），會直接發送通知。
    """
    url = page["url"]
    label = page["label"]

    try:
        html = fetch_page_html(url)
    except Exception as e:
        log.error("抓取網頁失敗（%s / %s）：%s", label, url, e)
        # 抓取失敗就沿用上一次的狀態，避免把「抓取失敗」誤判成「全部售完」
        return all_previous_state.get(url, {})

    current_state = check_availability(html, page["products"], expected_sale_id=extract_sale_id_from_url(url))

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
        send_to_all_channels(message)

    return current_state


def run_once():
    all_previous_state = load_previous_state()
    all_new_state = dict(all_previous_state)  # 保留其他頁面舊的狀態，逐一更新

    for page in MONITORED_PAGES:
        all_new_state[page["url"]] = check_one_page(page, all_previous_state)

    save_state(all_new_state)


def run_status_report():
    """
    不管有沒有變化，直接查詢並發送「目前所有頁面的完整庫存狀態」。
    用途：手動想立刻知道現在的庫存狀況，不用等排程自動偵測到變化才通知。
    """
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

        current_state = check_availability(html, page["products"], expected_sale_id=extract_sale_id_from_url(url))
        all_new_state[url] = current_state

        for name, is_available in current_state.items():
            status_text = "可購買" if is_available else "不可購買/售完"
            log.info("[%s] %-20s -> %s", label, name, status_text)

        message = build_status_message(page, current_state, newly_available=[], header=f"🔍 目前庫存查詢：{label}")
        log.info("[%s] 發送目前庫存狀態查詢結果", label)
        send_to_all_channels(message, subject="Weverse 庫存狀態查詢")

    save_state(all_new_state)


def run_test_notifications():
    """
    獨立的測試功能：不檢查網頁，直接對「有填寫設定」的通知管道各發一則測試訊息，
    方便確認 Discord / Telegram / Gmail 的 Webhook、Token、密碼有沒有設定正確。
    """
    message = (
        "✅ 這是一則測試訊息\n\n"
        "如果你在 Discord / Telegram / Gmail 收到這則訊息，代表這個通知管道設定成功。\n"
        f"發送時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    sent_any = False

    if DISCORD_WEBHOOK_URL:
        log.info("正在發送 Discord 測試訊息...")
        notify_discord(message)
        sent_any = True
    else:
        log.info("未設定 DISCORD_WEBHOOK_URL，略過 Discord 測試")

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
        # 給雲端排程器（例如 GitHub Actions）使用：只跑一次就結束，
        # 由排程器（cron）每 15 分鐘啟動一次這支程式，而不是讓程式自己無限迴圈。
        log.info("以 --once 模式執行（只檢查一次）")
        run_once()
        return

    log.info("每 %d 秒（%.1f 分鐘）檢查一次（本機常駐模式）", CHECK_INTERVAL_SECONDS, CHECK_INTERVAL_SECONDS / 60)
    while True:
        run_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

# ------------------------------------------------------------------
# 進階：如果 requests 抓不到正確狀態，改用 Playwright（真瀏覽器渲染）
# ------------------------------------------------------------------
#
# 1. 安裝：
#      pip install playwright
#      playwright install chromium
#
# 2. 把上面的 fetch_page_html() 換成類似這樣的寫法：
#
#     from playwright.sync_api import sync_playwright
#
#     def fetch_page_html(url: str) -> str:
#         with sync_playwright() as p:
#             browser = p.chromium.launch(headless=True)
#             page = browser.new_page()
#             page.goto(url, wait_until="networkidle", timeout=30000)
#             # 如果商品是用下拉選單切換款式，可能需要在這裡加上：
#             # page.click("你的下拉選單 selector")
#             # page.click(f"text={款式名稱}")
#             # page.wait_for_timeout(1000)
#             html = page.content()
#             browser.close()
#             return html
#
#    實際的 selector 要用瀏覽器「開發人員工具」(F12) 去找按鈕/下拉選單的
#    CSS selector 或文字內容，因為每個網站結構不同，需要照實際頁面調整。
