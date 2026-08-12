#!/usr/bin/env python3
"""Qualified task contract v3: construction, fingerprinting and read-only validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


QUALIFIED_CONTRACT_VERSION = 3
QUALIFIED_SCHEMA = "qualified-task/v1"
CONTRACT_FILE = "contract.json"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contract_fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _entries(values: list[str], prefix: str, field: str) -> list[dict]:
    result = []
    for index, raw in enumerate(values, 1):
        text = str(raw).strip()
        if not text:
            raise ValueError(f"{field} 条目不能为空")
        result.append({"id": f"{prefix}{index}", field: text})
    if not result:
        raise ValueError(f"{field} 至少需要一条")
    return result


def build_qualified_contract(
    *,
    task_id: str,
    work_key: str,
    acceptance: list[str],
    outputs: list[str],
    evidence: list[str],
) -> dict:
    if not work_key:
        raise ValueError("contract v3 必须提供 work_key")
    return {
        "schema": QUALIFIED_SCHEMA,
        "contract_version": QUALIFIED_CONTRACT_VERSION,
        "task_id": task_id,
        "work_key": work_key,
        "acceptance": _entries(acceptance, "A", "criterion"),
        "outputs": _entries(outputs, "O", "description"),
        "evidence": _entries(evidence, "E", "requirement"),
    }


def _valid_entries(value: object, id_prefix: str, field: str) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            return False
        if item.get("id") != f"{id_prefix}{index}":
            return False
        if not isinstance(item.get(field), str) or not item[field].strip():
            return False
    return True


def load_and_validate_contract(task_dir: Path, status: dict) -> tuple[dict | None, str | None]:
    """Return (contract, error). Legacy v1/v2 intentionally returns (None, None)."""
    version = status.get("contract_version") or 0
    if version < QUALIFIED_CONTRACT_VERSION:
        return None, None
    if version != QUALIFIED_CONTRACT_VERSION:
        return None, f"unsupported qualified contract_version={version!r}"
    if not status.get("work_key") or not status.get("root_work_id"):
        return None, "v3 requires work_key and root_work_id"
    path = task_dir / CONTRACT_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"contract.json unreadable: {exc}"
    if not isinstance(value, dict):
        return None, "contract.json must be an object"
    if value.get("schema") != QUALIFIED_SCHEMA or value.get("contract_version") != version:
        return None, "contract schema/version mismatch"
    if value.get("task_id") != task_dir.name or value.get("work_key") != status.get("work_key"):
        return None, "contract task/work identity mismatch"
    if not _valid_entries(value.get("acceptance"), "A", "criterion"):
        return None, "acceptance contract missing or invalid"
    if not _valid_entries(value.get("outputs"), "O", "description"):
        return None, "output contract missing or invalid"
    if not _valid_entries(value.get("evidence"), "E", "requirement"):
        return None, "evidence contract missing or invalid"
    actual = contract_fingerprint(value)
    if status.get("contract_fingerprint") != actual:
        return None, "contract fingerprint mismatch"
    return value, None


def review_binds_contract(task_dir: Path, fingerprint: str) -> bool:
    path = task_dir / "REVIEW.md"
    if not path.exists() or not fingerprint:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(
        re.search(
            rf"^\s*(?:合同指纹|contract_fingerprint)\s*[:：]\s*{re.escape(fingerprint)}\s*$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def agent_heartbeat_freshness(board_root: Path, agent: str, stale_hours: float = 24.0) -> float | None:
    """返回端心跳新鲜度（小时）。None=无心跳文件或不可解析。

    读取 experimental/<agent>-heartbeat.md 中**最后**一个 `时间=` 时间戳
    （心跳为 append-only，末行即最新）。用于 submit 时验收通道 fail-fast
    （P1-2，2026-08-12 治理新增）。
    """
    hb_dir = board_root / "experimental"
    candidates = [
        hb_dir / f"{agent}-heartbeat.md",
        hb_dir / f"{agent}-heartbeat-advanced.md",
        hb_dir / f"{agent}-heartbeat-max.md",
    ]
    latest_ts: str | None = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in content.splitlines():
            if "时间=" not in line:
                continue
            ts_str = line.split("时间=", 1)[1].split("|", 1)[0].strip()
            latest_ts = ts_str  # append-only，最后出现的即最新
    if latest_ts is None:
        return None
    normalized = latest_ts.strip()
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        from datetime import datetime, timezone, timedelta
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        now = datetime.now(parsed.tzinfo)
        return (now - parsed).total_seconds() / 3600
    except ValueError:
        return None

