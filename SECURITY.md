# 安全政策

## 支援範圍

安全修正以 default branch 的最新版本為準。舊 commit、個人 fork 與自行修改的部署不另行維護。

## 回報問題

請優先使用 repository 的 **Security → Advisories → Report a vulnerability** 私密回報。若該功能沒有開啟，請建立一則不含攻擊程式、token、個資或完整 endpoint 的一般 issue，請維護者提供私密聯絡方式。

回報內容請包含受影響版本、重現條件、可能影響，以及你已採取的緩解方式。不要測試不屬於你的 Supabase、Telegram、GitHub 或 Cloudflare 帳號，也不要擷取其他使用者的資料。

這個專案目前沒有付費漏洞獎勵。維護者會盡量在七天內確認收到，但不保證修復時程。

## 部署時的安全邊界

- `DATABASE_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 與選填的 `RESUME_TEXT` 應存為 GitHub Actions secrets，本機設定則只放在被忽略的 `.env`。
- 核心 monitor 不需要 Supabase service role key。
- 選用的 Cloudflare dashboard 需要 service role key；它必須是 Cloudflare server-side secret，且整個 dashboard hostname（包含 `/api/*`）都要受 Cloudflare Access 保護。Functions middleware 另以 `TEAM_DOMAIN` 與 `POLICY_AUD` 驗證 Access JWT，缺少設定時會關閉存取。
- migration 會為資料表啟用 RLS，且不建立匿名 policy。不要為了讓 dashboard 方便存取而新增公開的 `anon` write policy。
- 公開 repository 不應包含履歷、姓名、地址、求職紀錄或公司內部資料。
- 只監控公開且允許存取的招聘來源。不要繞過登入、CAPTCHA、rate limit 或其他存取限制。

若 secret 曾出現在 commit、workflow log、issue 或聊天紀錄，僅刪除文字不夠；請立即更換對應的 database password、Telegram token 或 Supabase key，再清理歷史內容。
