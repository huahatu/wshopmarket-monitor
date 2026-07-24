# Weverse 商品開賣監控程式

每 15 分鐘檢查一次指定的 Weverse 商品頁面，偵測商品是否從「SOLD OUT」變成「可購買」，
一旦偵測到補貨就透過 Discord / Telegram / Gmail 通知你。

**目前設定監控 2 個頁面**（都是同樣的 5 款商品：CHOI YONG MEONG、HWANG CHOON、
BAMGEUT、DA-GO-NYANG、HHM NYA RING）：
- `sales/43782`
- `sales/60590`

每次通知會列出**該頁面目前完整的庫存狀態**（哪些有貨、哪些缺貨），
如果有商品是這次才從缺貨變成有貨，會額外加一行「🎉 XXX 補貨了！」。

想要新增或修改監控的頁面，打開 `weverse_monitor.py`，找到最上面的 `MONITORED_PAGES`
清單，依照裡面的格式（`url`、`label`、`products`）增加新的項目即可，不需要改動其他程式碼。

---

## ⚠️ 關於「排程沒有準時每 15 分鐘執行」

如果你發現實際執行間隔比 15 分鐘長很多（例如隔了 2~4 小時才執行一次），
**這是 GitHub Actions 本身已知、且官方文件承認的限制，不是你的設定有問題**：

> GitHub 官方文件說明：排程事件（schedule）在 GitHub Actions 負載較高時可能會延遲，
> 尤其是每個整點（UTC 00 分）前後是最容易塞車的時段。GitHub 並不保證排程會準時觸發，
> 官方措辭是「best effort」（盡力而為），沒有任何時間上的保證（SLA）。

社群回報過的實際狀況包括：延遲 15~60 分鐘是家常便飯，甚至有人回報過**完全被跳過、
隔了好幾個小時才補跑一次**的狀況，尤其容易發生在：
- 剛建立好、還很新的倉庫（GitHub 需要一點時間才會開始穩定觸發排程）
- 排程設定在整點附近（例如 `0 * * * *`）
- 免費帳號、使用量較低的倉庫（優先度較低，容易被排到後面）

**能不能讓它更準時？** 在 GitHub Actions 內部沒有辦法保證，這是平台本身的限制。
如果你真的需要精準的 15 分鐘間隔（例如搶購限量商品，差幾分鐘可能就會錯過），
比較可靠的做法是**用外部的免費排程服務，定時呼叫 GitHub API 觸發 workflow_dispatch**，
繞過 GitHub 自己內部不穩定的排程佇列。常見的免費外部排程服務有 cron-job.org、
EasyCron 等，只要註冊帳號設定「每 15 分鐘打一次 API」即可。如果你想要這個做法，
跟我說一聲，我可以幫你寫詳細設定步驟。

如果 15 分鐘沒有到、延遲個幾十分鐘到 1~2 小時你可以接受，就不需要額外處理，
讓它繼續跑一陣子（尤其是倉庫愈用愈久，排程通常會愈來愈穩定）。

---

## 0. 想要「電腦/手機關機也持續監測」？

方式 A（本機一直開著跑）沒辦法做到這件事——只要你的電腦睡眠、關機，或手機把 App 關掉，
程式就會停止。如果你要的是「不管我開不開機都持續監測」，你需要把程式放到**一台不屬於你、
一直開著的伺服器上執行**。這裡推薦最簡單、免費的做法：**GitHub Actions**（不需要自己買主機、
不需要一直開電腦，GitHub 會用他們的伺服器每 15 分鐘幫你執行一次）。


### 用 GitHub Actions 讓程式在雲端自動運作（推薦、免費）

1. **建立一個 GitHub 帳號**（如果還沒有的話）：https://github.com
2. **新增一個倉庫（Repository）**：右上角 `+` → `New repository`。
   - Repository name 隨意（例如 `weverse-monitor`）
   - 建議選 **Private**（私人），這樣別人看不到你的倉庫內容
   - 建立完成
