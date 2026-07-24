#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weverse Shop 商品到貨/開賣通知程式（支援同時監控多個商品頁面）
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
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 基本設定：想追蹤的商品頁面清單
# ------------------------------------------------------------------

load_dotenv()

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
    # 👇 新增的單一商品頁面在這裡
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/255/sales/51616",
        "label": "商品頁 51616 (單一商品)",
        "is_single": True,           # 告訴程式這是一個沒有款式選項的單一商品
        "products": ["缽專"],  # 你希望在通知訊息裡顯示的名字，隨便取即可
    },
    {
        "url": "https://shop.weverse.io/en/shop/KRW/artists/255/sales/51617",
        "label": "商品頁 51617 (單一商品)",
        "is_single": True,           # 告訴程式這是一個沒有款式選項的單一商品
        "products": ["球專"],  # 你希望在通知訊息裡顯示的名字，隨便取即可
    },
]

CHECK_INTERVAL_SECONDS = 15 * 60  # 15 分鐘

STATE_FILE = Path(__file__).parent / "state.json"
LOG_FILE = Path(__file__).parent / "monitor.log"

SOLD_OUT_KEYWORDS = ["sold out", "품절", "매진"]
AVAILABLE_KEYWORDS = ["add to cart", "purchase", "buy now"]
CONTEXT_WINDOW = 300

# ------------------------------------------------------------------
# 通知管道設定
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

HEADERS = {
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
    match = re.search(r"/sales/(\d+)", url)
    return match.group(1) if match else None

def check_availability(html: str, product_names, expected_sale_id=None, is_single=False) -> dict:
    button_results, missing_names = _check_availability_via_buttons_only(html, product_names, is_single)

    if not missing_names:
        return button_results

    log.warning("按鈕判斷找不到「%s」，嘗試改用 __NEXT_DATA__ JSON 補齊", missing_names)
    json_results = _check_availability_via_next_data(html, missing_names, expected_sale_id, is_single)
    if json_results:
        button_results.update(json_results)
        missing_names = [n for n in missing_names if n not in json_results]

    if missing_names:
        log.warning("__NEXT_DATA__ 也沒有資料，改用文字關鍵字比對", missing_names)
        for name in missing_names:
            button_results[name] = _check_availability_by_keyword_fallback(html, name, is_single)

    return button_results

def _check_availability_via_next_data(html: str, product_names, expected_sale_id=None, is_single=False):
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
                option_block = data.get("option") or {}
                if option_block.get("options"):
                    query_sale_id = None
                    if len(key) > 1 and isinstance(key[1], dict):
                        query_sale_id = key[1].get("saleId")
                    data_sale_id = data.get("saleId")
                    overall_status = data.get("status")
                    candidates.append(
                        (str(query_sale_id) if query_sale_id is not None else None,
                         str(data_sale_id) if data_sale_id is not None else None,
                         option_block["options"],
                         overall_status)
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
            if options is None:
                return None
        else:
            options = candidates[0][2]
            overall_status = candidates[0][3]

        if overall_status != "SALE":
            return {name: False for name in product_names}

        # 若是單一商品且 overall_status 為 SALE，就代表有貨
        if is_single:
            return {name: True for name in product_names}

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
        log.warning("解析 __NEXT_DATA__ 時發生例外：%s", e)
        return None

def _check_availability_via_buttons_only(html: str, product_names, is_single=False):
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    missing_names = []

    # 針對單一商品的按鈕判斷邏輯
    if is_single:
        buy_button_found = False
        is_available = False
        for btn in soup.find_all("button"):
            text = btn.get_text(strip=True).lower()
            # 只要按鈕文字包含 purchase、buy now 等關鍵字，就認定它是購買按鈕
            if any(kw in text for kw in AVAILABLE_KEYWORDS):
                buy_button_found = True
                is_available = not btn.has_attr("disabled")
                break
        
        if buy_button_found:
            for name in product_names:
                results[name] = is_available
        else:
            for name in product_names:
                missing_names.append(name)
        return results, missing_names

    # 原始多款式商品的按鈕判斷邏輯
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

def _check_availability_by_keyword_fallback(html: str, name: str, is_single=False) -> bool:
    if is_single:
        # 針對單一商品，因為沒有特定名稱可以比對位置，為了避免誤判 header/footer 的關鍵字，退回 False 交給安全機制處理
        return False
        
    lower_html = html.lower()
    idx = lower_html.find(name.lower())
    if idx == -1:
        return False

    start = max(0, idx - 50)
    end = min(len(lower_html), idx + CONTEXT_WINDOW)
    context = lower_html[start:end]

    has_sold_out = any(kw in context for kw in SOLD_OUT_KEYWORDS)
    has_available_word = any(kw in context for kw in AVAILABLE_KEYWORDS)
    return (not has_sold_out) and has_available_word


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
# 通知函式
# ------------------------------------------------------------------

def notify_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL: return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except: pass

def notify_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except: pass

def notify_gmail(subject: str, message: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not GMAIL_TO: return
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_TO
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [GMAIL_TO], msg.as_string())
    except: pass

def send_to_all_channels(message: str, subject: str = "Weverse 商品開賣通知") -> None:
    notify_discord(message)
    notify_telegram(message)
    notify_gmail(subject, message)

def build_status_message(page: dict, current_state: dict, newly_available: list, header: str = None) -> str:
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
    url = page["url"]
    label = page["label"]
    is_single = page.get("is_single", False) # 讀取是否為單一商品標籤

    try:
        html = fetch_page_html(url)
    except Exception as e:
        log.error("抓取網頁失敗（%s / %s）：%s", label, url, e)
        return all_previous_state.get(url, {})

    current_state = check_availability(
        html, 
        page["products"], 
        expected_sale_id=extract_sale_id_from_url(url), 
        is_single=is_single
    )

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
        is_single = page.get("is_single", False)
        try:
            html = fetch_page_html(url)
        except Exception as e:
            continue

        current_state = check_availability(
            html, 
            page["products"], 
            expected_sale_id=extract_sale_id_from_url(url), 
            is_single=is_single
        )
        all_new_state[url] = current_state

        for name, is_available in current_state.items():
            status_text = "可購買" if is_available else "不可購買/售完"
            log.info("[%s] %-20s -> %s", label, name, status_text)

        message = build_status_message(page, current_state, newly_available=[], header=f"🔍 目前庫存查詢：{label}")
        send_to_all_channels(message, subject="Weverse 庫存狀態查詢")
    save_state(all_new_state)


def run_test_notifications():
    message = "✅ 測試訊息\n發送時間：" + datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if DISCORD_WEBHOOK_URL: notify_discord(message)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID: notify_telegram(message)
    if GMAIL_ADDRESS and GMAIL_APP_PASSWORD and GMAIL_TO: notify_gmail("測試信", message)


def main():
    import sys
    run_only_once = "--once" in sys.argv
    run_test = "--test" in sys.argv
    run_status = "--status" in sys.argv

    if run_test:
        run_test_notifications()
        return
    if run_status:
        run_status_report()
        return
    if run_only_once:
        run_once()
        return

    while True:
        run_once()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
