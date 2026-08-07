#!/usr/bin/env python3
"""Guarded UI-TARS recovery and experience reuse for Xiaohongshu UI drift."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import math
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ui_tars_observer import (
    DEFAULT_CONFIG,
    ObserverError,
    UiTarsObserver,
    endpoint_is_local,
    extract_json_object,
    normalize_chat_content,
    sanitize_visible_text,
)


ALLOWED_ACTIONS = {
    "tap_text",
    "tap_resource",
    "tap_point",
    "swipe",
    "back",
    "wait",
    "stop",
}
CRITICAL_TERMS = {
    "发布",
    "定时发布",
    "删除",
    "确认删除",
    "支付",
    "付款",
    "提交",
    "登录",
    "重新登录",
    "验证码",
    "公开可见",
    "仅自己可见",
    "账号异常",
    "内容违规",
}
CRITICAL_RESOURCE_PARTS = {
    "postbtn",
    "publish",
    "delete",
    "login",
    "privacy",
    "schedule",
}
RECOVERABLE_FAILURES = {"missing_text", "missing_resource", "missing_control"}


class RecoveryPolicyError(ObserverError):
    pass


def stable_error_signature(value: str) -> str:
    normalized = re.sub(r"\d+", "#", value.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def page_fingerprint(focus: str, nodes: list[object]) -> str:
    features = [focus]
    for node in nodes:
        resource_id = str(getattr(node, "resource_id", "")).strip()
        if resource_id:
            features.append(f"id:{resource_id}")
    value = "\n".join(sorted(set(features)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _round_by_factor(number: int, factor: int) -> int:
    return round(number / factor) * factor


def smart_resize(height: int, width: int, factor: int = 28) -> tuple[int, int]:
    min_pixels = 100 * factor * factor
    max_pixels = 16384 * factor * factor
    resized_height = max(factor, _round_by_factor(height, factor))
    resized_width = max(factor, _round_by_factor(width, factor))
    if resized_height * resized_width > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        resized_height = math.floor(height / beta / factor) * factor
        resized_width = math.floor(width / beta / factor) * factor
    elif resized_height * resized_width < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        resized_height = math.ceil(height * beta / factor) * factor
        resized_width = math.ceil(width * beta / factor) * factor
    return resized_height, resized_width


def parse_model_action(
    content: str,
    *,
    screenshot_width: int,
    screenshot_height: int,
) -> dict:
    try:
        return extract_json_object(content)
    except ObserverError:
        pass
    action_match = re.search(
        r"Action:\s*([A-Za-z_]+)\s*\((.*?)\)\s*$",
        content,
        re.DOTALL,
    )
    if not action_match:
        raise ObserverError("恢复模型既未返回 JSON，也未返回可解析的 UI-TARS Action。")
    action_name = action_match.group(1).lower()
    arguments = action_match.group(2)
    if action_name in {"wait"}:
        return {"action_type": "wait", "seconds": 2}
    if action_name in {"press_back", "back"}:
        return {"action_type": "back"}
    if action_name in {"finished", "call_user"}:
        return {"action_type": "stop", "reason": action_name}
    if action_name not in {"click", "left_click", "left_single"}:
        raise ObserverError(f"UI-TARS 原生动作不在恢复白名单：{action_name}")
    point_match = re.search(
        r"(?:start_box|point)\s*=\s*['\"](?:<\|box_start\|>)?"
        r"\(?\[?\s*([\d.]+)\s*[, ]\s*([\d.]+)"
        r"(?:\s*[, ]\s*([\d.]+)\s*[, ]\s*([\d.]+))?"
        r"\s*\]?\)?(?:<\|box_end\|>)?['\"]",
        arguments,
    )
    if not point_match:
        raise ObserverError("UI-TARS click 动作缺少可解析坐标。")
    values = [float(value) for value in point_match.groups() if value is not None]
    x = values[0] if len(values) == 2 else (values[0] + values[2]) / 2
    y = values[1] if len(values) == 2 else (values[1] + values[3]) / 2
    resized_height, resized_width = smart_resize(
        screenshot_height,
        screenshot_width,
    )
    return {
        "action_type": "tap_point",
        "point": [
            max(0, min(1000, x / resized_width * 1000)),
            max(0, min(1000, y / resized_height * 1000)),
        ],
        "source_format": "native_ui_tars",
    }


class ExperienceStore:
    def __init__(self, root: Path):
        self.root = root
        self.candidates = root / "candidates"
        self.approved = root / "approved"
        self.quarantined = root / "quarantined"
        self.incidents = root / "incidents"
        self.codex_pending = root / "codex_queue" / "pending"
        self.codex_resolved = root / "codex_queue" / "resolved"
        self.codex_failed = root / "codex_queue" / "failed"
        for folder in (
            self.candidates,
            self.approved,
            self.quarantined,
            self.incidents,
            self.codex_pending,
            self.codex_resolved,
            self.codex_failed,
        ):
            folder.mkdir(parents=True, exist_ok=True)

    def record_incident(
        self,
        *,
        failure_kind: str,
        target: str,
        fingerprint: str,
        error_signature: str,
        error: str,
        screenshot: str,
        focus: str,
    ) -> Path:
        now = datetime.now().astimezone()
        incident_id = (
            f"{now.strftime('%Y%m%d-%H%M%S')}-"
            f"{failure_kind}-{error_signature}"
        )
        path = self.incidents / f"{incident_id}.json"
        value = {
            "schema_version": 1,
            "incident_id": incident_id,
            "status": "open",
            "failure_kind": failure_kind,
            "target": target,
            "page_fingerprint": fingerprint,
            "error_signature": error_signature,
            "error": error,
            "focus": focus,
            "screenshot": screenshot,
            "created_at": now.isoformat(timespec="seconds"),
            "recovery_may_not_override": [
                "content_gate",
                "schedule_gate",
                "account_check",
                "captcha",
                "policy_warning",
                "ambiguous_publish_result",
            ],
        }
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def enqueue_codex(
        self,
        *,
        incident: Path,
        failure_kind: str,
        target: str,
        fingerprint: str,
        error: str,
        screenshot: str,
        focus: str,
    ) -> Path:
        incident_value = json.loads(incident.read_text(encoding="utf-8"))
        handoff_id = str(incident_value["incident_id"])
        path = self.codex_pending / f"{handoff_id}.json"
        value = {
            "schema_version": 1,
            "handoff_id": handoff_id,
            "status": "pending_codex_takeover",
            "incident": str(incident),
            "failure_kind": failure_kind,
            "target": target,
            "page_fingerprint": fingerprint,
            "error": error,
            "focus": focus,
            "screenshot": screenshot,
            "requested_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "resume_only_after": "target_visible_and_deterministically_verified",
            "hard_stops": [
                "publish_or_schedule_confirmation",
                "delete",
                "login_or_captcha",
                "account_or_policy_warning",
                "ambiguous_submission_result",
            ],
            "device_policy": {
                "model": "ONEPLUS A6003",
                "transport": "usb",
                "ignore_other_adb_devices": True,
            },
        }
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def resolve_incident(
        path: Path,
        *,
        source: str,
        experience: str,
    ) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = "recovered"
        value["recovery_source"] = source
        value["experience"] = experience
        value["recovered_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def find(self, failure_kind: str, target: str, fingerprint: str) -> tuple[Path, dict] | None:
        ranked: list[tuple[int, Path, dict]] = []
        for rank, folder in ((2, self.approved), (1, self.candidates)):
            for path in folder.glob("*.json"):
                try:
                    skill = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if skill.get("failure_kind") != failure_kind:
                    continue
                if skill.get("target") != target:
                    continue
                if skill.get("page_fingerprint") != fingerprint:
                    continue
                if not skill.get("actions"):
                    continue
                ranked.append((rank, path, skill))
        if not ranked:
            return None
        _, path, skill = sorted(
            ranked,
            key=lambda item: (
                item[0],
                int(item[2].get("success_count", 0)),
                item[2].get("updated_at", ""),
            ),
            reverse=True,
        )[0]
        return path, skill

    def save_candidate(
        self,
        *,
        failure_kind: str,
        target: str,
        fingerprint: str,
        error_signature: str,
        actions: list[dict],
        evidence: dict,
    ) -> Path:
        skill_id = hashlib.sha256(
            f"{failure_kind}\0{target}\0{fingerprint}".encode("utf-8")
        ).hexdigest()[:20]
        path = self.candidates / f"{skill_id}.json"
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        previous = {}
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                previous = {}
        value = {
            "schema_version": 1,
            "skill_id": skill_id,
            "status": "candidate",
            "verification_scope": "target_visible_after_recovery",
            "failure_kind": failure_kind,
            "target": target,
            "page_fingerprint": fingerprint,
            "error_signature": error_signature,
            "actions": actions,
            "success_count": int(previous.get("success_count", 0)) + 1,
            "failure_count": int(previous.get("failure_count", 0)),
            "created_at": previous.get("created_at", now),
            "updated_at": now,
            "last_evidence": evidence,
            "never_authorizes": [
                "public_publish",
                "scheduled_publish",
                "delete",
                "login",
                "captcha",
                "account_or_policy_override",
            ],
        }
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if value["success_count"] >= 2:
            approved_path = self.approved / path.name
            value["status"] = "approved"
            approved_path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            path.unlink(missing_ok=True)
            return approved_path
        return path

    def record_reuse_success(self, path: Path, skill: dict, evidence: dict) -> Path:
        skill["success_count"] = int(skill.get("success_count", 0)) + 1
        skill["updated_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        skill["last_evidence"] = evidence
        target_path = path
        if skill["success_count"] >= 2 and path.parent == self.candidates:
            skill["status"] = "approved"
            target_path = self.approved / path.name
        target_path.write_text(
            json.dumps(skill, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if target_path != path:
            path.unlink(missing_ok=True)
        return target_path

    def record_failure(self, path: Path, skill: dict, reason: str) -> None:
        skill["failure_count"] = int(skill.get("failure_count", 0)) + 1
        skill["updated_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        skill["last_failure"] = reason
        if skill["failure_count"] >= 1:
            skill["status"] = "quarantined"
            target = self.quarantined / path.name
            target.write_text(
                json.dumps(skill, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            path.unlink(missing_ok=True)
        else:
            path.write_text(
                json.dumps(skill, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


class UiTarsRecoveryAgent:
    def __init__(self, config_path: Path = DEFAULT_CONFIG):
        self.observer = UiTarsObserver(config_path)
        self.config = self.observer.config
        experience_root = self.config.get("experience_root")
        if experience_root:
            root = Path(str(experience_root)).expanduser()
        else:
            root = (
                Path.home()
                / ".local"
                / "share"
                / "codex"
                / "xhs-android-publisher"
                / "ui_tars_experience"
            )
        self.store = ExperienceStore(root)

    @property
    def recovery_enabled(self) -> bool:
        return bool(self.config.get("recovery_enabled", False))

    @property
    def model_enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    @property
    def recovery_provider(self) -> str:
        return str(self.config.get("recovery_provider", "ui_tars")).strip()

    def status(self) -> dict:
        def count(folder: Path) -> int:
            return sum(1 for _ in folder.glob("*.json"))

        base_url = str(self.config.get("base_url", "")).strip()
        pending_handoffs = self.pending_handoffs()
        return {
            "recovery_enabled": self.recovery_enabled,
            "recovery_provider": self.recovery_provider,
            "model_enabled": self.model_enabled,
            "endpoint_scope": (
                "local_or_lan"
                if base_url and endpoint_is_local(base_url)
                else "cloud"
                if base_url
                else "not_configured"
            ),
            "allow_cloud": bool(self.config.get("allow_cloud", False)),
            "recovery_max_steps": min(
                max(int(self.config.get("recovery_max_steps", 3)), 1),
                5,
            ),
            "experience_root": str(self.store.root),
            "counts": {
                "incidents": count(self.store.incidents),
                "candidates": count(self.store.candidates),
                "approved": count(self.store.approved),
                "quarantined": count(self.store.quarantined),
                "codex_pending": count(self.store.codex_pending),
                "codex_resolved": count(self.store.codex_resolved),
                "codex_failed": count(self.store.codex_failed),
            },
            "oldest_codex_handoff": (
                str(pending_handoffs[0]) if pending_handoffs else None
            ),
        }

    def pending_handoffs(self) -> list[Path]:
        return sorted(
            self.store.codex_pending.glob("*.json"),
            key=lambda path: (path.stat().st_mtime, path.name),
        )

    @staticmethod
    def _target_visible(operator: object, failure_kind: str, target: str) -> bool:
        nodes = operator.dump_ui()
        if failure_kind == "missing_text":
            return any(str(getattr(node, "text", "")) == target for node in nodes)
        if failure_kind == "missing_resource":
            return any(
                str(getattr(node, "resource_id", "")) == target for node in nodes
            )
        return False

    @staticmethod
    def _has_stop_condition(nodes: list[object]) -> str | None:
        visible = "\n".join(
            f"{getattr(node, 'text', '')}\n{getattr(node, 'content_desc', '')}"
            for node in nodes
        )
        for term in CRITICAL_TERMS:
            if term in visible and term in {
                "登录",
                "重新登录",
                "验证码",
                "账号异常",
                "内容违规",
            }:
                return term
        return None

    @staticmethod
    def _action_hits_critical_node(action: dict, nodes: list[object]) -> bool:
        if action.get("action_type") != "tap_point":
            return False
        point = action.get("point")
        if not (
            isinstance(point, list)
            and len(point) == 2
            and all(isinstance(value, (int, float)) for value in point)
        ):
            return True
        x, y = point
        for node in nodes:
            text = str(getattr(node, "text", ""))
            description = str(getattr(node, "content_desc", ""))
            resource_id = str(getattr(node, "resource_id", "")).lower()
            critical = any(term in f"{text}\n{description}" for term in CRITICAL_TERMS)
            critical = critical or any(
                part in resource_id for part in CRITICAL_RESOURCE_PARTS
            )
            if not critical:
                continue
            x1, y1, x2, y2 = getattr(node, "bounds", (0, 0, 0, 0))
            if x1 <= x <= x2 and y1 <= y <= y2:
                return True
        return False

    def _validate_action(self, action: dict, nodes: list[object]) -> None:
        action_type = str(action.get("action_type", ""))
        if action_type not in ALLOWED_ACTIONS:
            raise RecoveryPolicyError(f"模型动作不在白名单：{action_type}")
        serialized = json.dumps(action, ensure_ascii=False).lower()
        if any(term.lower() in serialized for term in CRITICAL_TERMS):
            raise RecoveryPolicyError("模型动作涉及发布、删除、登录或其他关键操作。")
        resource_id = str(action.get("resource_id", "")).lower()
        if any(part in resource_id for part in CRITICAL_RESOURCE_PARTS):
            raise RecoveryPolicyError("模型动作指向受保护的关键控件。")
        if self._action_hits_critical_node(action, nodes):
            raise RecoveryPolicyError("模型坐标落在受保护的关键控件区域。")

    @staticmethod
    def _screen_size(operator: object) -> tuple[int, int]:
        output = operator.shell("wm", "size")
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if not match:
            match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            raise RecoveryPolicyError(f"无法解析屏幕尺寸：{output}")
        return int(match.group(1)), int(match.group(2))

    def _execute_action(self, operator: object, action: dict) -> None:
        nodes = operator.dump_ui()
        self._validate_action(action, nodes)
        action_type = action["action_type"]
        if action_type == "stop":
            raise RecoveryPolicyError(str(action.get("reason", "模型要求停止。")))
        if action_type == "wait":
            time.sleep(min(max(float(action.get("seconds", 1)), 0.2), 5.0))
            return
        if action_type == "back":
            operator.shell("input", "keyevent", "KEYCODE_BACK")
            time.sleep(0.8)
            return
        if action_type == "swipe":
            start = action.get("start")
            end = action.get("end")
            if not (
                isinstance(start, list)
                and isinstance(end, list)
                and len(start) == 2
                and len(end) == 2
            ):
                raise RecoveryPolicyError("swipe 缺少有效 start/end。")
            width, height = self._screen_size(operator)
            sx = round(float(start[0]) / 1000 * width)
            sy = round(float(start[1]) / 1000 * height)
            ex = round(float(end[0]) / 1000 * width)
            ey = round(float(end[1]) / 1000 * height)
            operator.shell(
                "input",
                "swipe",
                str(sx),
                str(sy),
                str(ex),
                str(ey),
                "350",
            )
            time.sleep(0.8)
            return
        if action_type == "tap_text":
            target = str(action.get("text", ""))
            matches = [
                node for node in nodes if str(getattr(node, "text", "")) == target
            ]
            if len(matches) != 1:
                raise RecoveryPolicyError(
                    f"tap_text 必须唯一匹配，当前“{target}”匹配 {len(matches)} 个。"
                )
            x, y = matches[0].center
        elif action_type == "tap_resource":
            target = str(action.get("resource_id", ""))
            matches = [
                node
                for node in nodes
                if str(getattr(node, "resource_id", "")) == target
            ]
            if len(matches) != 1:
                raise RecoveryPolicyError(
                    f"tap_resource 必须唯一匹配，当前匹配 {len(matches)} 个。"
                )
            x, y = matches[0].center
        elif action_type == "tap_point":
            point = action.get("point")
            if not (
                isinstance(point, list)
                and len(point) == 2
                and all(0 <= float(value) <= 1000 for value in point)
            ):
                raise RecoveryPolicyError("tap_point 必须是 0–1000 的归一化坐标。")
            width, height = self._screen_size(operator)
            x = round(float(point[0]) / 1000 * width)
            y = round(float(point[1]) / 1000 * height)
            converted = dict(action)
            converted["point"] = [x, y]
            self._validate_action(converted, nodes)
        else:
            raise RecoveryPolicyError(f"未实现的模型动作：{action_type}")
        operator.shell("input", "tap", str(x), str(y))
        time.sleep(0.8)

    def _request_action(
        self,
        operator: object,
        *,
        screenshot_path: Path,
        failure_kind: str,
        target: str,
        error: str,
        prior_actions: list[dict],
    ) -> tuple[dict, str]:
        base_url = str(self.config.get("base_url", "")).strip()
        allow_cloud = bool(self.config.get("allow_cloud", False))
        if not endpoint_is_local(base_url) and not allow_cloud:
            raise ObserverError(
                "自动接管端点是外网地址，但配置未显式设置 allow_cloud=true。"
            )
        model = str(self.config.get("model", "")).strip()
        if not model:
            raise ObserverError("自动接管配置缺少 model。")
        nodes = operator.dump_ui()
        visible_nodes = self.observer._visible_nodes(nodes)
        image_data = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
        system_prompt = (
            "你是小红书 Android 故障恢复代理。当前是受控恢复模式，不是自由操作。"
            "目标只是让原自动化需要的文本或 resource-id 重新出现在页面中。"
            "禁止发布、定时发布、删除、提交、登录、验证码、支付、修改公开范围。"
            "一次只输出一个 JSON 动作，不得输出 Action/Thought 格式或额外文字。"
            "action_type 只能是 tap_text、tap_resource、tap_point、swipe、back、wait、stop。"
            "tap_point、swipe 使用 0..1000 归一化坐标。优先使用唯一文本或 resource-id；"
            "不确定或遇到账号/验证码/违规/可能已提交时必须 stop。"
            "JSON 可包含 action_type、text、resource_id、point、start、end、seconds、"
            "reason、confidence、expected_change。"
        )
        context = {
            "failure_kind": failure_kind,
            "target": target,
            "error": error,
            "current_focus": operator.current_focus(),
            "visible_nodes": visible_nodes,
            "prior_actions": prior_actions,
        }
        body = {
            "model": model,
            "temperature": float(self.config.get("temperature", 0)),
            "max_tokens": int(self.config.get("recovery_max_tokens", 260)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(context, ensure_ascii=False),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            },
                        },
                    ],
                },
            ],
        }
        request = Request(
            self.observer._chat_completions_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.observer._api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(
                request,
                timeout=float(self.config.get("timeout_seconds", 60)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as api_error:
            detail = api_error.read().decode("utf-8", errors="replace")[:500]
            raise ObserverError(
                f"恢复模型返回 HTTP {api_error.code}：{detail}"
            ) from api_error
        except (URLError, TimeoutError, json.JSONDecodeError) as api_error:
            raise ObserverError(f"恢复模型调用失败：{api_error}") from api_error
        try:
            content = normalize_chat_content(
                payload["choices"][0]["message"]["content"]
            )
        except (KeyError, IndexError, TypeError) as api_error:
            raise ObserverError("恢复模型响应格式不正确。") from api_error
        width, height = self._screen_size(operator)
        action = parse_model_action(
            content,
            screenshot_width=width,
            screenshot_height=height,
        )
        self._validate_action(action, nodes)
        return action, content

    def recover(
        self,
        operator: object,
        *,
        failure_kind: str,
        target: str,
        error: str,
    ) -> dict:
        if failure_kind not in RECOVERABLE_FAILURES:
            return {"attempted": False, "reason": "failure_not_recoverable"}
        if not self.recovery_enabled:
            return {"attempted": False, "reason": "recovery_disabled"}

        nodes = operator.dump_ui()
        focus = operator.current_focus()
        fingerprint = page_fingerprint(focus, nodes)
        error_signature = stable_error_signature(error)
        screenshot = operator.screenshot(
            f"ui-tars-recovery-before-{int(time.time())}.png"
        )
        incident = self.store.record_incident(
            failure_kind=failure_kind,
            target=target,
            fingerprint=fingerprint,
            error_signature=error_signature,
            error=error,
            screenshot=str(screenshot),
            focus=focus,
        )
        evidence = {
            "screenshot": str(screenshot),
            "focus": focus,
            "error": error,
        }
        stop_condition = self._has_stop_condition(nodes)
        if stop_condition:
            return {
                "attempted": False,
                "must_stop": True,
                "reason": f"hard_stop:{stop_condition}",
                "incident": str(incident),
            }

        found = self.store.find(failure_kind, target, fingerprint)
        if found:
            path, skill = found
            try:
                for action in skill["actions"]:
                    self._execute_action(operator, action)
                    if self._target_visible(operator, failure_kind, target):
                        updated = self.store.record_reuse_success(
                            path,
                            skill,
                            evidence,
                        )
                        self.store.resolve_incident(
                            incident,
                            source="experience",
                            experience=str(updated),
                        )
                        return {
                            "attempted": True,
                            "recovered": True,
                            "source": "experience",
                            "experience": str(updated),
                            "incident": str(incident),
                            "actions": skill["actions"],
                        }
                raise RecoveryPolicyError("经验动作执行后目标仍未出现。")
            except Exception as reuse_error:
                self.store.record_failure(path, skill, str(reuse_error))

        if self.recovery_provider == "codex":
            handoff = self.store.enqueue_codex(
                incident=incident,
                failure_kind=failure_kind,
                target=target,
                fingerprint=fingerprint,
                error=error,
                screenshot=str(screenshot),
                focus=focus,
            )
            return {
                "attempted": bool(found),
                "recovered": False,
                "must_stop": True,
                "reason": "pending_codex_takeover",
                "incident": str(incident),
                "codex_handoff": str(handoff),
            }

        if not self.model_enabled:
            return {
                "attempted": bool(found),
                "recovered": False,
                "reason": "model_disabled_and_no_verified_experience",
                "incident": str(incident),
            }

        actions: list[dict] = []
        raw_outputs: list[str] = []
        max_steps = min(max(int(self.config.get("recovery_max_steps", 3)), 1), 5)
        for step in range(max_steps):
            current_screenshot = operator.screenshot(
                f"ui-tars-recovery-step-{int(time.time())}-{step + 1}.png"
            )
            action, raw = self._request_action(
                operator,
                screenshot_path=current_screenshot,
                failure_kind=failure_kind,
                target=target,
                error=error,
                prior_actions=actions,
            )
            raw_outputs.append(raw)
            self._execute_action(operator, action)
            actions.append(action)
            if self._target_visible(operator, failure_kind, target):
                after = operator.screenshot(
                    f"ui-tars-recovery-success-{int(time.time())}.png"
                )
                evidence["after_screenshot"] = str(after)
                experience = self.store.save_candidate(
                    failure_kind=failure_kind,
                    target=target,
                    fingerprint=fingerprint,
                    error_signature=error_signature,
                    actions=actions,
                    evidence=evidence,
                )
                self.store.resolve_incident(
                    incident,
                    source="model",
                    experience=str(experience),
                )
                return {
                    "attempted": True,
                    "recovered": True,
                    "source": "model",
                    "actions": actions,
                    "experience": str(experience),
                    "incident": str(incident),
                    "raw_outputs": raw_outputs,
                }
        return {
            "attempted": True,
            "recovered": False,
            "reason": "target_not_visible_after_recovery",
            "incident": str(incident),
            "actions": actions,
            "raw_outputs": raw_outputs,
        }