3. 把我提供的整個 `weverse_monitor` 資料夾內容上傳到這個倉庫（包含 `.github/workflows/monitor.yml`
   這個檔案——它就是排程設定檔，已經幫你寫好了）。上傳方式：
   - 如果你熟悉 git：在資料夾內執行
     ```bash
     git init
     git add .
     git commit -m "init"
     git branch -M main
     git remote add origin https://github.com/你的帳號/weverse-monitor.git
     git push -u origin main
     ```
   - 如果不熟悉 git：可以直接在 GitHub 網頁上用「Add file → Upload files」把所有檔案拖曳上傳
     （**注意：不要上傳 `.env` 檔案本身**，因為裡面是你的私人金鑰／密碼——下一步會改用更安全的方式設定）
4. **設定機密資訊（Secrets）**：因為 `.env` 不能上傳到 GitHub（會外洩你的密碼），
   改成在 GitHub 網頁上設定「Repository secrets」：
   - 進入倉庫 → `Settings` → 左側選單 `Secrets and variables` → `Actions` → `New repository secret`
   - 依照你要用的通知方式，新增以下任一組（名稱要完全一致）：
     - `DISCORD_WEBHOOK_URL`
     - `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
     - `GMAIL_ADDRESS`、`GMAIL_APP_PASSWORD`、`GMAIL_TO`
   - 每個 secret 的值，就是你原本要填在 `.env` 裡的內容（怎麼取得請看下方第 2 章教學）
5. 完成後，到倉庫上方的 `Actions` 分頁，你會看到一個叫 `Weverse Monitor` 的 workflow。
   - 可以先點進去按 `Run workflow` 手動觸發一次，確認有沒有正常運作、有沒有收到通知
   - 之後它就會依照 `.github/workflows/monitor.yml` 裡設定的 `*/15 * * * *`，
     每 15 分鐘由 GitHub 的伺服器自動執行一次，**跟你的電腦、手機開不開機完全無關**
6. 如果想確認執行紀錄或除錯，到 `Actions` 分頁點進任一次執行紀錄，可以看到完整的 log 輸出。

### 如何立即查詢「目前完整庫存狀態」（不用等排程自動偵測到變化）

1. 到倉庫的 `Actions` 分頁，點左側 `Weverse Monitor`
2. 點右上角 `Run workflow`
3. 這次把 `status_check` 打勾（`test_notification` 不要勾）
4. 點綠色的 `Run workflow` 執行
5. 等 10~30 秒，你設定好的通知管道就會收到**每個監控頁面目前完整的庫存狀態**
   （每款商品分別顯示有貨/缺貨），不管有沒有發生變化都會發送。

### 關於「能不能顯示庫存數量」

不行——**Weverse Shop 的商品頁面本身就沒有公開顯示確切的剩餘庫存數量**，
頁面上只會顯示「SOLD OUT（售完）」或是可以購買，並不會寫「剩下 3 件」這種數字
（我實際檢查過同類型的其他商品頁面，確認頁面結構上就是沒有這項資訊）。
這不是程式抓不到，而是網站本身根本沒有把這個數字顯示給一般使用者看，
所以程式最多只能做到「有貨 / 缺貨」的二分判斷，沒辦法顯示確切數量。



不需要另外找一個「隨時可買」的網頁來測試——直接用內建的**測試通知功能**最快：

1. 到倉庫的 `Actions` 分頁，點左側 `Weverse Monitor`
2. 點右上角 `Run workflow` 按鈕，會跳出一個小視窗
3. 把裡面的 `test_notification` 打勾（勾選為 `true`）
4. 點綠色的 `Run workflow` 執行
5. 等大約 10~30 秒，去檢查你的 Discord 頻道 / Telegram / 信箱有沒有收到一則寫著
   「✅ 這是一則測試訊息」的通知

哪個管道有填設定（Secrets），這次測試就會發那個管道的訊息；沒填的管道會直接跳過，
並在 log 裡顯示「未設定 xxx，略過測試」，這樣你可以清楚知道哪些管道還沒設定成功。

如果某個管道該收到卻沒收到，去看這次執行紀錄裡「執行一次檢查（或測試通知）」步驟的 log，
裡面通常會有 `xxx 通知失敗` 或 `xxx 通知發生例外` 的錯誤訊息，把那段訊息貼給我，我可以幫你排查
（常見原因：Discord Webhook 網址複製錯誤或漏字、Telegram 忘記先跟機器人說過話導致抓不到
Chat ID、Gmail 用了一般登入密碼而不是應用程式專用密碼）。

確認三個通知管道都測試成功之後，之後排程執行時（每 15 分鐘的 `*/15 * * * *`）就不用再手動
勾選 `test_notification` 了，它預設是關閉的，會自動去真的檢查商品頁面。


> 補充：GitHub Actions 的排程（cron）不保證分秒不差，尖峰時段可能會延遲幾分鐘才觸發，
> 這是 GitHub 免費方案的正常現象，對「15 分鐘檢查一次」這種用途影響不大。
> 免費帳號（Public 倉庫無限制；Private 倉庫每月有 2000 分鐘額度）通常綽綽有餘，
> 因為每次執行只需要幾十秒。

### 其他持續運作的做法（進階，供參考）

如果不想用 GitHub，也可以把 `weverse_monitor.py`（方式 A 的無限迴圈版本）部署到：
- 一台一直開著的雲端主機 / VPS（例如小型雲端伺服器），用 `nohup` 或 `systemd` 常駐執行
- 免費的雲端 Python 執行平台（例如 PythonAnywhere 的排程工作 Scheduled Tasks）
- 家裡一台一直開機的小型電腦（例如 Raspberry Pi），設定 cron 每 15 分鐘執行一次

這些做法效果跟 GitHub Actions 類似，只是設定步驟不同，如果你有特別偏好的平台可以再告訴我，
我可以幫你寫對應的部署步驟。

---

## 1. 安裝

```bash
cd weverse_monitor
pip install -r requirements.txt
cp .env.example .env
```

打開 `.env`，依照下面教學填入你要用的通知方式（可以同時填多個，不需要的留空）。

---

## 2. 三種通知方式設定教學

你不需要三種都設定，選一個或多個都可以，程式會自動偵測 `.env` 裡有填的欄位。

### 方法一：Discord（推薦，最簡單）

1. 開啟你的 Discord 伺服器，找一個你要接收通知的頻道。
2. 點頻道設定（齒輪圖示）→「整合 Integrations」→「Webhook」→「新增 Webhook」。
3. 幫這個 Webhook 取個名字（例如 `Weverse 通知`），點「複製 Webhook URL」。
4. 把複製到的網址貼到 `.env` 的：
   ```
   DISCORD_WEBHOOK_URL=貼上你複製的網址
   ```

### 方法二：Telegram

1. 在 Telegram 搜尋 `@BotFather`，傳送 `/newbot`，依照指示幫機器人取名字，
   完成後 BotFather 會給你一組 **Bot Token**（長得像 `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`）。
2. 把這組 Token 貼到 `.env` 的 `TELEGRAM_BOT_TOKEN`。
3. 在 Telegram 找到你剛建立的機器人，點「開始 Start」隨便傳一句話給它（例如 `hi`）。
4. 用瀏覽器打開下面網址（把 `<TOKEN>` 換成你的 Token）：
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   在回傳的 JSON 裡找到 `"chat":{"id": 數字, ...}`，那組數字就是你的 **Chat ID**。
5. 把這組數字貼到 `.env` 的 `TELEGRAM_CHAT_ID`。

### 方法三：Gmail

Gmail 帳號無法直接用密碼登入寄信，需要建立「應用程式專用密碼 App Password」：

1. 前往你的 Google 帳號設定，開啟「兩步驟驗證 (2-Step Verification)」（若尚未開啟必須先開啟）。
2. 到「應用程式密碼 App Passwords」頁面（在 Google 帳號的安全性設定裡搜尋「應用程式密碼」）。
3. 建立一組新的應用程式密碼（名稱隨意，例如 `weverse-monitor`），Google 會給你一組 16 碼密碼。
4. 把資訊填入 `.env`：
   ```
   GMAIL_ADDRESS=你的Gmail帳號@gmail.com
   GMAIL_APP_PASSWORD=剛剛產生的16碼應用程式密碼（不是你的Gmail登入密碼）
   GMAIL_TO=要收到通知的信箱（可以跟GMAIL_ADDRESS一樣，寄給自己）
   ```

---

## 3. 執行方式

### 方式 A：直接執行，讓程式自己每 15 分鐘檢查一次（最簡單）

```bash
python3 weverse_monitor.py
```

這樣程式會在終端機裡持續運作，每 15 分鐘檢查一次。如果關掉終端機，程式就會停止，
所以建議在背景執行（Mac/Linux）：

```bash
nohup python3 weverse_monitor.py > /dev/null 2>&1 &
```

或用 `tmux` / `screen` 開一個獨立的終端機視窗來跑。

### 方式 B：用排程工具定時執行一次（適合不想讓程式一直開著）

如果你不想讓程式一直運行，可以把 `weverse_monitor.py` 裡的 `main()` 迴圈拿掉，
改成每次執行只跑一次 `run_once()`，然後交給系統排程器每 15 分鐘啟動一次：

- **Mac / Linux（crontab）：**
  ```bash
  crontab -e
  ```
  加入這一行（每 15 分鐘執行一次）：
  ```
  */15 * * * * cd /完整路徑/weverse_monitor && /usr/bin/python3 weverse_monitor.py >> cron.log 2>&1
  ```

- **Windows（工作排程器 Task Scheduler）：**
  開啟「工作排程器」→「建立基本工作」→ 觸發條件選「每天」，並設定重複間隔為 15 分鐘 →
  動作選「啟動程式」，程式路徑填 `python.exe`，引數填 `weverse_monitor.py` 的完整路徑。

---

## 4. 重要限制與後續調整

- 這支程式用最直接的方式（下載網頁原始碼）去偵測 `SOLD OUT` / `ADD TO CART` / `PURCHASE`
  等關鍵字。Weverse Shop 是用 Next.js 打造的網站，如果你實際跑幾次後發現：
  - 5 款商品的名稱都抓不到（log 會顯示警告訊息），
  - 或者狀態判斷錯亂（例如把 A 商品的狀態誤判成 B 商品的狀態），

  代表款式選單那塊內容需要瀏覽器執行 JavaScript 才會出現，或是商品名稱在網頁原始碼裡
  彼此距離太近導致判讀範圍互相干擾。目前的判斷邏輯已經改成「以相鄰商品名稱的出現位置
  當作判讀邊界」（而不是固定字元數的視窗），減少誤判成隔壁商品狀態的機率，但這仍然是
  文字關鍵字比對的heuristic 做法，**沒辦法保證 100% 準確**。如果修正後還是持續判斷錯誤，
  代表這個網站的結構就是沒辦法單純用文字比對可靠判斷，請改用下方註解裡寫的
  **Playwright 版本**（用真的無頭瀏覽器去渲染、甚至直接切換下拉選單讀取每個選項
  真正的按鈕文字），需要額外安裝：
  ```bash
  pip install playwright
  playwright install chromium
  ```
  如果你想要，我也可以直接幫你把 Playwright 版本完整寫出來（會需要你用瀏覽器的
  開發人員工具 F12，找一下實際下拉選單的 HTML 結構，並回報給我，這樣我才能寫出
  對應這個網站真正結構的程式碼）。


- 程式會把每次檢查結果記錄在 `monitor.log`，方便你確認程式有沒有正常運作、
  以及每個商品目前被判斷成什麼狀態。

- `state.json` 用來記住「上一次的狀態」，避免每 15 分鐘重複發送同樣的通知。
  如果想要重置（例如想重新收到一次通知），把 `state.json` 刪掉即可。
