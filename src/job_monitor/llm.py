from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from .matching import LEVEL_BY_SENIORITY
from .models import ParsedJob, RemoteType, Seniority


class LLMFields(BaseModel):
    seniority: Seniority
    remote_type: RemoteType
    requires_citizenship: bool
    requires_clearance: bool
    domain_terms: list[str]


class LLMEnricher:
    def __init__(self, api_key: str, model: str):
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("LLM support is not installed; run `uv sync --extra llm`") from exc
        self.client: Any = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def enrich(self, job: ParsedJob) -> ParsedJob:
        schema = LLMFields.model_json_schema()
        response = await self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": (
                        "Extract only facts explicitly stated in this US job posting. "
                        "Do not infer citizenship or clearance requirements.\n\n"
                        f"Title: {job.raw.title}\nLocation: {job.raw.location_raw}\n"
                        f"Description: {job.raw.description_raw[:12000]}"
                    ),
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "job_fields",
                    "strict": True,
                    "schema": schema,
                }
            },
        )
        fields = LLMFields.model_validate(json.loads(response.output_text))
        job.seniority = fields.seniority
        job.level = LEVEL_BY_SENIORITY[fields.seniority]
        job.remote_type = fields.remote_type
        job.requires_citizenship = fields.requires_citizenship
        job.requires_clearance = fields.requires_clearance
        job.ambiguities.clear()
        return job
