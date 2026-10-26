"""Anthropic 호출 래퍼. 소량(파일럿)은 직접 호출, 전체 실행은 Message Batches API(비용 50%)로 보낸다.

분류 결과는 output_config.format의 JSON 스키마로 받는다. 같은 호출에서 오류가 난 항목은 None으로 돌려주므로,
호출하는 쪽은 None인 항목을 저장하지 않고 다음 실행에서 다시 시도하면 된다.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import anthropic
from dotenv import dotenv_values

from . import ROOT

MODEL = "claude-haiku-5-5"
DIRECT_CHUNK = 100   # 직접 호출 시 저장 단위
BATCH_CHUNK = 2000   # Batches 한 건당 요청 수. 요청 본문 256MB 제한과, 중단 시 잃는 작업량을 함께 고려한 값이다.
POLL_SECONDS = 30


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=dotenv_values(ROOT / ".env")["ANTHROPIC_API_KEY"], max_retries=5)


def _params(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    return {
        "model": MODEL,
        "max_tokens": max_tokens,  # Haiku 5.5는 기본으로 사고하며, 사고 토큰도 max_tokens에 포함된다
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    }


def _parse(message) -> dict | None:
    if message.stop_reason != "end_turn":  # refusal, max_tokens 등
        return None
    text = next((b.text for b in message.content if b.type == "text"), None)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _direct(client, items: dict[str, str], params: Callable[[str], dict]) -> dict[str, dict | None]:
    def one(key: str) -> dict | None:
        try:
            return _parse(client.messages.create(**params(items[key])))
        except anthropic.APIError as e:
            print(f"llm: {key} failed ({type(e).__name__})")
            return None

    with ThreadPoolExecutor(8) as pool:
        return dict(zip(items, pool.map(one, items)))


def _batch(client, items: dict[str, str], params: Callable[[str], dict]) -> dict[str, dict | None]:
    batch = client.messages.batches.create(
        requests=[{"custom_id": key, "params": params(user)} for key, user in items.items()]
    )
    print(f"llm: submitted batch {batch.id} ({len(items)} requests)")  # 중단돼도 이 ID로 콘솔에서 결과를 받을 수 있다
    while (batch := client.messages.batches.retrieve(batch.id)).processing_status != "ended":
        time.sleep(POLL_SECONDS)
    results: dict[str, dict | None] = {key: None for key in items}
    for res in client.messages.batches.results(batch.id):  # 순서가 보장되지 않으므로 custom_id로 매칭한다
        if res.result.type == "succeeded":
            results[res.custom_id] = _parse(res.result.message)
    return results


def run(items: dict[str, str], *, system: str, schema: dict, save: Callable[[dict[str, dict]], None],
        max_tokens: int = 2000, use_batch_api: bool = False) -> tuple[int, int]:
    """items: {키: 사용자 메시지}. 단위(chunk)마다 성공한 결과만 save()에 넘기고 (성공 수, 실패 수)를 반환한다."""
    client = _client()
    size = BATCH_CHUNK if use_batch_api else DIRECT_CHUNK
    keys, ok, bad = list(items), 0, 0
    for i in range(0, len(keys), size):
        chunk = {k: items[k] for k in keys[i : i + size]}
        results = (_batch if use_batch_api else _direct)(
            client, chunk, lambda user: _params(system, user, schema, max_tokens)
        )
        good = {k: v for k, v in results.items() if v is not None}
        save(good)
        ok, bad = ok + len(good), bad + len(results) - len(good)
        print(f"llm: {ok + bad}/{len(keys)} done ({bad} failed)")
    return ok, bad
