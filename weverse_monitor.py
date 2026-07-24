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
import json
import time
import logging
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime

import requests
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
        "label": "商品頁 43782 原皮吊飾",
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
        "label": "商品頁 60590 蘋果抱枕",
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

def check_availability(html: str, product_names) -> dict:
    """
    回傳格式： {"CHOI YONG MEONG": True/False, ...}
    True 代表判斷為「可購買」，False 代表「不可購買 / 找不到 / 售完」
    """
    lower_html = html.lower()
    results = {}

    for name in product_names:
        idx = lower_html.find(name.lower())
        if idx == -1:
            log.warning("在頁面上找不到商品名稱：%s（可能是名稱打錯，或該區塊需要 JS 才會出現）", name)
            results[name] = False
            continue

        start = max(0, idx - CONTEXT_WINDOW)
        end = min(len(lower_html), idx + len(name) + CONTEXT_WINDOW)
        context = lower_html[start:end]

        has_sold_out = any(kw in context for kw in SOLD_OUT_KEYWORDS)
        has_available_word = any(kw in context for kw in AVAILABLE_KEYWORDS)

        # 判斷邏輯：沒有 SOLD OUT 字樣，且有出現 ADD TO CART / PURCHASE 字樣 -> 可購買
        is_available = (not has_sold_out) and has_available_word
        results[name] = is_available

    return results


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


def build_status_message(page: dict, current_state: dict, newly_available: list) -> str:
    """組出「完整庫存狀態 + 補貨提醒」的通知內容"""
    lines = [f"📦 {page['label']}", ""]

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

    current_state = check_availability(html, page["products"])

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

    log.info("開始監控 %d 個 Weverse 商品頁面", len(MONITORED_PAGES))
    for page in MONITORED_PAGES:
        log.info("  - %s：%s", page["label"], page["url"])

    if run_test:
        log.info("以 --test 模式執行（只發測試通知，不檢查商品頁面）")
        run_test_notifications()
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
