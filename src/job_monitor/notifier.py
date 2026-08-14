from __future__ import annotations

import html
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import MatchedJob, MatchResult, ParsedJob


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def source_age_days(posted_at: datetime | None, reference_at: datetime | None = None) -> int | None:
    if posted_at is None:
        return None
    reference = _as_utc(reference_at or datetime.now(UTC))
    posted = _as_utc(posted_at)
    return max(0, int((reference - posted).total_seconds() // 86400))


def render_freshness(
    posted_at: datetime | None, first_seen_at: datetime, *, compact: bool = False
) -> str:
    first_seen = _as_utc(first_seen_at)
    if posted_at is None:
        if compact:
            return f"first seen {first_seen.date().isoformat()}; source date unknown"
        return f"首次發現：{first_seen.date().isoformat()}；來源日期：未提供"

    posted = _as_utc(posted_at)
    age = source_age_days(posted, first_seen)
    if compact:
        return f"first seen {first_seen.date().isoformat()}; source {age}d old"
    return f"首次發現：{first_seen.date().isoformat()}；來源日期：{posted.date().isoformat()}（約 {age} 天前）"


def render_job_message(
    company_name: str, job: ParsedJob, result: MatchResult, first_seen_at: datetime
) -> str:
    badge = "🔥 強烈推薦" if result.tier == "strong" else "✅ 符合"
    if result.bucket == "stretch":
        badge = "🪜 " + badge
    reasons = "、".join(result.reasons) or "規則配對"
    gaps = "、".join(result.gaps) or "無明顯缺口"
    freshness = render_freshness(job.raw.posted_at, first_seen_at)
    return (
        f"{badge} | {html.escape(str(result.profile))} | {result.score:.0%}\n"
        f"<b>{html.escape(company_name)} - {html.escape(job.raw.title)}</b>\n"
        f"📍 {html.escape(job.raw.location_raw or '未提供')}\n"
        f"命中：{html.escape(reasons)}\n"
        f"缺口：{html.escape(gaps)}\n"
        f"新鮮度：{html.escape(freshness)}\n"
        f'<a href="{html.escape(str(job.raw.url), quote=True)}">官方申請連結</a>'
    )


def render_failure_alert(company_name: str, failures: int, error: str) -> str:
    return (
        "⚠️ 來源連續失敗\n"
        f"<b>{html.escape(company_name)}</b>\n"
        f"連續失敗：{failures} 次\n"
        f"錯誤：{html.escape(error[:1000])}"
    )


def render_run_summary(
    *,
    run_key: str,
    stats: dict[str, int],
    errors: list[dict[str, str]],
    matched_jobs: list[MatchedJob],
    zero_job_sources: list[str],
    max_matches: int = 8,
) -> str:
    fresh_matches = sum(1 for item in matched_jobs if item.is_new)
    lines = [
        f"📊 Job Radar TW｜職缺雷達 Daily Summary - {html.escape(run_key)}",
        f"來源：{stats.get('sources_succeeded', 0)}/{stats.get('sources_attempted', 0)} 成功",
        f"抓到職缺：{stats.get('jobs_fetched', 0)}",
        f"新職缺：{stats.get('jobs_new', 0)}；內容變更：{stats.get('jobs_changed', 0)}；關閉：{stats.get('jobs_closed', 0)}",
        f"符合門檻：{stats.get('matches', 0)}；逐筆通知：{stats.get('notifications', 0)}",
        f"本次新匹配：{fresh_matches}；逐筆候選：{stats.get('immediate_candidates', 0)}；"
        f"限量保留：{stats.get('notifications_suppressed', 0)}；"
        f"待重試：{stats.get('notifications_pending', 0)}",
    ]

    if zero_job_sources:
        lines.append("")
        lines.append("⚠️ 已驗證來源本次抓到 0 筆：")
        lines.extend(f"- {html.escape(name)}" for name in zero_job_sources[:8])

    if errors:
        lines.append("")
        lines.append("⚠️ 來源錯誤：")
        for item in errors[:8]:
            company = html.escape(item.get("company", "unknown"))
            error = html.escape(item.get("error", "")[:160])
            lines.append(f"- {company}: {error}")

    lines.append("")
    if matched_jobs:
        ordered = sorted(matched_jobs, key=lambda match: match.result.score, reverse=True)
        target_jobs = [item for item in ordered if item.result.bucket == "target"]
        stretch_jobs = [item for item in ordered if item.result.bucket == "stretch"]
        shown = 0
        if target_jobs:
            lines.append("本次符合職缺：")
        for item in target_jobs[:max_matches]:
            marker = "NEW " if item.is_new else ""
            freshness = render_freshness(item.job.raw.posted_at, item.first_seen_at, compact=True)
            lines.append(
                f"- {marker}{html.escape(item.company_name)} - "
                f'<a href="{html.escape(str(item.job.raw.url), quote=True)}">{html.escape(item.job.raw.title)}</a> '
                f"({html.escape(str(item.result.profile))} {item.result.score:.0%}, "
                f"{html.escape(item.job.raw.location_raw or '未提供')}; {html.escape(freshness)})"
            )
            shown += 1
        remaining = max_matches - shown
        if stretch_jobs and remaining > 0:
            lines.append("🪜 延伸職缺（高於你目前職級，可作為挑戰）：")
        for item in stretch_jobs[:remaining]:
            marker = "NEW " if item.is_new else ""
            freshness = render_freshness(item.job.raw.posted_at, item.first_seen_at, compact=True)
            lines.append(
                f"- {marker}{html.escape(item.company_name)} - "
                f'<a href="{html.escape(str(item.job.raw.url), quote=True)}">{html.escape(item.job.raw.title)}</a> '
                f"({html.escape(str(item.result.profile))} {item.result.score:.0%}, "
                f"{html.escape(item.job.raw.location_raw or '未提供')}; {html.escape(freshness)})"
            )
            shown += 1
        if len(matched_jobs) > shown:
            lines.append(
                f"...另有 {len(matched_jobs) - shown} 筆，請到 Supabase match_results 查看完整清單。"
            )
    else:
        lines.append("本次沒有符合門檻的新/變更職缺。")

    if not errors and not zero_job_sources:
        lines.append("")
        lines.append("系統狀態：正常")

    return "\n".join(lines)


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current.rstrip())
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _send_chunk(self, chunk: str) -> None:
        response = await self.client.post(
            self.url,
            json={
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()

    async def send(self, text: str) -> None:
        for chunk in split_message(text):
            try:
                await self._send_chunk(chunk)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Telegram API returned HTTP {exc.response.status_code}"
                ) from None
            except httpx.HTTPError:
                raise RuntimeError("Telegram API request failed") from None
