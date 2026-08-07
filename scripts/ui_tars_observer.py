#!/usr/bin/env python3
"""Read-only UI-TARS observer for the Xiaohongshu Android workflow.

The observer may describe the current UI, but it never executes a model-proposed
action and never changes article, schedule, draft, or publication state.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_CONFIG = Path(
    os.environ.get(
        "UI_TARS_CONFIG",
        Path.home() / ".config" / "codex" / "xhs-ui-tars.json",
    )
).expanduser()
STOP_WORDS = {
    "验证码": "captcha",
    "账号异常": "account_abnormal",
    "内容违规": "policy_warning",
    "发布失败": "publish_failure",
    "登录": "login_required",
    "重新登录": "login_required",
}


class ObserverError(RuntimeError):
    pass


def endpoint_is_local(base_url: str) -> bool:
    hostname = (urlparse(base_url).hostname or "").strip().lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def sanitize_visible_text(value: str) -> str:
    value = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[REDACTED_EMAIL]",
        value,
    )
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED_PHONE]", value)
    value = re.sub(
        r"(小红书号[：:]?\s*)[A-Za-z0-9_-]+",
        r"\1[REDACTED_ID]",
        value,
    )
    return value


def sanitize_json_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_visible_text(value)
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED_ID]"
                if str(key).lower() in {"account_id", "user_id", "phone"}
                else sanitize_json_value(item)
            )
            for key, item in value.items()
        }
    return value


def extract_json_object(value: str) -> dict:
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            result, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    raise ObserverError("模型未返回可解析的 JSON 对象。")


def normalize_chat_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(value or "")


class UiTarsObserver:
    def __init__(self, config_path: Path = DEFAULT_CONFIG):
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {"enabled": False}
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ObserverError("UI-TARS 观察器配置必须是 JSON 对象。")
        return value

    def _chat_completions_url(self) -> str:
        base_url = str(self.config.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            raise ObserverError("观察器配置缺少 base_url。")
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"

    def _api_key(self) -> str:
        env_name = str(self.config.get("api_key_env", "UI_TARS_API_KEY")).strip()
        value = os.environ.get(env_name, "")
        if value:
            return value
        if endpoint_is_local(str(self.config.get("base_url", ""))):
            return "local-observer"
        raise ObserverError(f"环境变量 {env_name} 未设置，不能调用云端模型。")

    @staticmethod
    def _visible_nodes(nodes: list[object]) -> list[dict]:
        result = []
        for node in nodes:
            text = sanitize_visible_text(str(getattr(node, "text", "")).strip())
            description = sanitize_visible_text(
                str(getattr(node, "content_desc", "")).strip()
            )
            resource_id = str(getattr(node, "resource_id", "")).strip()
            if not (text or description or resource_id):
                continue
            result.append(
                {
                    "text": text,
                    "content_desc": description,
                    "resource_id": resource_id,
                    "clickable": bool(getattr(node, "clickable", False)),
                    "bounds": list(getattr(node, "bounds", ())),
                }
            )
            if len(result) >= 120:
                break
        return result

    @staticmethod
    def _risk_flags(nodes: list[dict]) -> list[str]:
        combined = "\n".join(
            f"{node.get('text', '')}\n{node.get('content_desc', '')}"
            for node in nodes
        )
        return sorted(
            {code for word, code in STOP_WORDS.items() if word in combined}
        )

    def _invoke_model(
        self,
        screenshot_path: Path,
        observation: dict,
        *,
        allow_cloud: bool,
    ) -> tuple[dict, str]:
        base_url = str(self.config.get("base_url", "")).strip()
        is_local = endpoint_is_local(base_url)
        if not is_local and not allow_cloud:
            raise ObserverError(
                "观察器端点是外网地址；必须显式传入 --allow-cloud 才能上传手机截图。"
            )
        model = str(self.config.get("model", "")).strip()
        if not model:
            raise ObserverError("观察器配置缺少 model。")

        image_data = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
        ui_context = json.dumps(
            {
                "deterministic_page_summary": sanitize_json_value(
                    observation["deterministic_page_summary"]
                ),
                "current_focus": observation["current_focus"],
                "visible_nodes": observation["visible_nodes"],
            },
            ensure_ascii=False,
        )
        system_prompt = (
            "你是独居生活指南项目的小红书 Android 只读观察器。"
            "你只能描述当前截图和风险，不得要求、建议或执行公开发布、删除、"
            "发送、支付、登录、验证码处理，也不得输出点击坐标。"
            "如果页面未知、账号异常、验证码、内容违规、网络结果不明确，"
            "must_stop 必须为 true。只输出一个 JSON 对象，字段为："
            "page_type(string)、confidence(number 0..1)、visible_evidence(array)、"
            "anomaly(string)、risk_flags(array)、must_stop(boolean)、"
            "recommended_next_step(string)。"
        )
        user_prompt = (
            "判断当前小红书页面。模型输出只会被保存为诊断记录，不会执行。"
            f"\n已脱敏的 UI 上下文：{ui_context}"
        )
        request_body = {
            "model": model,
            "temperature": float(self.config.get("temperature", 0)),
            "max_tokens": int(self.config.get("max_tokens", 450)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
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
            self._chat_completions_url(),
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = float(self.config.get("timeout_seconds", 60))
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise ObserverError(f"模型服务返回 HTTP {error.code}：{detail}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ObserverError(f"模型服务调用失败：{error}") from error

        try:
            content = normalize_chat_content(
                payload["choices"][0]["message"]["content"]
            )
        except (KeyError, IndexError, TypeError) as error:
            raise ObserverError("模型服务响应缺少 choices[0].message.content。") from error
        return extract_json_object(content), content

    def observe(
        self,
        operator: object,
        *,
        allow_cloud: bool = False,
        invoke_model: bool = True,
    ) -> dict:
        timestamp = datetime.now().astimezone()
        stamp = timestamp.strftime("%Y%m%d-%H%M%S")
        screenshot_path = operator.screenshot(f"ui-tars-observe-{stamp}.png")
        nodes = operator.dump_ui()
        visible_nodes = self._visible_nodes(nodes)
        local_risks = self._risk_flags(visible_nodes)
        observation = {
            "ok": True,
            "mode": "shadow_read_only",
            "observed_at": timestamp.isoformat(timespec="seconds"),
            "current_focus": operator.current_focus(),
            "deterministic_page_summary": operator.page_summary(nodes),
            "visible_nodes": visible_nodes,
            "local_risk_flags": local_risks,
            "must_stop": bool(local_risks),
            "screenshot": str(screenshot_path),
            "model_enabled": bool(self.config.get("enabled", False)),
            "model_invoked": False,
            "cloud_upload": False,
            "execution_allowed": False,
            "article_state_changed": False,
            "schedule_state_changed": False,
            "publish_action_allowed": False,
        }

        raw_model_output = ""
        if invoke_model and observation["model_enabled"]:
            analysis, raw_model_output = self._invoke_model(
                screenshot_path,
                observation,
                allow_cloud=allow_cloud,
            )
            model_risks = [
                str(value) for value in analysis.get("risk_flags", []) if value
            ]
            observation["model_invoked"] = True
            observation["cloud_upload"] = not endpoint_is_local(
                str(self.config.get("base_url", ""))
            )
            observation["model"] = str(self.config.get("model", ""))
            observation["model_analysis"] = analysis
            observation["must_stop"] = bool(
                observation["must_stop"]
                or analysis.get("must_stop", False)
                or model_risks
            )
        else:
            observation["model_status"] = (
                "disabled_by_config"
                if not observation["model_enabled"]
                else "skipped_by_command"
            )

        record_path = Path(operator.output_dir) / f"ui-tars-observe-{stamp}.json"
        record = dict(observation)
        if raw_model_output:
            record["raw_model_output"] = raw_model_output
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        compact = {
            key: value
            for key, value in observation.items()
            if key != "visible_nodes"
        }
        compact["visible_node_count"] = len(visible_nodes)
        compact["record"] = str(record_path)
        return compact


def run_observation_session(
    operator: object,
    *,
    config_path: Path = DEFAULT_CONFIG,
    allow_cloud: bool = False,
    invoke_model: bool = True,
    wake: bool = True,
) -> dict:
    started = time.monotonic()
    result: dict
    cleanup: dict = {"ok": False, "screen": "unknown"}
    try:
        if wake:
            begin = operator.begin()
            if not begin.get("ok"):
                raise ObserverError(f"手机观察会话启动失败：{begin}")
        observer = UiTarsObserver(config_path)
        result = observer.observe(
            operator,
            allow_cloud=allow_cloud,
            invoke_model=invoke_model,
        )
    except Exception as error:  # Preserve cleanup evidence for every failure.
        result = {
            "ok": False,
            "mode": "shadow_read_only",
            "error": str(error),
            "execution_allowed": False,
            "article_state_changed": False,
            "schedule_state_changed": False,
            "publish_action_allowed": False,
        }
    finally:
        try:
            cleanup = operator.end()
        except Exception as error:
            cleanup = {"ok": False, "screen": "unknown", "error": str(error)}

    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    result["cleanup"] = cleanup
    result["phone_screen_off"] = bool(
        cleanup.get("ok") and cleanup.get("screen") == "off"
    )
    if not result["phone_screen_off"]:
        result["ok"] = False
    return result
