#!/usr/bin/env python3
"""Guarded Xiaohongshu public publishing entrypoint.

This script performs no retry. It always attempts screen-off cleanup and emits
one machine-readable JSON result.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from xhs_operator import OperatorError, XhsOperator
from ui_tars_recovery import UiTarsRecoveryAgent


DEFAULT_CONFIG = Path(
    os.environ.get(
        "XHS_CONFIG",
        Path.home() / ".config" / "codex" / "xhs-android-publisher.json",
    )
).expanduser()


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书安卓安全发布入口")
    parser.add_argument("article_id")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    started = time.monotonic()
    payload: dict[str, object] = {
        "ok": False,
        "result": "failed_unverified",
        "article_id": args.article_id,
    }
    operator: XhsOperator | None = None
    try:
        pending = UiTarsRecoveryAgent().pending_handoffs()
        if pending:
            payload["result"] = "skipped"
            raise OperatorError(
                "存在尚未处理的 UI-TARS/Codex 故障接管项，"
                f"禁止启动新的发布流程：{pending[0]}"
            )
        operator = XhsOperator(args.config)
        doctor = operator.doctor()
        payload["doctor"] = doctor
        if not doctor.get("ok"):
            raise OperatorError("设备预检未通过，禁止进入发布流程。")

        article = operator.article(args.article_id)
        if article.manifest.get("status") == "published":
            payload["result"] = "skipped"
            raise OperatorError("文章状态已是 published，禁止重复发布。")
        check = operator.check_article(args.article_id)
        payload["article_check"] = check
        if not check.get("ok"):
            raise OperatorError("文章门禁未通过：" + "；".join(check["errors"]))

        result = operator.publish_article(args.article_id)
        payload.update(result)
        payload["ok"] = bool(result.get("ok")) and result.get("privacy") == "公开可见"
        payload["result"] = "published" if payload["ok"] else "failed_unverified"
    except (OperatorError, OSError, json.JSONDecodeError) as error:
        payload["error"] = str(error)
    finally:
        screen_off = False
        screen_evidence = ""
        if operator is not None:
            try:
                cleanup = operator.end()
                power = operator.shell("dumpsys", "power")
                wakefulness_off = (
                    "mWakefulness=Dozing" in power
                    or "mWakefulness=Asleep" in power
                )
                suspend_released = "mHoldingDisplaySuspendBlocker=false" in power
                screen_off = (
                    cleanup.get("screen") == "off"
                    and wakefulness_off
                    and suspend_released
                )
                screen_evidence = (
                    "wakefulness_off="
                    f"{str(wakefulness_off).lower()},"
                    "display_suspend_released="
                    f"{str(suspend_released).lower()}"
                )
                payload["cleanup"] = cleanup
            except Exception as cleanup_error:
                payload["cleanup_error"] = str(cleanup_error)
        payload["phone_screen_off"] = screen_off
        payload["screen_off_evidence"] = screen_evidence
        payload["complete_workflow_seconds"] = round(time.monotonic() - started, 1)
        if not screen_off:
            payload["ok"] = False
            if payload.get("result") == "published":
                payload["result"] = "failed_unverified"

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
