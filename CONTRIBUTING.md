# 參與貢獻

歡迎修正錯誤、補測試、改善文件，或加入新的官方職缺來源。先開 issue 說明使用情境通常會比較省時間；小型修正可直接送 pull request。

## 開發環境

需要 Python 3.12 與 [`uv`](https://docs.astral.sh/uv/)。

```text
uv sync --extra dev
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

macOS／Linux：

```bash
cp .env.example .env
```

多數測試不需要真實 secrets。請勿提交 `.env`、bot token、database URL、service role key、履歷或任何個人求職資料。

送出 PR 前請執行：

```text
uv run ruff check .
uv run pytest -q
uv run monitor validate-config --strict
```

## 修改來源

只接受企業官方公開 API、ATS endpoint，或允許存取的 career page。不要加入登入繞過、CAPTCHA 規避、IP 輪換，或以聚合網站冒充官方來源的作法。

調整 `config/companies.yml` 時：

- slug 必須唯一。
- endpoint 尚未實際驗證前保持 `enabled: false` 與 `source_verified: false`。
- 用 `uv run monitor sources verify --company COMPANY_SLUG --status all` 實測。
- 解析器變更需補上成功、空結果與異常回應的測試。

不要把短暫抓取成功當成來源永遠穩定；PR 請附上 ATS 類型、官方 careers URL 與驗證方式，不要貼敏感回應內容。

## 修改匹配規則

- 公開偏好放在 `config/preferences.yml`，profile 詞彙與權重放在 `config/profiles.yml`。
- 每組 profile 權重總和必須為 `1.0`。
- 新的硬性排除條件需要測試通過與不應被誤擋的反例。
- 預設行為應讓不同使用者都能調整，不要加入姓名、特定履歷內容或只服務單一帳號的條件。

## Pull request

請讓一個 PR 專注處理一件事，並在說明中列出：

- 問題與採用的行為。
- 測試方式。
- 是否更動 config、migration、secrets 或通知格式。
- 若來源需網路驗證，驗證日期與結果筆數。

送出貢獻即表示你同意以本 repository 的 MIT License 提供該內容。
