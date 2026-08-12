#!/usr/bin/env python3
"""公告牌核心契约与安全写入原语。

这里只存机器必须一致的最小口径：注册端、能力/等级、状态枚举、
安全任务 ID、模型家族识别、文件锁和原子写。展示文案仍由 board-setup.py 维护。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


CONTRACT_VERSION = 3

AGENT_CONTRACTS = {
    "trae": {
        "display_name": "Trae",
        "caps": ["scheduled-task", "trae"],
        "model_families": ["glm"],
        "max_execute_level": "L2",
        "max_review_level": "L2",
        "internal_hetero_model": False,
    },
    "workbuddy": {
        "display_name": "WorkBuddy",
        "caps": ["scheduled-task", "workbuddy"],
        "model_families": ["deepseek", "kimi", "glm", "minimax"],
        "max_execute_level": "L3",
        "max_review_level": "L3",
        "internal_hetero_model": True,
    },
    "antigravity": {
        "display_name": "Antigravity",
        "caps": ["scheduled-task", "antigravity"],
        "model_families": ["gemini"],
        "max_execute_level": "L2",
        "max_review_level": "L2",
        "internal_hetero_model": False,
    },
    "qwenwork": {
        "display_name": "QwenWork",
        "caps": ["scheduled-task", "qwenwork"],
        "model_families": ["qwen"],
        "max_execute_level": "L3",
        "max_review_level": "L3",
        "default_execute_level": "L2",
        "default_review_level": "L2",
        "model_level_overrides": {
            "qwen38max": {"execute": "L3", "review": "L3"},
        },
        "internal_hetero_model": False,
    },
    "codex": {
        "display_name": "Codex",
        "caps": ["codex"],
        "model_families": ["gpt"],
        "max_execute_level": "L4",
        "max_review_level": "L4",
        "internal_hetero_model": False,
    },
}

ALLOWED_STATUSES = {
    "open",
    "claimed",
    "awaiting_review",
    "awaiting_user_approval",
    "done",
    "blocked",
    "dlq",
    "cancelled",
    "superseded",
    "archived",
}

VALID_TRANSITIONS = {
    None: {"open", "awaiting_user_approval"},  # CP2/CP3: genesis 允许直接创建 awaiting_user_approval
    "open": {"claimed", "awaiting_user_approval", "done", "cancelled", "superseded"},  # open→done 仅兼容旧 archive event
    "claimed": {"open", "awaiting_review", "blocked", "dlq", "cancelled", "superseded"},
    "awaiting_review": {"done", "awaiting_user_approval", "claimed", "cancelled", "superseded"},
    "awaiting_user_approval": {"open", "claimed", "done", "cancelled", "superseded"},
    "blocked": {"open", "claimed", "dlq", "cancelled", "superseded"},
    "dlq": {"open", "cancelled", "superseded"},
    "done": {"archived"},
    "cancelled": {"archived"},
    "superseded": {"archived"},
    "archived": set(),
}

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL_FAMILIES = (
    ("deepseek", "deepseek"),
    ("gemini", "gemini"),
    ("claude", "claude"),
    ("minimax", "minimax"),
    ("qwen", "qwen"),
    ("qwork", "qwen"),
    ("kimi", "kimi"),
    ("glm", "glm"),
    ("gpt", "gpt"),
    ("o3", "gpt"),
    ("o4", "gpt"),
)
_GENERIC_MODEL_LABELS = {"", "auto", "ai", "assistant", "model", "unknown", "test-model"}

_AGENT_ALIASES = {}
for _key, _profile in AGENT_CONTRACTS.items():
    _AGENT_ALIASES[_key] = _key
    _AGENT_ALIASES[_profile["display_name"].lower()] = _key


def is_safe_token(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_TOKEN.fullmatch(value)) and ".." not in value


def validate_task_id(value: object) -> bool:
    return is_safe_token(value)


def is_physical_task_dir(task_dir: Path, tasks_root: Path) -> bool:
    """仅接受 tasks/ 的真实直接子目录，拒绝符号链接与目录逃逸。"""
    try:
        return (
            not task_dir.is_symlink()
            and task_dir.is_dir()
            and task_dir.resolve().parent == tasks_root.resolve()
        )
    except OSError:
        return False


def canonical_agent(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _AGENT_ALIASES.get(value.strip().lower())


def is_canonical_agent(value: object) -> bool:
    return isinstance(value, str) and value == canonical_agent(value)


def validate_model_label(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() not in _GENERIC_MODEL_LABELS


def model_family(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    for token, family in _MODEL_FAMILIES:
        if token in lowered:
            return family
    return None


def level_value(value: object) -> int:
    if isinstance(value, str) and len(value) == 2 and value[0] == "L" and value[1].isdigit():
        return int(value[1])
    return -1


def normalized_model_key(value: object) -> str | None:
    """将真实模型档名归一为稳定比较键；不接受泛化模型标签。"""
    if not validate_model_label(value):
        return None
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def effective_agent_level(agent: object, model: object, role: str) -> str:
    """返回端+真实模型的有效执行/验收上限，未知模型走端的保守默认档。"""
    agent_key = canonical_agent(agent)
    if agent_key is None or role not in {"execute", "review"}:
        return "L0"
    profile = AGENT_CONTRACTS[agent_key]
    default = profile.get(
        f"default_{role}_level",
        profile[f"max_{role}_level"],
    )
    model_key = normalized_model_key(model)
    override = profile.get("model_level_overrides", {}).get(model_key, {})
    return override.get(role, default)


def effective_execute_level(agent: object, model: object) -> str:
    return effective_agent_level(agent, model, "execute")


def effective_review_level(agent: object, model: object) -> str:
    return effective_agent_level(agent, model, "review")


def review_override_allows(
    status: object,
    reviewer: object,
    reviewer_model: object | None = None,
) -> bool:
    """仅接受用户明确批准、任务级、端+模型精确绑定的一次性验收例外。"""
    if not isinstance(status, dict):
        return False
    override = status.get("review_override")
    if not isinstance(override, dict):
        return False
    reviewer_key = canonical_agent(reviewer)
    allowed_key = canonical_agent(override.get("allowed_reviewer"))
    allowed_model = override.get("allowed_model")
    allowed_family = override.get("allowed_model_family")
    reason = override.get("reason")
    evidence = override.get("approval_evidence")
    if (
        override.get("approved_by") != "user"
        or override.get("one_time") is not True
        or override.get("task_id") != status.get("id")
        or not isinstance(override.get("approved_at"), str)
        or not override.get("approved_at")
        or reviewer_key is None
        or reviewer_key != allowed_key
        or not validate_model_label(allowed_model)
        or model_family(allowed_model) != allowed_family
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(evidence, str)
        or not evidence.strip()
        or level_value(status.get("complexity")) > level_value(override.get("max_task_level"))
    ):
        return False
    if reviewer_model is not None:
        return (
            isinstance(reviewer_model, str)
            and reviewer_model.strip().lower() == allowed_model.strip().lower()
        )
    return True


NO_ELIGIBLE_INDEPENDENT_REVIEWER = "NO_ELIGIBLE_INDEPENDENT_REVIEWER"
REVIEW_AUTHORIZATION_SCHEMA = "reviewer-authorization/v1"
REVIEW_AUTHORIZATION_SCOPE = "independent-technical-review"


def requires_independent_review(task: object) -> bool:
    return (
        isinstance(task, dict)
        and task.get("task_type") != "tracking"
        and task.get("review_required") is not False
    )


def _review_policy(status: object, card_text: str = "") -> dict:
    """Read the small, additive reviewer policy surface without migrating cards."""
    policy = status.get("review_policy") if isinstance(status, dict) else None
    policy = dict(policy) if isinstance(policy, dict) else {}

    def card_value(field: str) -> str | None:
        match = re.search(
            rf"(?:^|[\s，,；;]){re.escape(field)}\s*[:=：]\s*([^\s，,；;]+)",
            card_text or "",
            re.IGNORECASE | re.MULTILINE,
        )
        return match.group(1).strip().strip('"\'') if match else None

    preferred = policy.get("preferred_reviewer") or card_value("preferred_reviewer")
    locked_value = policy.get("locked_reviewer")
    if locked_value is None:
        locked_value = card_value("locked_reviewer")
    locked = locked_value is True or str(locked_value).strip().lower() == "true"
    min_level = policy.get("min_reviewer_level") or card_value("min_reviewer_level")
    hetero_value = policy.get("require_hetero_model")
    if hetero_value is None:
        hetero_value = card_value("require_hetero_model")
    require_hetero = (
        hetero_value is True
        or str(hetero_value).strip().lower() == "true"
        or level_value(status.get("complexity") if isinstance(status, dict) else None) >= 3
    )
    forbidden = policy.get("forbidden_reviewers")
    if not isinstance(forbidden, list):
        forbidden = []
    return {
        "locked_reviewer": canonical_agent(preferred) if locked else None,
        "min_reviewer_level": min_level if level_value(min_level) in {1, 2, 3, 4} else None,
        "require_hetero_model": require_hetero,
        "forbidden_reviewers": {
            key for value in forbidden if (key := canonical_agent(value)) is not None
        },
    }


def review_authorization_allows(
    status: object,
    reviewer: object,
    reviewer_model: object | None = None,
    *,
    card_text: str = "",
    allow_consumed: bool = False,
) -> bool:
    """Validate an exact, task-scoped, one-shot reviewer authorization.

    Event provenance is deliberately checked by the transition/integrity layer,
    because this pure contract helper does not read the filesystem.
    """
    if not isinstance(status, dict):
        return False
    authorization = status.get("review_authorization")
    if not isinstance(authorization, dict):
        return False
    reviewer_key = canonical_agent(reviewer)
    authorized_key = canonical_agent(authorization.get("reviewer"))
    authorized_model = authorization.get("reviewer_model")
    authorized_family = authorization.get("reviewer_family")
    executor_key = canonical_agent(status.get("executor"))
    executor_family = model_family(status.get("executor_model"))
    policy = _review_policy(status, card_text)
    consumed_at = authorization.get("consumed_at")
    active = authorization.get("active") is True
    if allow_consumed:
        lifecycle_ok = active or (
            authorization.get("active") is False
            and isinstance(consumed_at, str)
            and bool(consumed_at.strip())
        )
    else:
        lifecycle_ok = active and not consumed_at
    if (
        authorization.get("schema") != REVIEW_AUTHORIZATION_SCHEMA
        or not is_safe_token(authorization.get("authorization_id"))
        or authorization.get("task_id") != status.get("id")
        or authorization.get("approved_by") != "user"
        or not isinstance(authorization.get("approved_at"), str)
        or not authorization.get("approved_at")
        or authorization.get("one_time") is not True
        or authorization.get("scope") != REVIEW_AUTHORIZATION_SCOPE
        or not lifecycle_ok
        or reviewer_key is None
        or reviewer_key != authorized_key
        or reviewer_key == executor_key
        or reviewer_key in policy["forbidden_reviewers"]
        or (
            policy["locked_reviewer"] is not None
            and reviewer_key != policy["locked_reviewer"]
        )
        or not validate_model_label(authorized_model)
        or authorized_family not in AGENT_CONTRACTS[reviewer_key]["model_families"]
        or model_family(authorized_model) != authorized_family
        or not isinstance(authorization.get("reason"), str)
        or not authorization.get("reason").strip()
        or not isinstance(authorization.get("approval_evidence"), str)
        or not authorization.get("approval_evidence").strip()
    ):
        return False
    required_level = max(
        level_value(status.get("complexity")),
        level_value(policy["min_reviewer_level"]),
    )
    if required_level < 1 or required_level > level_value(
        authorization.get("requested_max_level")
    ):
        return False
    if policy["require_hetero_model"] and executor_key is not None and (
        not executor_family
        or not authorized_family
        or executor_family == authorized_family
    ):
        return False
    if reviewer_model is not None and (
        not isinstance(reviewer_model, str)
        or reviewer_model.strip().lower() != authorized_model.strip().lower()
    ):
        return False
    return True


def required_review_level(task: object, card_text: str = "") -> str:
    if not isinstance(task, dict):
        return "L0"
    policy = _review_policy(task, card_text)
    required = max(
        level_value(task.get("complexity", "L1")),
        level_value(policy["min_reviewer_level"]),
    )
    return f"L{required}" if required in {1, 2, 3, 4} else "L0"


def eligible_reviewers(
    task: object,
    executor: object | None = None,
    *,
    executor_model: object | None = None,
    card_text: str = "",
    contracts: dict | None = None,
    include_authorization: bool = True,
) -> list[dict]:
    """Return auditable native/authorized independent reviewer routes."""
    if not isinstance(task, dict):
        return []
    profiles = contracts or AGENT_CONTRACTS
    executor_key = canonical_agent(executor if executor is not None else task.get("executor"))
    executor_model = (
        executor_model if executor_model is not None else task.get("executor_model")
    )
    executor_family = model_family(executor_model)
    policy = _review_policy(task, card_text)
    required_level = level_value(required_review_level(task, card_text))
    if required_level not in {1, 2, 3, 4}:
        return []

    routes = []
    for reviewer_key, profile in profiles.items():
        if reviewer_key == executor_key or reviewer_key in policy["forbidden_reviewers"]:
            continue
        if (
            policy["locked_reviewer"] is not None
            and reviewer_key != policy["locked_reviewer"]
        ):
            continue
        if required_level > level_value(profile.get("max_review_level")):
            continue
        families = [
            family
            for family in profile.get("model_families", [])
            if not policy["require_hetero_model"]
            or (executor_family and family != executor_family)
        ]
        if policy["require_hetero_model"] and not families:
            continue
        routes.append(
            {
                "reviewer": reviewer_key,
                "route": "native",
                "max_review_level": profile.get("max_review_level"),
                "eligible_model_families": families,
            }
        )

    authorization = task.get("review_authorization")
    if include_authorization and isinstance(authorization, dict):
        authorized_reviewer = canonical_agent(authorization.get("reviewer"))
        if review_authorization_allows(
            task,
            authorized_reviewer,
            authorization.get("reviewer_model"),
            card_text=card_text,
        ):
            routes.append(
                {
                    "reviewer": authorized_reviewer,
                    "reviewer_model": authorization.get("reviewer_model"),
                    "route": "user_authorized_one_shot",
                    "authorization_id": authorization.get("authorization_id"),
                    "max_review_level": authorization.get("requested_max_level"),
                    "eligible_model_families": [authorization.get("reviewer_family")],
                }
            )
    return routes


def card_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def fsync_directory(path: Path) -> None:
    """尽力持久化目录项；部分文件系统不支持目录 fsync，届时安全降级。"""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, content: str) -> None:
    """在同目录写临时文件后 os.replace，读者只会看到旧版或完整新版。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
        fsync_directory(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def board_lock(board_root: Path, name: str):
    """同一 board 根内的命名互斥锁；锁文件留盘并由 .gitignore 排除。"""
    if not is_safe_token(name):
        raise ValueError(f"unsafe lock name: {name!r}")
    lock_dir = board_root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def task_lock(board_root: Path, task_id: str):
    """任务转换统一锁。

    先拿 v1.7 的 legacy claim 锁，再拿 transition 锁：既与仍在运行的旧
    claim/lease 进程互斥，也让升级后的所有动作共享同一串行化边界。
    """
    if not validate_task_id(task_id):
        raise ValueError(f"unsafe task id for lock: {task_id!r}")
    with board_lock(board_root, f"claim-{task_id}"):
        with board_lock(board_root, f"transition-{task_id}"):
            yield
