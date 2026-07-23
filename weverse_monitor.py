#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weverse Shop 商品到貨/開賣通知程式
=====================================

功能：
1. 每隔 CHECK_INTERVAL_SECONDS（預設 900 秒 = 15 分鐘）檢查一次指定的 Weverse 商品頁面。
2. 針對指定的商品名稱清單，判斷每個商品目前是「SOLD OUT（售完/無法購買）」
   還是「可購買（頁面出現 ADD TO CART 或 PURCHASE 字樣、且沒有 SOLD OUT 字樣）」。
3. 只有在「狀態從『不可購買』變成『可購買』」時才發出通知（避免每 15 分鐘狂發同樣的訊息）。
4. 支援三種通知方式：Discord Webhook、Telegram Bot、Gmail（可以同時開啟多種）。

⚠️ 重要提醒（請務必閱讀）：
Weverse Shop 是用 Next.js 打造的網站，商品頁面裡「選擇款式」的下拉選單
（例如 CHOI YONG MEONG / HWANG CHOON / BAMGEUT ...）在某些情況下是由瀏覽器端的
JavaScript 動態渲染或動態抓取庫存資料。這支程式預設用「直接下載網頁原始碼」
（requests）的方式去偵測文字，如果實際測試發現抓不到正確的售罄狀態
（例如每次都顯示同一種結果、或抓不到 5 款商品的名稱），
代表該區塊需要瀏覽器執行 JavaScript 才會出現，這時請改用下面「進階：Playwright 版本」
的做法（本檔案底部有寫法說明），用真的瀏覽器（headless browser）去渲染後再判斷。

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
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 基本設定
# ------------------------------------------------------------------

load_dotenv()  # 讀取同目錄下的 .env 檔案

PRODUCT_URL = "https://shop.weverse.io/en/shop/KRW/artists/3/sales/43782"

# 想要追蹤的 5 款商品名稱
TARGET_PRODUCTS = [
    "CHOI YONG MEONG",
    "HWANG CHOON",
    "BAMGEUT",
    "DA-GO-NYANG",
    "HHM NYA RING",
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
            # 在頁面上找不到這個商品名稱，先記為不可購買，並記錄警告方便除錯
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


def notify_all(newly_available):
    """newly_available: 這次新偵測到「可購買」的商品名稱清單"""
    if not newly_available:
        return

    lines = ["🎉 Weverse 商品現在可以購買了！", ""]
    for name in newly_available:
        lines.append(f"• {name}")
    lines.append("")
    lines.append(PRODUCT_URL)
    lines.append(datetime.now().strftime("偵測時間：%Y-%m-%d %H:%M:%S"))
    message = "\n".join(lines)

    log.info("發現新可購買商品，發送通知：%s", newly_available)

    notify_discord(message)
    notify_telegram(message)
    notify_gmail("Weverse 商品開賣通知", message)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def run_once():
    try:
        html = fetch_page_html(PRODUCT_URL)
    except Exception as e:
        log.error("抓取網頁失敗：%s", e)
        return

    current_state = check_availability(html, TARGET_PRODUCTS)
    previous_state = load_previous_state()

    newly_available = []
    for name, is_available in current_state.items():
        was_available = previous_state.get(name, False)
        status_text = "可購買" if is_available else "不可購買/售完"
        log.info("%-20s -> %s", name, status_text)

        if is_available and not was_available:
            newly_available.append(name)

    notify_all(newly_available)
    save_state(current_state)


def main():
    import sys

    run_only_once = "--once" in sys.argv

    log.info("開始監控 Weverse 商品頁面：%s", PRODUCT_URL)
    log.info("追蹤商品：%s", ", ".join(TARGET_PRODUCTS))

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
