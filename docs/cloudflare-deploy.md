# 選用：部署 Cloudflare dashboard

Telegram 與 GitHub Actions 不需要 Cloudflare。只有想用私人網址查看、分類與追蹤職缺時，才需要部署這個 dashboard。

專案裡有兩種 dashboard：

- `uv run monitor web`：在自己的電腦執行，預設只開放 `127.0.0.1`。
- `cloudflare/`：Cloudflare Pages 靜態頁面加 Pages Functions，透過 Supabase REST 讀寫資料。

Cloudflare 版本會接觸求職紀錄，並能更新申請階段。Repository 內的 middleware 會驗證 Cloudflare Access JWT；缺少 Access 設定時會回傳 `503`，不會在未驗證身份時開放 API。仍請照以下順序，先完成 Access policy，再放 Supabase service-role key。

## 前置條件

- 核心 monitor 已跑過 Setup，Supabase 裡已有資料表。
- repository 內有 `cloudflare/` 目錄。
- 一個 Cloudflare 帳號，並已啟用 Zero Trust。

## 1. 建立 Pages project

在 Cloudflare Dashboard 開啟 **Workers & Pages**，建立 Pages project 並連接 Git repository。選擇 production branch，使用以下 build 設定：

```text
Framework preset: None
Root directory: cloudflare
Build command: npm run build
Build output directory: public
```

Build script 會把 `src/job_monitor/web_static` 與安全 headers 複製到 `cloudflare/public`。`public/` 是產物，不需要 commit。

第一次部署因為還沒有 Access 環境變數，回傳 `503` 是預期行為。此時不要先放 Supabase key。

## 2. 建立 Cloudflare Access policy

在 Cloudflare Zero Trust 建立 [Access application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/)，範圍涵蓋整個 Pages hostname，例如 `PROJECT_NAME.pages.dev`。加入 allow policy，例如只允許指定 email。

記下兩個值：

- Team domain：格式為 `https://TEAM_NAME.cloudflareaccess.com`。
- Application Audience（AUD）Tag：在這個 Access application 的資料中可找到。

不要只保護首頁或漏掉 `/api/*`。前端和 API 應使用同一個受保護的 hostname。

## 3. 設定 Functions 環境變數

在 Pages project 的 **Settings → Environment variables**，先為 Production 加入：

```text
TEAM_DOMAIN=https://TEAM_NAME.cloudflareaccess.com
POLICY_AUD=APPLICATION_AUDIENCE_TAG
```

`TEAM_DOMAIN` 必須使用 `https://`，而且結尾不可有 `/`。`POLICY_AUD` 必須對應上一步建立的 Access application。

接著到 Supabase project settings 取得 Project URL 與 `service_role` key，再加入：

```text
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVICE_ROLE_KEY
```

Service role 會繞過 RLS，請把它標成 Cloudflare encrypted secret。它不可出現在前端 JavaScript、GitHub commit、`.env.example` 或公開 log。

Production 與 Preview 的變數彼此獨立。不要把 production service-role key 放進 Preview；若真的需要 Preview，請另外建立 Access application，並使用隔離的測試資料庫與 credentials。

## 4. 重新部署並驗證

更新環境變數後重新部署。先用無痕視窗開啟 Pages URL，應先看到 Cloudflare Access 登入，而不是 dashboard 資料。

通過 Access 後開啟：

```text
https://PROJECT_NAME.pages.dev/api/health
```

預期回應：

```json
{"status":"ok"}
```

最後回到首頁，確認職缺可讀取，並測試一次申請階段更新。

## 常見問題

**頁面回傳 `503 Cloudflare Access is not configured`**

`TEAM_DOMAIN` 或 `POLICY_AUD` 沒有設在目前部署環境。補上後重新部署。

**頁面回傳 `TEAM_DOMAIN is invalid`**

確認格式是 `https://TEAM_NAME.cloudflareaccess.com`，沒有 path 或尾端斜線。

**登入後 dashboard 沒有資料**

確認 monitor 已寫入 `jobs` 與 `match_results`，並檢查 `SUPABASE_URL`、service-role key 及 Pages 的 Production／Preview 環境是否一致。

**無痕視窗直接看到 dashboard**

Access application 沒有涵蓋這個 hostname，或 allow policy 過寬。先移除 Supabase key，再修正 Access 範圍。

## 本機檢查 build

需要 Node.js。Lockfile 已提交，可重現安裝：

```text
cd cloudflare
npm ci
npm run build
```

Access middleware 需要有效的 Cloudflare JWT，一般 localhost 不會自動取得。若只是要在本機查看真實資料，使用 `uv run monitor web` 比較直接；不要為了本機預覽把 production service-role key 寫進檔案。
