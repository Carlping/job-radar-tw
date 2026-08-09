# Job Radar TW｜職缺雷達

[繁體中文](README.md) · [English](README.en.md)

[![Tests](https://github.com/Carlping/job-radar-tw/actions/workflows/test.yml/badge.svg)](https://github.com/Carlping/job-radar-tw/actions/workflows/test.yml)

定時查看公司官網的職缺，把結果存進 Supabase，再用 Telegram 通知你。排程跑在 GitHub Actions，不需要自己養一台伺服器。

目前支援 Greenhouse、Lever、Ashby、SmartRecruiters、Workday，以及帶有 JSON-LD 的公開職缺頁。這個專案只讀公開來源，不會代投履歷，也不是秒級更新服務。

Cloudflare 不在基本部署流程裡。只有想把 dashboard 放上網時，才需要看[選用的 Cloudflare 文件](docs/cloudflare-deploy.md)。

## 部署前要準備什麼

- 一個 GitHub 帳號。
- 一個 [Supabase](https://supabase.com/) project。
- 一個 Telegram bot，以及要收通知的 chat ID。

只用 GitHub Actions 的話，不必先安裝 Python。整套設定通常都能在瀏覽器裡完成。

## 部署

### 1. 複製 repository

在 GitHub 頁面按 **Use this template → Create a new repository**。如果沒有 template 按鈕，也可以按 **Fork**。

建議把新 repository 設成 private，尤其是打算放履歷時。公開 repository 的 fork 也會是公開的；需要私有版本時，請用 template，或 clone 後推到自己的 private repository。Token、資料庫網址與密碼一律不要寫進檔案。

Fork 完成後，先到 **Actions** 分頁啟用 workflows。[GitHub 預設不會在 public fork 執行 workflow](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflows-in-forked-repositories)。

### 2. 改成自己的搜尋條件

先處理這三個檔案：

- `config/preferences.yml`：地點、是否接受 remote、citizenship／clearance 與 seniority 排除條件。
- `config/companies.yml`：要監控的公司與官方 ATS endpoint；至少保留一家公司 `enabled: true`。
- `config/profiles.yml`：職稱、領域、技能、權重與通知門檻。Profile 名稱可自行決定，但必須和 `companies.yml` 裡的 `profiles` 對上。

可以直接用 GitHub 的檔案編輯器修改並 commit。`profiles.yml` 的每組 `weights` 加總必須是 `1.0`。`source_verified` 代表 endpoint 已經實際抓取成功，不要只因為想監控該公司就改成 `true`。

履歷比對是選用功能。最安全的做法是參考 `config/resume.example.md`，把自己的純文字／Markdown 內容存成 repository secret `RESUME_TEXT`，不要提交含姓名、電話或地址的履歷。本機使用者也可以建立自己的檔案，再於 `.env` 設定 `RESUME_PATH`。

### 3. 建立 Supabase database

1. 在 Supabase 建立 project，並保存 database password。
2. 在 project 頁面按 **Connect**，選 **Session pooler**，複製 port `5432` 的 connection string。
3. 把字串裡的密碼 placeholder 換成真正的 database password。若密碼含 `@`、`:`、`/` 等 URI 特殊字元，需先做 percent-encoding。

GitHub-hosted runner 常是 IPv4 環境，因此建議使用 [Session pooler](https://supabase.com/docs/guides/database/connecting-to-postgres)，不要使用預設只有 IPv6 的 direct connection。格式大致如下：

```text
postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
```

稍後的 Setup workflow 會建立資料表。想手動初始化時，也可以把 `migrations/001_initial.sql` 貼到 Supabase **SQL Editor** 執行。

### 4. 建立 Telegram bot

1. 打開 [@BotFather](https://t.me/BotFather)，輸入 `/newbot`，照指示取得 bot token。
2. 打開剛建立的 bot，按 **Start** 或傳送 `/start`。Bot 在你先開啟對話前不能主動傳訊息。
3. 在瀏覽器開啟下列網址，把 `{TOKEN}` 換成 bot token：

   ```text
   https://api.telegram.org/bot{TOKEN}/getUpdates
   ```

4. 在最新一筆回傳中找 `message.chat.id`，這個數字就是 chat ID。若 `result` 是空的，對 bot 再傳一則訊息後重新整理。群組 chat ID 通常是負數。

Token 等同 bot 密碼。如果不小心貼到公開頁面，請立刻回到 BotFather 撤銷並重發。

### 5. 加入 GitHub Actions secrets

到自己的 repository：**Settings → Secrets and variables → Actions → New repository secret**，加入：

| Name | Value |
| --- | --- |
| `DATABASE_URL` | Supabase Session pooler connection string |
| `TELEGRAM_BOT_TOKEN` | BotFather 給的 token |
| `TELEGRAM_CHAT_ID` | 上一步找到的 chat ID |

基本監控不需要 `SUPABASE_SERVICE_ROLE_KEY`。它只用於選配的 Cloudflare dashboard，而且應存放在 Cloudflare，不是提交到 GitHub。

`RESUME_TEXT` 與 `OPENAI_API_KEY` 都是選填 secret。沒有履歷或 OpenAI key，規則評分仍可正常執行。

需要調整預設值時，再到同一頁的 **Variables** 加入：

- 排程：`MONITOR_TIMEZONE`、`MONITOR_HOUR`。
- 簽證條件：`VISA_SPONSORSHIP_REQUIRED`。
- 通知：`IMMEDIATE_NOTIFICATION_MIN_SCORE`、`IMMEDIATE_NOTIFICATION_MAX_SOURCE_AGE_DAYS`、`IMMEDIATE_NOTIFICATION_MAX_PER_RUN`、`DAILY_SUMMARY_MAX_MATCHES`。
- LLM：`LLM_ENABLED`、`OPENAI_MODEL`；API key 放在 secret `OPENAI_API_KEY`。

若改排程時區或時間，也要同步修改 `.github/workflows/monitor.yml` 的 `schedule`，否則 workflow 可能在監控時段外觸發後直接跳過。

### 6. 跑一次 Setup

打開 **Actions → Setup Job Radar TW → Run workflow**，讓 `send_test_message` 保持勾選。這個 workflow 會：

- 檢查設定檔；
- 初始化 Supabase 資料表並啟用 RLS；
- 傳一則 Telegram 測試訊息。

三項都成功後再跑正式監控。若看不到 **Run workflow**，確認 workflow 已在 repository 的 default branch，且 Actions 已啟用。

### 7. 啟動 Monitor

打開 **Actions → Job Radar TW → Run workflow**。第一次建議先讓 `backfill` 保持關閉。

第一次掃描會建立 baseline：當下已存在的職缺會寫入資料庫並出現在 Daily Summary，但不會逐筆洗版。之後新出現的高分職缺才會依規則逐筆推播。

如果想補發 baseline 裡尚未通知的職缺，再手動執行一次並勾選 `backfill`。Backfill 會補送所有達到 profile 門檻的現有職缺，不要求是新職缺、強匹配或在新鮮度期限內；通知去重與單次數量上限仍然有效。

超過上限的項目會留在 outbox，之後的 run 會繼續傳送。想加快進度也可以再手動執行，留空的 `run_key` 會自動產生。Backfill 不會到網站追溯已下架的歷史職缺。

確認手動執行正常後就不用再操作。預設排程約在每天美東時間 20:00 開始，workflow 會安排數次備援觸發；`run_key` 會略過已成功或仍在執行的同日 run，失敗或逾時的 run 則可由下一次觸發重試。GitHub 排程可能延遲，不適合當成即時告警。

## 日常查看

每次真正完成的 run 都會傳一則 Daily Summary，包含來源成功率、抓到的職缺數、新增／變更／關閉數量，以及符合門檻的清單。強匹配的新職缺另有逐筆通知。

完整資料可在 Supabase 查看：`jobs` 是職缺與狀態，`match_results` 是評分，`source_runs` 是執行紀錄，`notifications` 用來避免重複推播。暫時傳送失敗或超過單次上限的逐筆通知會留在 `notification_outbox`，等下一次 run 繼續。

通知裡的 `first seen` 是 monitor 第一次看見職缺的日期；`source N d old` 則來自官網發布時間。

Outbox 讓一般重試不會重複推播。不過 Telegram Bot API 沒有 idempotency key；極少數情況下，若 Telegram 已收下訊息、runner 卻在寫回成功狀態前中止，下一次 run 可能再送一次。

## 本機先試跑（選用）

需要 Python 3.12 與 [`uv`](https://docs.astral.sh/uv/)。先 clone 自己的 repository，再執行：

```text
uv sync --extra dev
```

本機若要開啟 LLM enrichment，改用 `uv sync --extra dev --extra llm`。

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

macOS／Linux：

```bash
cp .env.example .env
```

`.env` 已被 `.gitignore` 排除。填入需要的值後，可先測一家公司；`dry-run` 不會寫資料庫，也不會送 Telegram：

```text
uv run monitor validate-config
uv run monitor sources list --status enabled
uv run monitor dry-run --company COMPANY_SLUG
```

常用指令：

```text
uv run monitor validate-config
uv run monitor dry-run [--company COMPANY_SLUG]
uv run monitor run [--company COMPANY_SLUG] [--backfill] [--run-key KEY]
uv run monitor sources list --status all
uv run monitor sources verify --company COMPANY_SLUG --status all
uv run monitor sources candidates
uv run monitor init-db
uv run monitor doctor --send-telegram
uv run monitor web
```

新加入的來源應先保持 disabled。確認 `uv run monitor sources verify --company COMPANY_SLUG --status all` 抓得到資料後，再加上 `--promote`；通過驗證的項目才會被寫成 `enabled: true` 與 `source_verified: true`。

本機正式執行需要 database 與 Telegram 設定。每次手動測試請換新的 `--run-key`，否則系統會把它視為重複 run。

## 常見問題

**`Network is unreachable` 或連不上 `db.PROJECT_REF.supabase.co`**

多半用了 Supabase direct connection。改用 **Session pooler、port 5432**。

**`password authentication failed`**

確認填的是 database password，不是 Supabase 帳號密碼；connection string 裡的特殊字元也要正確編碼。

**`Telegram ... required` 或 Setup 沒收到測試訊息**

檢查 secret 名稱是否完全一致、是否先對 bot 傳過 `/start`，以及 `TELEGRAM_CHAT_ID` 是否抄到其他對話。

**`matches > 0`，但 `notifications = 0`**

第一次 baseline、本次只有舊職缺，或新職缺沒有通過即時通知的強匹配／新鮮度規則，都會出現這個結果。Daily Summary 仍會保留符合門檻的清單；若要補送 baseline，可手動勾選 `backfill`。

**`skipped_reason: duplicate_run_key`**

這通常表示同一個 key 已成功，或另一個 run 還在執行，是正常的備援排程去重。若是手動執行，換一個 `run_key`。

**Fork 後一直沒有排程**

先到 Actions 啟用 workflow。GitHub 也可能停用長期沒有活動的 public repository 排程；到 **Actions → Job Radar TW → Enable workflow** 重新開啟。

**某家公司突然抓到 0 筆或連續失敗**

ATS endpoint 可能改版。先停用該來源，再以 `uv run monitor sources verify --company COMPANY_SLUG --status all` 檢查，不要用繞過登入、CAPTCHA 或網站限制的方式修復。

## Dashboard（選用）

只想收 Telegram 不需要部署 dashboard。本機查看可執行：

```text
uv run monitor web
```

再開啟 `http://127.0.0.1:8080`。如需私人網址，可參考 [Cloudflare Pages 部署](docs/cloudflare-deploy.md)；該版本會使用 Supabase service role，務必加上 Cloudflare Access。

## 專案文件

- [功能規格](docs/spec.md)
- [貢獻方式](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [MIT License](LICENSE)
