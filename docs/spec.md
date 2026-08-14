# Job Radar TW｜職缺雷達功能規格

## 目的

定期讀取公司官方招聘來源，保存職缺版本，依使用者設定評分，並把執行摘要與值得優先查看的新職缺送到 Telegram。系統以每日監控為目標，不承諾即時送達。

## 執行方式

- 正式排程：GitHub Actions。
- 資料庫：PostgreSQL；公版部署以 Supabase 為預設。
- 通知：Telegram Bot API。
- 本機 dashboard：FastAPI，僅綁定 `127.0.0.1`。
- 網路 dashboard：Cloudflare Pages／Functions，選用且必須另設存取控制。

核心監控不依賴 Cloudflare、OpenAI API 或常駐 server。

## 設定邊界

- `config/companies.yml` 保存公司、ATS 類型、endpoint、啟用狀態與使用的 profiles。
- `config/profiles.yml` 保存一組或多組匹配 profile 的關鍵字、權重與門檻。名稱由設定檔決定，每組權重總和必須為 `1.0`。
- `config/preferences.yml` 保存地點、remote、citizenship／clearance 與 seniority 偏好。
- `config/candidate.yml`（選用）保存候選人的年資、職級、學位與帶人經驗；未設定時不套用職級／經驗合理性檢查。
- `RESUME_TEXT` secret 或本機純文字／Markdown 履歷可補充 skills 與 domain terms；沒有履歷時只使用 profile 規則。
- LLM enrichment 預設關閉。啟用後只處理規則分數落在模糊區間，且 seniority、remote 或身份條件有歧義的職缺；呼叫失敗時回到規則結果。

## 來源

支援以下公開來源：

- Greenhouse
- Lever
- Ashby
- SmartRecruiters
- Workday 公開 endpoint
- career page 的 JSON-LD `JobPosting`

採用共用 JSON 讀取器的來源遇到暫時性錯誤時，最多嘗試三次。全域抓取併發上限預設為五，同一網域序列執行。單一來源失敗不得阻斷其他公司；已驗證來源若回傳零筆，需在摘要中標示。

不支援需要登入的來源，也不採用 CAPTCHA 規避、IP 輪換，或其他繞過網站限制的作法。

## 職缺生命週期

1. 以 `(company, external_job_id)` 識別職缺；來源未提供 ID 時，以公司、職稱、地點與 canonical URL 建立穩定替代 ID。
2. URL 會移除 query 與 fragment，再保存為 canonical URL。
3. 分開保存來源發布時間 `source_posted_at` 與系統首次發現時間 `first_seen_at`。
4. 內容 hash 改變時建立新版本並重新評分；一般 run 會跳過未變更職缺，backfill 則可重新載入現有結果以補送通知。
5. 一個職缺在同公司兩次成功完整掃描都未出現後，才標記為 `closed`。

## 匹配與通知

每家公司可指定一或多個 profiles。規則評分會考慮 title、domain、skills、location 與 seniority；profile 的 `threshold` 決定是否符合，`strong_threshold` 決定是否為強匹配。啟用 candidate profile 後，職缺另分為 target、stretch 與 unrealistic，且逐筆即時通知只發送 target。

一般 run 的 Daily Summary 會列出本次新建或內容變更後仍符合門檻的職缺；backfill 會列出本次重新檢查的符合項目。一般逐筆通知另外要求：

- 是新職缺；
- 達到強匹配與即時通知最低分數；
- 通過 preferences 中的硬性排除條件；
- 來源日期未超過新鮮度上限；
- 尚未以相同 job／profile／content hash 通知過。

候選過多時依分數排序，只送出單次上限內的項目。其餘候選留在 outbox，由之後的 run 繼續處理。

決定發送時，系統會在保存職缺版本與評分的同一筆資料庫交易裡，把訊息放進 outbox，再呼叫 Telegram。傳送失敗時不會標成已通知；後續 run 仍會重試，即使職缺內容沒有再次改變。每則待送訊息只交給一個 run；如果職缺已關閉或內容已換版，就捨棄舊訊息。

Telegram Bot API 不提供 idempotency key，因此這裡採「至少傳送一次」（at-least-once）：正常重試由 outbox 與 notification key 去重；若 Telegram 已接受訊息、runner 卻在確認寫回前中止，極少數情況仍可能重複一次。

### Baseline

某家公司第一次完成完整掃描前，當下抓到的職缺都屬於 baseline。這個狀態會保存在資料庫，所以首跑中斷後重試也不會把既有職缺誤當成新通知。預設會保存與評分，也會列入 Daily Summary，但不逐筆通知，避免剛部署就洗版。

手動 backfill 可補發 baseline 中達到 profile 門檻（`eligible: true`）且尚未通知的職缺。這個模式不要求 strong、new 或 freshness，但仍受通知去重與單次數量上限限制；超過上限的項目留在 outbox，後續 run 會繼續補送，也可再次手動執行以加快進度。Backfill 不負責擷取來源已下架的歷史資料。

## 排程與冪等

- 預設目標時間為每天 20:00 `America/New_York`。
- GitHub workflow 以多個錯開整點的 cron 作為備援；延遲送達的排程可在次日上午以前沿用前一天的 daily key。
- `source_runs.run_key` 唯一。相同 key 已成功或仍在執行中時，後續觸發回傳 `duplicate_run_key`；失敗、部分失敗或超過逾時上限的 run，可由後續觸發重新取得執行權。
- 手動 workflow 未指定 key 時，使用 GitHub run ID 與 attempt 產生唯一 key。
- workflow 上限 60 分鐘，monitor 指令於 45 分鐘中止，保留發送失敗通知的時間。

## 資料與安全

- 所有通知連結必須指向來源提供的官方申請網址。
- migration 會為 monitor 使用的資料表啟用 RLS，且不建立 `anon`／`authenticated` policy。
- 核心資料路徑只需要 PostgreSQL `DATABASE_URL`；不需要 Supabase service role key。
- Cloudflare dashboard 透過 Pages Functions 使用 service role key；middleware 會依 `TEAM_DOMAIN` 與 `POLICY_AUD` 驗證 Access JWT，缺少設定時一律回傳 `503`。
- secrets 不得提交到 Git。公開 repository 不應保存含個資的履歷。

## 不在範圍內

- 自動投遞或替使用者登入招聘網站。
- 履歷生成或為每份職缺改寫履歷。
- 保證列出來源網站的所有歷史職缺。
- 秒級或嚴格準時的通知。
- 繞過來源的技術或政策限制。

## 驗收條件

- 沒有 database 或 Telegram secrets 時，`uv run monitor dry-run` 仍可抓取與評分。
- Setup workflow 能驗證設定、初始化空 database，並確認 Telegram 可送達。
- 已成功的 run key 不重複掃描；失敗或逾時的同 key run 可以重試。正常重試以 job／profile／content hash 去重；傳送成功但尚未寫回時 runner 中止的罕見情況允許重複一次。
- 任一來源失敗不阻斷其他來源，全部來源失敗時 workflow 回傳失敗。
- 第一次 baseline 預設不逐筆洗版；明確 backfill 可補發未通知項目。
- 職缺連續缺失一次不關閉，第二次成功掃描仍缺失才關閉。
- dashboard 不影響核心排程與 Telegram 通知。
