#!/usr/bin/env python3
"""Local Android/Xiaohongshu operations with deterministic ADB primitives."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


PACKAGE = "com.xingin.xhs"
DEFAULT_CONFIG = Path(
    os.environ.get(
        "XHS_CONFIG",
        Path.home() / ".config" / "codex" / "xhs-android-publisher.json",
    )
).expanduser()
REMOTE_DIR = "/sdcard/Pictures/XiaohongshuAccount"
INPUT_HELPER_PACKAGE = "com.codex.xhsinput"


class OperatorError(RuntimeError):
    pass


def run(command: list[str], *, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    output = result.stdout or ""
    if check and result.returncode:
        raise OperatorError(output.strip() or f"Command failed: {' '.join(command)}")
    return output.strip()


def find_adb() -> str:
    candidates = [
        os.environ.get("ADB"),
        shutil.which("adb"),
        "/opt/homebrew/bin/adb",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise OperatorError("找不到 adb，请先安装 Android Platform Tools。")


def parse_adb_devices(output: str) -> list[dict[str, object]]:
    devices: list[dict[str, object]] = []
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        properties: dict[str, str] = {}
        for field in fields[2:]:
            if ":" in field:
                key, value = field.split(":", 1)
                properties[key] = value
        serial = fields[0]
        devices.append(
            {
                "serial": serial,
                "state": fields[1],
                "model": properties.get("model", "").replace("_", " "),
                "transport": (
                    "usb"
                    if "usb" in properties and ":" not in serial
                    else "network"
                    if ":" in serial
                    else "unknown"
                ),
            }
        )
    return devices


def select_target_device(
    devices: list[dict[str, object]],
    *,
    expected_model: str,
    expected_transport: str,
) -> tuple[str, list[str]]:
    eligible = [
        device
        for device in devices
        if device.get("state") == "device"
        and str(device.get("model", "")).casefold() == expected_model.casefold()
        and str(device.get("transport", "")) == expected_transport
    ]
    if len(eligible) != 1:
        found = [
            f"{device.get('model') or '未知型号'}"
            f"({device.get('transport')},{device.get('state')})"
            for device in devices
        ]
        raise OperatorError(
            f"需要唯一的 {expected_transport} {expected_model}，"
            f"当前匹配 {len(eligible)} 台；已发现：{', '.join(found) or '无'}。"
        )
    selected = str(eligible[0]["serial"])
    ignored = [
        str(device["serial"])
        for device in devices
        if str(device.get("serial")) != selected
    ]
    return selected, ignored


@dataclass
class UiNode:
    text: str
    resource_id: str
    content_desc: str
    clickable: bool
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)


@dataclass
class Article:
    folder: Path
    manifest: dict

    @property
    def article_id(self) -> str:
        return str(self.manifest["id"])

    @property
    def body(self) -> str:
        return (self.folder / self.manifest["body_file"]).read_text(encoding="utf-8").strip()

    @property
    def image_paths(self) -> list[Path]:
        return [self.folder / relative for relative in self.manifest["images"]]


class XhsOperator:
    def __init__(self, config_path: Path):
        self.adb_path = find_adb()
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.output_dir = Path(self.config["runtime_output_dir"]).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._ui_tars_recovery_attempted: set[tuple[str, str]] = set()
        self._ui_tars_recovery_records: list[dict] = []
        self._device_serial: str | None = None
        self._ignored_devices: list[str] = []

    def adb(self, *args: str, check: bool = True) -> str:
        if self._device_serial is None:
            self.require_device()
        return run(
            [self.adb_path, "-s", self._device_serial, *args],
            check=check,
        )

    def adb_raw(self, *args: str, check: bool = True) -> str:
        return run([self.adb_path, *args], check=check)

    def shell(self, *args: str, check: bool = True) -> str:
        return self.adb("shell", *args, check=check)

    def connected_devices(self) -> list[str]:
        return [
            str(device["serial"])
            for device in parse_adb_devices(self.adb_raw("devices", "-l"))
            if device.get("state") == "device"
        ]

    def require_device(self) -> str:
        if self._device_serial is not None:
            return self._device_serial
        devices = parse_adb_devices(self.adb_raw("devices", "-l"))
        self._device_serial, self._ignored_devices = select_target_device(
            devices,
            expected_model=str(self.config["expected_device_model"]),
            expected_transport=str(
                self.config.get("expected_device_transport", "usb")
            ),
        )
        return self._device_serial

    def current_focus(self) -> str:
        output = self.shell("dumpsys", "window")
        match = re.search(r"mCurrentFocus=(.+)", output)
        return match.group(1).strip() if match else "未知"

    def package_installed(self) -> bool:
        return PACKAGE in self.shell("pm", "list", "packages", PACKAGE)

    def helper_installed(self) -> bool:
        return INPUT_HELPER_PACKAGE in self.shell(
            "pm", "list", "packages", INPUT_HELPER_PACKAGE
        )

    def set_clipboard(self, text: str) -> None:
        if not self.helper_installed():
            raise OperatorError("中文输入助手未安装。")
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        output = self.shell(
            "am",
            "broadcast",
            "-n",
            f"{INPUT_HELPER_PACKAGE}/.ClipboardReceiver",
            "-a",
            "com.codex.xhsinput.SET_CLIPBOARD",
            "--es",
            "text_b64",
            encoded,
        )
        if "result=0" not in output:
            raise OperatorError(f"写入手机剪贴板失败：{output}")

    def paste_into(self, resource_id: str, text: str) -> None:
        nodes = self.find_nodes(resource_id=resource_id)
        if not nodes:
            raise OperatorError(f"找不到输入框：{resource_id}")
        x, y = nodes[0].center
        self.shell("input", "tap", str(x), str(y))
        time.sleep(0.3)
        # A resumed or retried publishing task may already contain partial text.
        # Select-all keeps retries idempotent instead of appending duplicates.
        self.shell("input", "keycombination", "KEYCODE_CTRL_LEFT", "KEYCODE_A")
        self.shell("input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.2)
        self.set_clipboard(text)
        self.shell("input", "keyevent", "KEYCODE_PASTE")
        time.sleep(0.5)

    def record_stage(self, article_id: str, stage: str, **details: object) -> None:
        payload = {
            "article_id": article_id,
            "stage": stage,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **details,
        }
        target = self.output_dir / f"article-{article_id}-progress.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def device_pin(self) -> str:
        service = self.config.get("device_pin_keychain_service")
        if not service:
            raise OperatorError("配置中缺少设备密码钥匙串服务名。")
        account = self.config.get("device_pin_keychain_account", "xhs-operator")
        pin = run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ]
        )
        if not pin.isdigit():
            raise OperatorError("钥匙串中的设备密码格式无效。")
        return pin

    def keyguard_showing(self) -> bool:
        output = self.shell("dumpsys", "window", "policy")
        flags = (
            "isStatusBarKeyguard=true",
            "mShowingLockscreen=true",
            "showing=true",
        )
        return any(flag in output for flag in flags)

    def unlock(self) -> None:
        if not self.keyguard_showing() and "NotificationShade" not in self.current_focus():
            return
        self.shell("input", "swipe", "540", "1900", "540", "650", "300")
        time.sleep(0.6)
        self.shell("input", "text", self.device_pin())
        self.shell("input", "keyevent", "KEYCODE_ENTER")
        time.sleep(1.0)

    def screen_on(self) -> bool:
        power = self.shell("dumpsys", "power")
        match = re.search(r"Display Power: state=(ON|OFF|DOZE)", power)
        if match:
            return match.group(1) == "ON"
        return "mWakefulness=Awake" in power

    def begin(self) -> dict:
        """Wake the device, dismiss the keyguard, and foreground Xiaohongshu."""
        self.require_device()
        self.shell("svc", "power", "stayon", "usb")
        active_timeout = str(self.config.get("active_screen_timeout_ms", 1800000))
        self.shell("settings", "put", "system", "screen_off_timeout", active_timeout)
        if not self.screen_on():
            self.shell("input", "keyevent", "KEYCODE_WAKEUP")
            time.sleep(1.0)
        self.shell("wm", "dismiss-keyguard", check=False)
        time.sleep(0.5)
        self.unlock()
        for _ in range(2):
            if "NotificationShade" not in self.current_focus():
                break
            # This OnePlus needs MENU once to leave its black keyguard surface.
            self.shell("input", "keyevent", "KEYCODE_MENU")
            time.sleep(0.8)
            if "SubPanel:" in self.current_focus():
                self.shell("input", "keyevent", "KEYCODE_BACK")
                time.sleep(0.4)
        for _ in range(2):
            self.shell(
                "monkey",
                "-p",
                PACKAGE,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            )
            time.sleep(1.2)
            if PACKAGE in self.current_focus():
                break
            if "NotificationShade" in self.current_focus():
                self.shell("input", "keyevent", "KEYCODE_MENU")
                time.sleep(0.8)
                if "SubPanel:" in self.current_focus():
                    self.shell("input", "keyevent", "KEYCODE_BACK")
        return {
            "ok": self.screen_on() and PACKAGE in self.current_focus(),
            "screen": "on" if self.screen_on() else "off",
            "focus": self.current_focus(),
        }

    def end(self) -> dict:
        """Release USB stay-awake and turn the screen off."""
        self.require_device()
        self.shell("svc", "power", "stayon", "false")
        normal_timeout = str(self.config.get("screen_timeout_ms", 60000))
        self.shell("settings", "put", "system", "screen_off_timeout", normal_timeout)
        if self.screen_on():
            self.shell("input", "keyevent", "KEYCODE_SLEEP")
            time.sleep(0.6)
        return {
            "ok": not self.screen_on(),
            "screen": "on" if self.screen_on() else "off",
        }

    def screenshot(self, name: str = "latest.png") -> Path:
        serial = self.require_device()
        target = self.output_dir / name
        with target.open("wb") as file:
            result = subprocess.run(
                [self.adb_path, "-s", serial, "exec-out", "screencap", "-p"],
                stdout=file,
                stderr=subprocess.PIPE,
            )
        if result.returncode:
            raise OperatorError(result.stderr.decode(errors="replace"))
        return target

    def dump_ui(self) -> list[UiNode]:
        remote = "/sdcard/xhs_operator_window.xml"
        last_error: OperatorError | None = None
        for attempt in range(4):
            try:
                self.shell("uiautomator", "dump", remote)
                last_error = None
                break
            except OperatorError as error:
                last_error = error
                if not self.screen_on():
                    self.shell("input", "keyevent", "KEYCODE_WAKEUP", check=False)
                    time.sleep(0.6)
                    self.shell("wm", "dismiss-keyguard", check=False)
                    self.unlock()
                time.sleep(0.5 + attempt * 0.5)
        if last_error is not None:
            raise last_error
        xml_text = self.shell("cat", remote)
        root = ET.fromstring(xml_text)
        nodes: list[UiNode] = []
        pattern = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
        for element in root.iter("node"):
            match = pattern.fullmatch(element.attrib.get("bounds", ""))
            if not match:
                continue
            nodes.append(
                UiNode(
                    text=element.attrib.get("text", ""),
                    resource_id=element.attrib.get("resource-id", ""),
                    content_desc=element.attrib.get("content-desc", ""),
                    clickable=element.attrib.get("clickable") == "true",
                    bounds=tuple(map(int, match.groups())),
                )
            )
        return nodes

    def find_nodes(
        self,
        *,
        text: str | None = None,
        resource_id: str | None = None,
        content_desc: str | None = None,
    ) -> list[UiNode]:
        matches = []
        for node in self.dump_ui():
            if text is not None and node.text != text:
                continue
            if resource_id is not None and node.resource_id != resource_id:
                continue
            if content_desc is not None and node.content_desc != content_desc:
                continue
            matches.append(node)
        return matches

    def try_ui_tars_recovery(
        self,
        *,
        failure_kind: str,
        target: str,
        error: str,
    ) -> bool:
        """Try one guarded recovery per missing target during this process."""
        attempt_key = (failure_kind, target)
        if attempt_key in self._ui_tars_recovery_attempted:
            return False
        self._ui_tars_recovery_attempted.add(attempt_key)
        try:
            from ui_tars_recovery import UiTarsRecoveryAgent

            result = UiTarsRecoveryAgent().recover(
                self,
                failure_kind=failure_kind,
                target=target,
                error=error,
            )
        except Exception as recovery_error:
            result = {
                "attempted": True,
                "recovered": False,
                "reason": f"recovery_error:{recovery_error}",
            }
        self._ui_tars_recovery_records.append(
            {
                "failure_kind": failure_kind,
                "target": target,
                **result,
            }
        )
        receipt = self.output_dir / "ui-tars-recovery-latest.json"
        receipt.write_text(
            json.dumps(
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "records": self._ui_tars_recovery_records,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return bool(result.get("recovered"))

    def tap_node(
        self,
        *,
        text: str | None = None,
        resource_id: str | None = None,
        content_desc: str | None = None,
        wait: float = 1.0,
    ) -> UiNode:
        nodes = self.find_nodes(text=text, resource_id=resource_id, content_desc=content_desc)
        if not nodes:
            criteria = {"text": text, "resource_id": resource_id, "content_desc": content_desc}
            error = f"找不到控件：{criteria}"
            if text is not None:
                recovered = self.try_ui_tars_recovery(
                    failure_kind="missing_text",
                    target=text,
                    error=error,
                )
            elif resource_id is not None:
                recovered = self.try_ui_tars_recovery(
                    failure_kind="missing_resource",
                    target=resource_id,
                    error=error,
                )
            else:
                recovered = False
            if recovered:
                nodes = self.find_nodes(
                    text=text,
                    resource_id=resource_id,
                    content_desc=content_desc,
                )
            if not nodes:
                raise OperatorError(error)
        node = nodes[0]
        x, y = node.center
        self.shell("input", "tap", str(x), str(y))
        time.sleep(wait)
        return node

    def wait_for_text(self, text: str, timeout: float = 8.0) -> UiNode:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nodes = self.find_nodes(text=text)
            if nodes:
                return nodes[0]
            time.sleep(0.5)
        error = f"等待界面“{text}”超时。"
        if self.try_ui_tars_recovery(
            failure_kind="missing_text",
            target=text,
            error=error,
        ):
            nodes = self.find_nodes(text=text)
            if nodes:
                return nodes[0]
        raise OperatorError(error)

    def wait_for_resource(self, resource_id: str, timeout: float = 8.0) -> UiNode:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nodes = self.find_nodes(resource_id=resource_id)
            if nodes:
                return nodes[0]
            time.sleep(0.5)
        error = f"等待控件“{resource_id}”超时。"
        if self.try_ui_tars_recovery(
            failure_kind="missing_resource",
            target=resource_id,
            error=error,
        ):
            nodes = self.find_nodes(resource_id=resource_id)
            if nodes:
                return nodes[0]
        raise OperatorError(error)

    def tap_resource(self, resource_id: str, wait: float = 1.0) -> UiNode:
        return self.tap_node(resource_id=resource_id, wait=wait)

    def scan_media(self, remote_path: str) -> None:
        self.shell(
            "am",
            "broadcast",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            f"file://{remote_path}",
        )

    def media_store_order(self, remote_dir: str) -> list[dict]:
        """Return images in the same deterministic order used by the XHS picker."""
        output = self.shell(
            "content",
            "query",
            "--uri",
            "content://media/external/images/media",
            "--projection",
            "_display_name:date_modified:_data",
        )
        relative_dir = remote_dir.removeprefix("/sdcard/")
        rows = []
        for line in output.splitlines():
            match = re.search(
                r"_display_name=([^,]+), date_modified=(\d+), _data=(.+)$",
                line,
            )
            if not match:
                continue
            name, modified, media_path = match.groups()
            if f"/{relative_dir}/" not in media_path:
                continue
            rows.append(
                {
                    "name": name,
                    "date_modified": int(modified),
                    "path": media_path,
                }
            )
        # Xiaohongshu's picker sorts by the media modification time, newest first.
        # File name is only a stable fallback and is never relied on because every
        # page receives a unique timestamp below.
        return sorted(
            rows,
            key=lambda row: (-row["date_modified"], row["name"]),
        )

    def push_asset(self, local_path: Path, remote_name: str) -> str:
        if not local_path.exists():
            raise OperatorError(f"素材不存在：{local_path}")
        self.shell("mkdir", "-p", REMOTE_DIR)
        remote_path = f"{REMOTE_DIR}/{remote_name}"
        self.adb("push", str(local_path), remote_path)
        self.scan_media(remote_path)
        return remote_path

    def article(self, article_id: str) -> Article:
        root = Path(self.config["article_root"]).expanduser()
        matches = sorted(path for path in root.glob(f"{article_id}_*") if path.is_dir())
        if len(matches) != 1:
            raise OperatorError(f"编号 {article_id} 应对应一个文章文件夹，当前找到 {len(matches)} 个。")
        manifest_path = matches[0] / "发布内容.json"
        if not manifest_path.exists():
            raise OperatorError(f"缺少发布清单：{manifest_path}")
        return Article(
            folder=matches[0],
            manifest=json.loads(manifest_path.read_text(encoding="utf-8")),
        )

    @staticmethod
    def png_size(path: Path) -> tuple[int, int]:
        with path.open("rb") as file:
            header = file.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            raise OperatorError(f"不是有效 PNG：{path}")
        return struct.unpack(">II", header[16:24])

    def check_article(
        self,
        article_id: str,
        allowed_statuses: tuple[str, ...] = ("ready",),
    ) -> dict:
        article = self.article(article_id)
        manifest = article.manifest
        errors: list[str] = []
        if manifest.get("id") != article_id:
            errors.append("清单 id 与文件夹编号不一致")
        if manifest.get("status") not in allowed_statuses:
            errors.append(
                "清单状态应为 "
                + " 或 ".join(allowed_statuses)
                + f"，当前为 {manifest.get('status')}"
            )
        title = str(manifest.get("title", "")).strip()
        if not 2 <= len(title) <= 20:
            errors.append(f"标题长度应为 2–20 字符，当前 {len(title)}")
        body = article.body
        if not body:
            errors.append("正文为空")
        if len(body) > 1000:
            errors.append(f"正文超过 1000 字符，当前 {len(body)}")
        required_tag = str(self.config.get("required_tag", "")).strip().lstrip("#")
        if int(manifest.get("content_rules_version", 1)) >= 2 and required_tag:
            tags = [
                str(value).strip().lstrip("#").strip()
                for value in manifest.get("tags", [])
            ]
            if required_tag not in tags:
                errors.append(f"新内容必须包含话题 #{required_tag}")
            if f"#{required_tag}" not in body:
                errors.append(f"新内容正文末尾必须包含 #{required_tag}")
        images = article.image_paths
        if not 1 <= len(images) <= 6:
            errors.append(f"图片数量应为 1–6，当前 {len(images)}")
        expected_size = tuple(manifest.get("expected_image_size", []))
        image_info = []
        for index, path in enumerate(images, 1):
            if not path.exists():
                errors.append(f"缺少第 {index} 张图片：{path}")
                continue
            try:
                size = self.png_size(path)
            except OperatorError as error:
                errors.append(str(error))
                continue
            if expected_size and size != expected_size:
                errors.append(f"第 {index} 张尺寸为 {size}，应为 {expected_size}")
            image_info.append(
                {"index": index, "name": path.name, "width": size[0], "height": size[1]}
            )
        return {
            "ok": not errors,
            "id": article_id,
            "folder": str(article.folder),
            "title": title,
            "title_length": len(title),
            "body_length": len(body),
            "images": image_info,
            "errors": errors,
        }

    def ready_article_ids(self, target_date: date | None = None) -> list[str]:
        root = Path(self.config["article_root"]).expanduser()
        ready: list[str] = []
        for manifest_path in sorted(root.glob("*_*/发布内容.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            article_id = str(manifest.get("id", ""))
            if manifest.get("status") != "ready" or not article_id:
                continue
            if target_date is not None:
                scheduled_at = str(manifest.get("scheduled_at", "")).strip()
                try:
                    scheduled_date = datetime.fromisoformat(scheduled_at).date()
                except ValueError:
                    continue
                if scheduled_date != target_date:
                    continue
            ready.append(article_id)
        return ready

    @staticmethod
    def preparation_date(value: str | None = None) -> date:
        if value:
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise OperatorError("日期必须使用 YYYY-MM-DD 格式。") from error
        return datetime.now().astimezone().date() + timedelta(days=1)

    def daily_check(self, limit: int = 3, target: str | None = None) -> dict:
        target_date = self.preparation_date(target)
        article_ids = self.ready_article_ids(target_date)[:limit]
        results = [self.check_article(article_id) for article_id in article_ids]
        return {
            "ok": all(result["ok"] for result in results),
            "target_date": target_date.isoformat(),
            "ready_count": len(self.ready_article_ids(target_date)),
            "selected_count": len(results),
            "limit": limit,
            "articles": results,
        }

    def scaffold_article(self, article_id: str, title: str) -> dict:
        if not re.fullmatch(r"(?:\d{3}|H\d{2})", article_id):
            raise OperatorError("固定文章使用三位数字；热点文章使用 H 加两位数字，例如 H04。")
        is_hot_topic = article_id.startswith("H")
        clean_title = re.sub(r'[\\/:*?"<>|]', "", title).strip()
        if not clean_title:
            raise OperatorError("文章标题不能为空。")
        root = Path(self.config["article_root"]).expanduser()
        existing = list(root.glob(f"{article_id}_*"))
        if existing:
            raise OperatorError(f"编号 {article_id} 已存在。")
        folder = root / f"{article_id}_{clean_title}"
        (folder / "页面导出").mkdir(parents=True)
        (folder / "配图").mkdir()
        (folder / "正文.txt").write_text(
            "在这里填写正文。\n\n",
            encoding="utf-8",
        )
        default_tags = [
            str(value).strip().lstrip("#")
            for value in self.config.get("default_tags", [])
            if str(value).strip()
        ]
        manifest = {
            "id": article_id,
            "status": "draft",
            "content_rules_version": 2,
            "visual_rules_version": 2,
            "content_type": "hot_topic" if is_hot_topic else "fixed_schedule",
            "title": title.strip(),
            "body_file": "正文.txt",
            "images": [],
            "expected_image_size": [1125, 1500],
            "tags": default_tags,
            "safety_sources": [],
        }
        if is_hot_topic:
            manifest["cover_badge"] = "热点话题"
        (folder / "发布内容.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "id": article_id,
            "title": title.strip(),
            "folder": str(folder),
            "next_step": "完成正文与图片后运行文章门禁；手机草稿验证成功才可标记 ready。",
        }

    def validate_visual_delivery(self, article_id: str) -> None:
        result = self.check_article(
            article_id,
            allowed_statuses=("draft", "designing", "ready", "published"),
        )
        if not result["ok"]:
            raise OperatorError("视觉交付验收未通过：" + "；".join(result["errors"]))

    def daily_drafts(self, limit: int = 3, target: str | None = None) -> dict:
        target_date = self.preparation_date(target)
        article_ids = self.ready_article_ids(target_date)[:limit]
        results = []
        for article_id in article_ids:
            results.append(self.create_draft(article_id))
        return {
            "ok": all(result["ok"] for result in results),
            "target_date": target_date.isoformat(),
            "selected_count": len(article_ids),
            "limit": limit,
            "articles": results,
        }

    def sync_article(
        self,
        article_id: str,
        allowed_statuses: tuple[str, ...] = ("ready", "published"),
    ) -> dict:
        # Published articles may still need their disposable phone album copies
        # refreshed after a corrected Figma export.
        check = self.check_article(article_id, allowed_statuses=allowed_statuses)
        if not check["ok"]:
            raise OperatorError("文章校验失败：" + "；".join(check["errors"]))
        article = self.article(article_id)
        remote_root = "/sdcard/Pictures/XiaohongshuPublish"
        album_name = f"{article_id}-{int(time.time())}"
        remote_dir = f"{remote_root}/{album_name}"

        # Every sync gets a fresh article-only album. Reusing a folder lets
        # Xiaohongshu keep a stale album cache and previously caused a foreign
        # article image to be selected from “全部”. Remove only prior disposable
        # session folders for this exact article id.
        old_dirs = self.shell(
            "find",
            remote_root,
            "-maxdepth",
            "1",
            "-mindepth",
            "1",
            "-type",
            "d",
            "-name",
            f"{article_id}-*",
            check=False,
        ).splitlines()
        for old_dir in old_dirs:
            if not old_dir.startswith(f"{remote_root}/{article_id}-"):
                continue
            old_paths = self.shell(
                "find", old_dir, "-maxdepth", "1", "-type", "f", check=False
            ).splitlines()
            for old_path in old_paths:
                if old_path.startswith(f"{old_dir}/"):
                    self.shell("rm", "-f", old_path)
                    self.scan_media(old_path)
            self.shell("rm", "-r", old_dir)
        self.shell("mkdir", "-p", remote_dir)

        remote_images = []
        image_hashes = []
        for index, local_path in enumerate(article.image_paths, 1):
            remote_path = f"{remote_dir}/{index:02d}.png"
            self.adb("push", str(local_path), remote_path)
            local_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
            remote_hash = self.shell("sha256sum", remote_path).split()[0]
            if remote_hash != local_hash:
                raise OperatorError(
                    f"手机端第 {index} 张图片哈希不一致，已停止草稿同步。"
                )
            remote_images.append(remote_path)
            image_hashes.append(
                {
                    "index": index,
                    "name": f"{index:02d}.png",
                    "sha256": local_hash,
                }
            )

        # MediaStore timestamps, rather than file names, control the order in the
        # Xiaohongshu gallery. Give 01 the newest timestamp and space every page
        # by one minute so the order cannot become random when files are copied
        # within the same second.
        newest = datetime.now().astimezone().replace(microsecond=0)
        for index, remote_path in enumerate(remote_images):
            modified = newest - timedelta(minutes=index)
            self.shell(
                "touch",
                "-m",
                "-d",
                modified.strftime("%Y-%m-%dT%H:%M:%S"),
                remote_path,
            )
            self.scan_media(remote_path)

        expected_order = [Path(path).name for path in remote_images]
        media_rows: list[dict] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            media_rows = self.media_store_order(remote_dir)
            actual_order = [row["name"] for row in media_rows]
            if actual_order == expected_order:
                break
            time.sleep(0.5)
        else:
            raise OperatorError(
                "手机媒体库图片顺序未稳定为 "
                f"{'→'.join(expected_order)}，当前为 {'→'.join(actual_order)}。"
            )
        return {
            "ok": True,
            "id": article_id,
            "album_name": album_name,
            "remote_dir": remote_dir,
            "images": remote_images,
            "image_hashes": image_hashes,
            "media_store_order": expected_order,
            "media_store_rows": media_rows,
        }

    def fill_article(
        self,
        article_id: str,
        allowed_statuses: tuple[str, ...] = ("ready",),
    ) -> dict:
        check = self.check_article(article_id, allowed_statuses=allowed_statuses)
        if not check["ok"]:
            raise OperatorError("文章校验失败：" + "；".join(check["errors"]))
        article = self.article(article_id)
        self.paste_into("com.xingin.xhs:id/editTitle", article.manifest["title"])
        self.paste_into("com.xingin.xhs:id/postNoteEditContentView", article.body)
        topics = [
            str(value).strip().lstrip("#").strip()
            for value in article.manifest.get("tags", [])
            if str(value).strip().lstrip("#").strip()
        ]
        if not topics:
            raise OperatorError("发布清单缺少话题标签。")
        last_topic = topics[-1]
        topic_suggestion = self.wait_for_text(last_topic, timeout=5)
        if topic_suggestion.resource_id != "com.xingin.xhs:id/tvTopicName":
            raise OperatorError(f"小红书未把 #{last_topic} 识别为正式话题。")
        nodes = self.dump_ui()
        title_nodes = [
            node for node in nodes if node.resource_id == "com.xingin.xhs:id/editTitle"
        ]
        body_nodes = [
            node
            for node in nodes
            if node.resource_id == "com.xingin.xhs:id/postNoteEditContentView"
        ]
        title = title_nodes[0].text if title_nodes else ""
        body = body_nodes[0].text if body_nodes else ""
        normalize = lambda value: re.sub(r"\n{2,}", "\n", value).strip()
        body_matches = normalize(body) == normalize(article.body)
        screenshot = self.screenshot(f"article-{article_id}-filled.png")
        return {
            "ok": (
                title == article.manifest["title"]
                and body_matches
                and topic_suggestion.resource_id
                == "com.xingin.xhs:id/tvTopicName"
            ),
            "id": article_id,
            "title": title,
            "title_matches": title == article.manifest["title"],
            "body_length": len(body),
            "body_matches": body_matches,
            "topics": topics,
            "topics_verified": True,
            "topic_verification": "paste_parser_exact_suggestion",
            "screenshot": str(screenshot),
        }

    @staticmethod
    def normalize_editor_text(value: str) -> str:
        # Xiaohongshu's detail page injects tab-only lines between paragraphs,
        # while the compose editor and source file use blank lines. Compare the
        # visible text, not those UI-only whitespace differences.
        lines = [line.strip() for line in value.replace("\r", "\n").split("\n")]
        return "\n".join(line for line in lines if line)

    def find_profile_post_card(self, title: str, max_scrolls: int = 6) -> UiNode:
        """Find a post on the account profile even if the grid kept its old offset."""
        for attempt in range(max_scrolls + 1):
            cards = [
                node
                for node in self.dump_ui()
                if node.resource_id == "com.xingin.xhs:id/card_view"
                and title in node.content_desc
            ]
            if len(cards) == 1:
                return cards[0]
            if len(cards) > 1:
                raise OperatorError(
                    f"账号主页标题“{title}”出现 {len(cards)} 次，已停止验收。"
                )
            if attempt < max_scrolls:
                # Pull the profile grid toward its newest posts at the top.
                self.shell("input", "swipe", "540", "700", "540", "1800", "450")
                time.sleep(1.0)
        raise OperatorError("发布后账号主页未出现对应文章卡片。")

    def verify_current_compose(self, article_id: str) -> dict:
        article = self.article(article_id)
        nodes = self.dump_ui()
        title = next(
            (
                node.text
                for node in nodes
                if node.resource_id == "com.xingin.xhs:id/editTitle"
            ),
            "",
        )
        body = next(
            (
                node.text
                for node in nodes
                if node.resource_id
                == "com.xingin.xhs:id/postNoteEditContentView"
            ),
            "",
        )
        image_tiles = sum(
            node.resource_id == "com.xingin.xhs:id/capaItemImage" for node in nodes
        )
        return {
            "ok": (
                title == article.manifest["title"]
                and self.normalize_editor_text(body)
                == self.normalize_editor_text(article.body)
                and image_tiles >= min(5, len(article.image_paths))
            ),
            "title": title,
            "body_length": len(body),
            "visible_image_tiles": image_tiles,
        }

    def open_profile(self) -> dict:
        self.tap_resource("com.xingin.xhs:id/index_me", wait=1.8)
        nodes = self.dump_ui()
        expected_name = self.config["account_name"]
        name = next((node.text for node in nodes if node.text == expected_name), "")
        if not name:
            nickname_ids = {
                "com.xingin.xhs:id/profile_new_page_avatar_card_nickname",
                "com.xingin.xhs:id/tv_nickname",
            }
            name = next(
                (node.text for node in nodes if node.resource_id in nickname_ids),
                "",
            )
        if not name:
            raise OperatorError("未能打开账号主页。")
        return {"account_name": name}

    def open_draft_box(self) -> UiNode:
        # The profile tab often renders the draft entry several seconds after
        # the nickname and post grid, especially immediately after saving.
        draft_badge = self.wait_for_resource("com.xingin.xhs:id/localDraft", timeout=20)
        for attempt in range(3):
            candidates = self.find_nodes(resource_id="com.xingin.xhs:id/ivDraftIcon")
            target = candidates[0] if candidates else draft_badge
            x, y = target.center
            self.shell("input", "tap", str(x), str(y))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.find_nodes(text="本地草稿"):
                    return draft_badge
                time.sleep(0.5)
            if attempt < 2:
                time.sleep(0.8)
        raise OperatorError("草稿箱入口连续三次未打开。")

    def open_current_draft_editor(self) -> str:
        """Open the current local draft across old and current XHS navigation."""
        deadline = time.monotonic() + 25
        legacy_badges = []
        while time.monotonic() < deadline:
            legacy_badges = self.find_nodes(
                resource_id="com.xingin.xhs:id/localDraft"
            )
            if legacy_badges:
                break
            time.sleep(0.5)
        if legacy_badges:
            self.open_draft_box()
            return "legacy_list"

        # Current XHS versions moved local drafts from the profile grid into
        # 发布 → 从相册选择 → 草稿箱. Opening it lands on the image preview;
        # continue to the compose page, where title/body are verified before
        # any publish button can be used.
        self.tap_resource("com.xingin.xhs:id/index_post", wait=1.5)
        self.wait_for_text("从相册选择", timeout=8)
        self.tap_node(text="从相册选择", wait=2.0)
        draft_entry = self.wait_for_resource(
            "com.xingin.xhs:id/rightTextEntrance", timeout=8
        )
        if draft_entry.text != "草稿箱":
            raise OperatorError("发布入口右上角不是草稿箱，已停止。")
        x, y = draft_entry.center
        self.shell("input", "tap", str(x), str(y))
        self.wait_for_resource("com.xingin.xhs:id/bottomGoNext", timeout=8)
        self.tap_resource("com.xingin.xhs:id/bottomGoNext", wait=2.0)
        self.wait_for_resource("com.xingin.xhs:id/capa_light_edit_next", timeout=8)
        self.tap_resource("com.xingin.xhs:id/capa_light_edit_next", wait=2.0)
        if self.find_nodes(resource_id="com.xingin.xhs:id/text_refuse"):
            self.tap_resource("com.xingin.xhs:id/text_refuse", wait=1.0)
        self.wait_for_resource("com.xingin.xhs:id/editTitle", timeout=8)
        return "current_editor"

    def find_draft_title(self, title: str, max_scrolls: int = 8) -> UiNode:
        """Find a draft title even when it starts below the visible grid."""
        for attempt in range(max_scrolls + 1):
            nodes = self.find_nodes(text=title)
            if len(nodes) == 1:
                return nodes[0]
            if len(nodes) > 1:
                raise OperatorError(
                    f"草稿标题“{title}”出现 {len(nodes)} 次，已停止以避免选错。"
                )
            if attempt < max_scrolls:
                self.shell("input", "swipe", "540", "1900", "540", "700", "450")
                time.sleep(1.0)
        raise OperatorError(f"草稿箱中找不到待发布文章“{title}”。")

    def find_album_name(self, album_name: str, max_scrolls: int = 8) -> UiNode:
        """Find an article-only album in Xiaohongshu's folder list."""
        for attempt in range(max_scrolls + 1):
            nodes = self.find_nodes(
                text=album_name,
                resource_id="com.xingin.xhs:id/albumFolderNameTv",
            )
            if len(nodes) == 1:
                return nodes[0]
            if len(nodes) > 1:
                raise OperatorError(
                    f"专属相册“{album_name}”出现 {len(nodes)} 次，已停止。"
                )
            if attempt < max_scrolls:
                self.shell("input", "swipe", "540", "2000", "540", "700", "450")
                time.sleep(0.7)
        raise OperatorError(f"相册列表中找不到文章专属相册“{album_name}”。")

    def delete_draft_from_open_box(self, article_id: str) -> dict:
        """Delete exactly one matching local draft while the draft box is open."""
        title = self.article(article_id).manifest["title"]
        title_nodes = self.find_nodes(text=title)
        if len(title_nodes) != 1:
            raise OperatorError(
                f"替换草稿要求标题“{title}”恰好存在一次，当前找到 {len(title_nodes)} 次。"
            )
        title_node = title_nodes[0]
        cards = [
            node
            for node in self.find_nodes(resource_id="com.xingin.xhs:id/card_view")
            if (
                node.bounds[0] <= title_node.center[0] <= node.bounds[2]
                and node.bounds[1] <= title_node.center[1] <= node.bounds[3]
            )
        ]
        if len(cards) != 1:
            raise OperatorError("无法确认待替换草稿所在的卡片。")
        card = cards[0]
        delete_nodes = [
            node
            for node in self.find_nodes(resource_id="com.xingin.xhs:id/iv_delete")
            if (
                card.bounds[0] <= node.center[0] <= card.bounds[2]
                and card.bounds[1] <= node.center[1] <= card.bounds[3]
            )
        ]
        if len(delete_nodes) != 1:
            raise OperatorError("无法确认待替换草稿的删除按钮。")
        x, y = delete_nodes[0].center
        self.shell("input", "tap", str(x), str(y))
        # UIAutomator can block while Xiaohongshu's native confirmation dialog
        # owns the accessibility window. This operator is intentionally bound to
        # the configured 1080×2280 handset, so confirm at the stable right-button
        # position and verify the result after the dialog closes.
        time.sleep(0.8)
        self.shell("input", "tap", "710", "1264")
        time.sleep(1.5)
        if self.find_nodes(text=title):
            raise OperatorError("旧草稿删除后仍然存在，已停止替换。")
        return {"deleted": True, "title": title}

    def delete_all_matching_drafts_from_open_box(self, article_id: str) -> dict:
        """Delete every local draft whose title exactly matches the article."""
        title = self.article(article_id).manifest["title"]
        deleted = 0
        for attempt in range(9):
            if self.find_nodes(text=title):
                break
            if attempt < 8:
                self.shell("input", "swipe", "540", "1900", "540", "700", "450")
                time.sleep(0.7)
        else:
            return {"deleted": 0, "title": title, "remaining": 0}
        for _ in range(10):
            title_nodes = self.find_nodes(text=title)
            if not title_nodes:
                break
            # Delete the lowest visible matching card first so the upper card
            # remains stable when the masonry grid closes the gap.
            title_node = sorted(
                title_nodes, key=lambda node: node.bounds[1], reverse=True
            )[0]
            cards = [
                node
                for node in self.find_nodes(
                    resource_id="com.xingin.xhs:id/card_view"
                )
                if (
                    node.bounds[0] <= title_node.center[0] <= node.bounds[2]
                    and node.bounds[1] <= title_node.center[1] <= node.bounds[3]
                )
            ]
            if len(cards) != 1:
                raise OperatorError("无法确认重复草稿所在的唯一卡片。")
            card = cards[0]
            delete_nodes = [
                node
                for node in self.find_nodes(
                    resource_id="com.xingin.xhs:id/iv_delete"
                )
                if (
                    card.bounds[0] <= node.center[0] <= card.bounds[2]
                    and card.bounds[1] <= node.center[1] <= card.bounds[3]
                )
            ]
            if len(delete_nodes) != 1:
                raise OperatorError("无法确认重复草稿的删除按钮。")
            x, y = delete_nodes[0].center
            self.shell("input", "tap", str(x), str(y))
            time.sleep(0.8)
            self.shell("input", "tap", "710", "1264")
            time.sleep(1.5)
            deleted += 1
        remaining = len(self.find_nodes(text=title))
        if remaining:
            raise OperatorError(
                f"删除重复草稿后仍有 {remaining} 篇同标题草稿。"
            )
        return {"deleted": deleted, "title": title, "remaining": remaining}

    def ensure_xhs_home(self) -> None:
        nodes = self.dump_ui()
        if any(node.resource_id == "com.xingin.xhs:id/index_post" for node in nodes):
            return
        # A force-stop is deterministic and safely discards any interrupted,
        # unsaved compose screen from a previous automation attempt.
        self.shell("am", "force-stop", PACKAGE)
        time.sleep(0.5)
        self.shell(
            "monkey",
            "-p",
            PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(3.0)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            nodes = self.dump_ui()
            unfinished = [
                node
                for node in nodes
                if node.resource_id
                == "com.xingin.xhs:id/btn_unfinished_draft_dialog_exit"
            ]
            if unfinished:
                x, y = unfinished[0].center
                self.shell("input", "tap", str(x), str(y))
                time.sleep(1.0)
                continue
            if any(
                node.resource_id == "com.xingin.xhs:id/index_post" for node in nodes
            ):
                return
            time.sleep(0.5)
        raise OperatorError("打开小红书首页超时。")

    def open_schedule_picker(
        self, article_id: str, scheduled_at: str
    ) -> list[UiNode]:
        target = datetime.fromisoformat(scheduled_at)
        if target <= datetime.now(target.tzinfo):
            raise OperatorError(f"定时发布时间必须晚于当前时间：{scheduled_at}")
        advanced = self.find_nodes(text="高级选项")
        if len(advanced) != 1:
            raise OperatorError("发布页找不到唯一的“高级选项”。")
        x, y = advanced[0].center
        self.shell("input", "tap", str(x), str(y))
        self.wait_for_text("定时发布", timeout=8)
        schedule = self.find_nodes(text="定时发布")
        if len(schedule) != 1:
            raise OperatorError("高级选项中找不到唯一的“定时发布”。")
        x, y = schedule[0].center
        self.shell("input", "tap", str(x), str(y))
        time.sleep(1.0)
        nodes = self.dump_ui()
        self.record_stage(
            article_id,
            "已打开小红书定时发布设置",
            scheduled_at=scheduled_at,
            visible_texts=[node.text for node in nodes if node.text],
        )
        return nodes

    @staticmethod
    def picker_selected_text(
        nodes: list[UiNode], x_min: int, x_max: int
    ) -> str:
        selected = [
            node.text
            for node in nodes
            if node.text
            and x_min <= node.center[0] <= x_max
            and 1790 <= node.bounds[1] <= 1820
            and 1860 <= node.bounds[3] <= 1885
        ]
        if len(selected) != 1:
            raise OperatorError(
                f"无法确认定时滚轮当前值，列范围={x_min}-{x_max}，值={selected}"
            )
        return selected[0]

    def step_schedule_wheel(self, x: int, steps: int) -> None:
        if not steps:
            return
        start_y, end_y = (1905, 1795) if steps > 0 else (1795, 1905)
        for _ in range(abs(steps)):
            self.shell(
                "input",
                "swipe",
                str(x),
                str(start_y),
                str(x),
                str(end_y),
                "140",
            )
            time.sleep(0.2)
        time.sleep(0.8)

    def set_schedule_picker(self, target: datetime) -> dict:
        today = datetime.now(target.tzinfo).date()
        day_steps = (target.date() - today).days
        if day_steps < 0:
            raise OperatorError("小红书定时发布不能选择过去日期。")
        self.step_schedule_wheel(200, day_steps)
        for _ in range(4):
            nodes = self.dump_ui()
            selected_date = self.picker_selected_text(nodes, 50, 360)
            match = re.search(r"(\d+)月(\d+)日", selected_date)
            if not match:
                raise OperatorError(f"无法解析日期滚轮值：{selected_date}")
            current_date = date(
                target.year, int(match.group(1)), int(match.group(2))
            )
            delta = (target.date() - current_date).days
            if not delta:
                break
            self.step_schedule_wheel(200, delta)
        else:
            raise OperatorError("日期滚轮未能稳定到目标值。")

        for _ in range(4):
            nodes = self.dump_ui()
            current_hour = int(self.picker_selected_text(nodes, 450, 650))
            delta = target.hour - current_hour
            if not delta:
                break
            self.step_schedule_wheel(540, delta)
        else:
            raise OperatorError("小时滚轮未能稳定到目标值。")

        for _ in range(4):
            nodes = self.dump_ui()
            current_minute = int(self.picker_selected_text(nodes, 780, 970))
            delta = target.minute - current_minute
            if not delta:
                break
            self.step_schedule_wheel(875, delta)
        else:
            raise OperatorError("分钟滚轮未能稳定到目标值。")

        nodes = self.dump_ui()
        selected_date = self.picker_selected_text(nodes, 50, 360)
        selected_hour = int(self.picker_selected_text(nodes, 450, 650))
        selected_minute = int(self.picker_selected_text(nodes, 780, 970))
        expected_date = f"{target.month}月{target.day}日"
        if (
            expected_date not in selected_date
            or selected_hour != target.hour
            or selected_minute != target.minute
        ):
            raise OperatorError(
                "定时发布滚轮校验失败："
                f"{selected_date} {selected_hour:02d}:{selected_minute:02d}"
            )
        return {
            "date": selected_date,
            "hour": selected_hour,
            "minute": selected_minute,
        }

    def schedule_current_compose(self, article_id: str, scheduled_at: str) -> dict:
        target = datetime.fromisoformat(scheduled_at)
        self.open_schedule_picker(article_id, scheduled_at)
        selected = self.set_schedule_picker(target)
        confirm = self.find_nodes(text="确定")
        if len(confirm) != 1:
            raise OperatorError("定时发布日期时间选择器找不到唯一的“确定”。")
        x, y = confirm[0].center
        self.shell("input", "tap", str(x), str(y))
        time.sleep(1.0)

        weekdays = "一二三四五六日"
        expected_label = (
            f"{target.month}月{target.day}日周{weekdays[target.weekday()]} "
            f"{target.hour:02d}:{target.minute:02d}发布"
        )
        if not self.find_nodes(text=expected_label):
            raise OperatorError(
                f"高级选项未显示目标预约时间“{expected_label}”。"
            )
        schedule_screenshot = self.screenshot(
            f"article-{article_id}-schedule-confirmed-before-submit.png"
        )
        self.record_stage(
            article_id,
            "小红书预约时间已核对",
            scheduled_at=scheduled_at,
            label=expected_label,
            selected=selected,
            screenshot=str(schedule_screenshot),
        )

        # Close the advanced-options sheet without leaving the compose page.
        self.shell("input", "swipe", "540", "1110", "540", "2100", "350")
        time.sleep(0.8)
        compose = self.verify_current_compose(article_id)
        if not compose["ok"]:
            raise OperatorError("定时提交前标题、正文或图片数量校验失败。")
        buttons = [
            node
            for node in self.find_nodes(
                resource_id="com.xingin.xhs:id/capaBigPostBtn"
            )
            if node.text == "定时发布"
        ]
        if len(buttons) != 1:
            raise OperatorError("发布页找不到唯一的“定时发布”提交按钮。")
        x, y = buttons[0].center
        self.shell("input", "tap", str(x), str(y))
        self.record_stage(article_id, "正在提交小红书原生定时发布")

        deadline = time.monotonic() + 75
        blocked_words = {"验证码", "账号异常", "发布失败", "内容违规"}
        while time.monotonic() < deadline:
            time.sleep(2.0)
            nodes = self.dump_ui()
            visible_texts = {node.text for node in nodes if node.text}
            blocked = sorted(blocked_words.intersection(visible_texts))
            if blocked:
                raise OperatorError("定时发布被小红书拦截：" + "、".join(blocked))
            on_compose = any(
                node.resource_id == "com.xingin.xhs:id/editTitle"
                for node in nodes
            )
            on_home = any(
                node.resource_id == "com.xingin.xhs:id/index_post"
                for node in nodes
            )
            if on_home and not on_compose:
                break
        else:
            raise OperatorError("等待小红书接收定时发布任务超时。")

        result_screenshot = self.screenshot(
            f"article-{article_id}-scheduled-result.png"
        )
        manifest_path = self.article(article_id).folder / "发布内容.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "scheduled"
        manifest["xhs_scheduled_at"] = scheduled_at
        manifest["xhs_schedule_verified_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z"
        )
        manifest["xhs_schedule_screenshot"] = str(schedule_screenshot)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.record_stage(article_id, "小红书原生定时发布已提交", ok=True)
        return {
            "ok": True,
            "id": article_id,
            "scheduled_at": scheduled_at,
            "label": expected_label,
            "schedule_screenshot": str(schedule_screenshot),
            "result_screenshot": str(result_screenshot),
        }

    def schedule_existing_draft(self, article_id: str) -> dict:
        article = self.article(article_id)
        scheduled_at = str(article.manifest.get("scheduled_at", "")).strip()
        if not (
            article.manifest.get("phone_draft_verified_at")
            and article.manifest.get("phone_draft_signature")
        ):
            raise OperatorError("文章没有已核验手机草稿记录，不能走原草稿改定时路径。")
        self.begin()
        try:
            self.ensure_xhs_home()
            profile = self.open_profile()
            if profile["account_name"] != self.config["account_name"]:
                raise OperatorError(
                    f"当前账号名为“{profile['account_name']}”，应为"
                    f"“{self.config['account_name']}”。"
                )
            draft_badge = self.open_draft_box()
            title_node = self.find_draft_title(article.manifest["title"])
            x, y = title_node.center
            self.shell("input", "tap", str(x), str(y))
            time.sleep(2.0)
            self.wait_for_resource("com.xingin.xhs:id/editTitle", timeout=8)
            compose = self.verify_current_compose(article_id)
            if not compose["ok"]:
                raise OperatorError("原草稿标题、正文或图片数量与文章清单不一致。")
            self.record_stage(
                article_id,
                "直接把已核验草稿改为小红书定时发布",
                draft_badge=draft_badge.text,
            )
            result = self.schedule_current_compose(article_id, scheduled_at)
            self.open_profile()
            badges = self.find_nodes(
                resource_id="com.xingin.xhs:id/localDraft"
            )
            cleanup = {"deleted": 0, "remaining": 0}
            if badges:
                self.open_draft_box()
                cleanup = self.delete_all_matching_drafts_from_open_box(
                    article_id
                )
                self.shell("input", "keyevent", "KEYCODE_BACK")
            result["draft_cleanup"] = cleanup
            return result
        finally:
            self.end()

    def create_draft(
        self,
        article_id: str,
        replace_existing: bool = False,
        schedule_at: str | None = None,
    ) -> dict:
        started = time.monotonic()
        existing_article = self.article(article_id)
        if (
            schedule_at
            and existing_article.manifest.get("status") == "scheduled"
            and existing_article.manifest.get("xhs_scheduled_at") == schedule_at
            and existing_article.manifest.get("xhs_schedule_verified_at")
        ):
            return {
                "ok": True,
                "id": article_id,
                "scheduled_at": schedule_at,
                "existing": True,
                "duplicate_protected": True,
            }
        self.validate_visual_delivery(article_id)
        self.record_stage(article_id, "同步图片")
        sync = self.sync_article(
            article_id,
            allowed_statuses=("draft", "designing", "ready", "published"),
        )
        self.record_stage(article_id, "唤醒手机")
        self.begin()
        try:
            self.record_stage(article_id, "打开小红书首页")
            self.ensure_xhs_home()
            # Idempotency: if a prior attempt already saved and validated the
            # same article title, do not create a duplicate local draft.
            profile = self.open_profile()
            if profile["account_name"] != self.config["account_name"]:
                raise OperatorError(
                    f"当前账号名为“{profile['account_name']}”，应为"
                    f"“{self.config['account_name']}”。"
                )
            existing_badges = self.find_nodes(
                resource_id="com.xingin.xhs:id/localDraft"
            )
            if existing_badges and not schedule_at:
                draft_badge = self.open_draft_box()
                existing = self.find_nodes(
                    text=self.article(article_id).manifest["title"]
                )
                if existing:
                    if replace_existing or schedule_at:
                        self.record_stage(article_id, "删除旧草稿以修正图片顺序")
                        self.delete_draft_from_open_box(article_id)
                    else:
                        verified_screenshot = self.screenshot(
                            f"article-{article_id}-draft-verified.png"
                        )
                        self.record_stage(article_id, "草稿已存在并验证", ok=True)
                        return {
                            "ok": True,
                            "id": article_id,
                            "images": len(sync["images"]),
                            "draft_badge": draft_badge.text,
                            "verified_title": existing[0].text,
                            "existing": True,
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                            "screenshot": str(verified_screenshot),
                        }
                self.shell("input", "keyevent", "KEYCODE_BACK")
                time.sleep(0.8)
            self.record_stage(article_id, "进入相册")
            self.tap_resource("com.xingin.xhs:id/index_post", wait=1.0)
            self.wait_for_text("从相册选择")
            self.tap_node(text="从相册选择", wait=1.5)
            self.wait_for_resource("com.xingin.xhs:id/albumPopLayout")
            album_labels = self.find_nodes(
                resource_id="com.xingin.xhs:id/albumNameTv"
            )
            if album_labels:
                x, y = album_labels[0].center
                self.shell("input", "tap", str(x), str(y))
                time.sleep(0.8)
            else:
                self.tap_resource("com.xingin.xhs:id/albumPopLayout", wait=0.8)
            album_node = self.find_album_name(sync["album_name"])
            x, y = album_node.center
            self.shell("input", "tap", str(x), str(y))
            time.sleep(1.2)

            selectable = sorted(
                self.find_nodes(resource_id="com.xingin.xhs:id/selectableLayout"),
                key=lambda node: (node.bounds[1], node.bounds[0]),
            )
            image_count = len(sync["images"])
            if len(selectable) < image_count:
                raise OperatorError(
                    f"相册只显示 {len(selectable)} 个可选项，需要 {image_count} 个。"
                )
            self.record_stage(article_id, "选择图片", image_count=image_count)
            # sync_article has already made the visible gallery order exactly
            # 01 → 06. Tap top-to-bottom, left-to-right; the post preserves this
            # tap sequence and therefore always starts with 01 (the cover).
            selected_nodes = selectable[:image_count]
            for node in selected_nodes:
                x, y = node.center
                self.shell("input", "tap", str(x), str(y))
                time.sleep(0.15)

            next_node = self.wait_for_resource("com.xingin.xhs:id/bottomGoNext")
            if next_node.text != f"下一步({image_count})":
                raise OperatorError(
                    f"图片选中数量不正确：{next_node.text}，应为 下一步({image_count})"
                )
            expected_order = [path.name for path in self.article(article_id).image_paths]
            order_screenshot = self.screenshot(
                f"article-{article_id}-image-order.png"
            )
            order_details = {
                "expected_post_order": expected_order,
                "gallery_display_order": sync["media_store_order"],
                "tap_order": sync["media_store_order"],
                "selection_rule": "媒体库固定为01→06，按画面从左到右、从上到下点击",
                "cover_is_first": sync["media_store_order"][0] == "01.png",
                "tap_positions": [node.center for node in selected_nodes],
                "selected_count": image_count,
                "screenshot": str(order_screenshot),
            }
            order_receipt = self.output_dir / f"article-{article_id}-image-order.json"
            order_receipt.write_text(
                json.dumps(
                    {
                        "article_id": article_id,
                        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        **order_details,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.record_stage(
                article_id,
                "图片顺序已核对",
                **order_details,
                receipt=str(order_receipt),
            )
            x, y = next_node.center
            self.shell("input", "tap", str(x), str(y))
            time.sleep(2.0)

            self.wait_for_resource("com.xingin.xhs:id/capa_light_edit_next")
            self.tap_resource("com.xingin.xhs:id/capa_light_edit_next", wait=2.0)
            if self.find_nodes(resource_id="com.xingin.xhs:id/text_refuse"):
                self.tap_resource("com.xingin.xhs:id/text_refuse", wait=1.0)

            self.record_stage(article_id, "填写标题和正文")
            self.wait_for_resource("com.xingin.xhs:id/editTitle", timeout=8)
            fill = self.fill_article(
                article_id,
                allowed_statuses=("draft", "designing", "ready", "published"),
            )
            if not fill["ok"]:
                raise OperatorError("标题或正文写入后校验失败。")
            self.shell("input", "keyevent", "KEYCODE_BACK")
            time.sleep(0.5)
            if schedule_at:
                return self.schedule_current_compose(article_id, schedule_at)
            self.record_stage(article_id, "保存草稿")
            save_buttons = self.find_nodes(
                resource_id="com.xingin.xhs:id/capaSaveDraft"
            )
            if save_buttons:
                x, y = save_buttons[0].center
                self.shell("input", "tap", str(x), str(y))
                time.sleep(1.0)
            else:
                self.tap_resource("com.xingin.xhs:id/capaTopSaveDraftBtn", wait=1.0)
            # As with the delete dialog, accessibility inspection may block while
            # this native confirmation dialog is open. Confirm at the stable
            # right-button position for the configured 1080×2280 handset.
            time.sleep(0.8)
            self.shell("input", "tap", "710", "1228")
            time.sleep(3.0)

            self.record_stage(article_id, "验证草稿")
            self.open_profile()
            draft_badge = self.open_draft_box()
            title = self.find_draft_title(self.article(article_id).manifest["title"])
            verified_screenshot = self.screenshot(f"article-{article_id}-draft-verified.png")
            self.shell("input", "keyevent", "KEYCODE_BACK")
            self.record_stage(article_id, "草稿已验证", ok=True)
            return {
                "ok": True,
                "id": article_id,
                "images": image_count,
                "draft_badge": draft_badge.text,
                "verified_title": title.text,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "screenshot": str(verified_screenshot),
            }
        finally:
            self.end()

    def publish_article(self, article_id: str) -> dict:
        started = time.monotonic()
        self.validate_visual_delivery(article_id)
        check = self.check_article(article_id)
        if not check["ok"]:
            raise OperatorError("文章校验失败：" + "；".join(check["errors"]))
        self.record_stage(article_id, "准备发布")
        self.begin()
        try:
            self.ensure_xhs_home()
            profile = self.open_profile()
            if profile["account_name"] != self.config["account_name"]:
                raise OperatorError(
                    f"当前账号名为“{profile['account_name']}”，应为"
                    f"“{self.config['account_name']}”。"
                )
            draft_mode = self.open_current_draft_editor()
            if draft_mode == "legacy_list":
                title_node = self.find_draft_title(
                    self.article(article_id).manifest["title"]
                )
                x, y = title_node.center
                self.shell("input", "tap", str(x), str(y))
                time.sleep(2.0)
                self.wait_for_resource("com.xingin.xhs:id/editTitle", timeout=8)
            compose = self.verify_current_compose(article_id)
            if not compose["ok"]:
                raise OperatorError(
                    "发布前校验失败：标题、正文或图片数量与文章清单不一致。"
                )
            preflight = self.screenshot(f"article-{article_id}-publish-preflight.png")
            self.record_stage(article_id, "发布前校验通过", **compose)

            buttons = self.find_nodes(
                resource_id="com.xingin.xhs:id/capaBigPostBtn"
            )
            if not buttons:
                buttons = self.find_nodes(
                    resource_id="com.xingin.xhs:id/capaTopPostBtn"
                )
            if not buttons:
                raise OperatorError("找不到“发布笔记”按钮。")
            x, y = buttons[0].center
            self.shell("input", "tap", str(x), str(y))
            self.record_stage(article_id, "正在上传")

            deadline = time.monotonic() + 75
            blocked_words = {"验证码", "账号异常", "发布失败", "内容违规"}
            while time.monotonic() < deadline:
                time.sleep(2.0)
                nodes = self.dump_ui()
                visible_texts = {node.text for node in nodes if node.text}
                blocked = sorted(blocked_words.intersection(visible_texts))
                if blocked:
                    raise OperatorError("发布被小红书拦截：" + "、".join(blocked))
                on_compose = any(
                    node.resource_id == "com.xingin.xhs:id/editTitle"
                    for node in nodes
                )
                on_home = any(
                    node.resource_id == "com.xingin.xhs:id/index_post"
                    for node in nodes
                )
                if on_home and not on_compose:
                    break
            else:
                raise OperatorError("等待小红书完成上传超时。")

            self.open_profile()
            article = self.article(article_id)
            post_card = self.find_profile_post_card(article.manifest["title"])
            x, y = post_card.center
            self.shell("input", "tap", str(x), str(y))
            time.sleep(2.0)
            detail_nodes = self.dump_ui()
            detail_title = next(
                (
                    node.text
                    for node in detail_nodes
                    if node.resource_id == "com.xingin.xhs:id/noteTitleTV"
                ),
                "",
            )
            detail_body = next(
                (
                    node.text
                    for node in detail_nodes
                    if node.resource_id == "com.xingin.xhs:id/imageNoteTextView"
                ),
                "",
            )
            privacy = next(
                (
                    node.text
                    for node in detail_nodes
                    if node.resource_id == "com.xingin.xhs:id/notePrivacyTv"
                ),
                "",
            )
            if (
                detail_title != article.manifest["title"]
                or self.normalize_editor_text(detail_body)
                != self.normalize_editor_text(article.body)
                or privacy != "公开可见"
            ):
                raise OperatorError("发布结果页校验失败，未确认文章为公开可见。")
            post_screenshot = self.screenshot(
                f"article-{article_id}-publish-result.png"
            )
            manifest_path = self.article(article_id).folder / "发布内容.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "published"
            manifest["published_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.record_stage(article_id, "发布完成", ok=True)
            return {
                "ok": True,
                "id": article_id,
                "account_name": profile["account_name"],
                "title": compose["title"],
                "privacy": privacy,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "preflight_screenshot": str(preflight),
                "result_screenshot": str(post_screenshot),
            }
        finally:
            self.end()

    def prepare(self) -> dict:
        self.require_device()
        # Xiaohongshu's custom gallery requires the full image permission.
        self.shell("pm", "grant", PACKAGE, "android.permission.READ_MEDIA_IMAGES", check=False)
        self.shell("appops", "set", PACKAGE, "READ_MEDIA_IMAGES", "allow", check=False)
        helper_apk = Path(__file__).resolve().parent.parent / "assets" / "xhs-input-helper.apk"
        if not self.helper_installed():
            if not helper_apk.is_file():
                raise OperatorError(f"缺少中文输入助手：{helper_apk}")
            self.adb("install", "-r", str(helper_apk))
        return {
            "ok": self.package_installed() and self.helper_installed(),
            "device": self.require_device(),
            "xiaohongshu_installed": self.package_installed(),
            "input_helper_installed": self.helper_installed(),
        }

    def profile_fields(self, nodes: list[UiNode] | None = None) -> dict[str, str]:
        fields = {}
        nodes = nodes or self.dump_ui()
        labels = {"名字", "小红书号", "背景图", "简介", "性别", "生日", "地区"}
        text_nodes = [node for node in nodes if node.text]
        for index, node in enumerate(text_nodes):
            if node.text not in labels:
                continue
            same_row = [
                other
                for other in text_nodes
                if other.text != node.text
                and abs(other.center[1] - node.center[1]) < 40
                and other.center[0] > node.center[0]
            ]
            if same_row:
                fields[node.text] = same_row[0].text
        return fields

    def page_summary(self, nodes: list[UiNode]) -> dict:
        texts = [node.text for node in nodes if node.text]
        if "编辑资料" in texts:
            return {"page": "编辑资料", "fields": self.profile_fields(nodes)}
        if "编辑主页" in texts and "笔记" in texts:
            account_id = next((text for text in texts if text.startswith("小红书号：")), "")
            edit_node = next(node for node in nodes if node.text == "编辑主页")
            candidates = [
                node.text
                for node in nodes
                if node.text
                and 180 < node.center[1] < edit_node.center[1] + 260
                and node.center[0] < 650
                and not node.text.startswith("小红书号")
                and node.text not in {"关注", "粉丝", "获赞与收藏"}
            ]
            return {
                "page": "账号主页",
                "account_name": candidates[0] if candidates else "",
                "account_id": account_id.replace("小红书号：", "").strip(),
            }
        return {"page": "其他页面"}

    def doctor(self) -> dict:
        device = self.require_device()
        screenshot = self.screenshot("doctor.png")
        nodes = self.dump_ui()
        screen = self.shell("wm", "size")
        model = self.shell("getprop", "ro.product.model")
        expected_model = str(self.config.get("expected_device_model", "")).strip()
        model_supported = not expected_model or model == expected_model
        expected_screen = str(self.config.get("expected_screen_size", "1080x2280"))
        screen_supported = expected_screen in screen
        app_installed = self.package_installed()
        helper_installed = self.helper_installed()
        return {
            "ok": (
                app_installed
                and helper_installed
                and screen_supported
                and model_supported
            ),
            "device": device,
            "ignored_devices": self._ignored_devices,
            "model": model,
            "expected_device_model": expected_model,
            "model_supported": model_supported,
            "screen": screen,
            "expected_screen_size": expected_screen,
            "screen_supported": screen_supported,
            "xiaohongshu_installed": app_installed,
            "input_helper_installed": helper_installed,
            "current_focus": self.current_focus(),
            "visible_text_count": sum(bool(node.text) for node in nodes),
            "page_summary": self.page_summary(nodes),
            "screenshot": str(screenshot),
        }


def print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书安卓运营助手")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="只读检查手机、小红书和当前页面")
    subparsers.add_parser("begin", help="开始任务：自动唤醒手机并打开小红书")
    subparsers.add_parser("end", help="结束任务：关闭常亮并熄屏")
    subparsers.add_parser("prepare", help="安装中文输入助手并准备图片权限")
    article_check = subparsers.add_parser("article-check", help="校验文章发布清单和图片")
    article_check.add_argument("article_id")
    article_sync = subparsers.add_parser("article-sync", help="校验并同步文章图片到手机")
    article_sync.add_argument("article_id")
    article_fill = subparsers.add_parser("article-fill", help="在当前发布页填写标题和正文")
    article_fill.add_argument("article_id")
    article_draft = subparsers.add_parser("article-draft", help="从文章文件夹自动生成并验证草稿")
    article_draft.add_argument("article_id")
    article_draft.add_argument(
        "--replace-existing",
        action="store_true",
        help="删除同标题旧草稿后重建，用于修正图片或顺序",
    )
    article_publish = subparsers.add_parser(
        "article-publish", help="发布已校验的本地草稿并记录发布时间"
    )
    article_publish.add_argument("article_id")
    article_schedule = subparsers.add_parser(
        "article-schedule", help="上传文章并交给小红书原生定时发布"
    )
    article_schedule.add_argument("article_id")
    article_new = subparsers.add_parser(
        "article-new", help="按编号和标题创建标准文章文件夹"
    )
    article_new.add_argument("article_id")
    article_new.add_argument("title")
    daily_check = subparsers.add_parser(
        "daily-check", help="批量校验次日待发布文章（默认最多 3 篇）"
    )
    daily_check.add_argument("--limit", type=int, default=3)
    daily_check.add_argument("--date", help="指定备稿日期，格式 YYYY-MM-DD；默认次日")
    daily_drafts = subparsers.add_parser(
        "daily-drafts", help="批量生成次日待发布草稿（默认最多 3 篇）"
    )
    daily_drafts.add_argument("--limit", type=int, default=3)
    daily_drafts.add_argument("--date", help="指定备稿日期，格式 YYYY-MM-DD；默认次日")
    snapshot = subparsers.add_parser("snapshot", help="保存当前手机截图")
    snapshot.add_argument("--name", default="latest.png")
    observe = subparsers.add_parser(
        "observe",
        help="UI-TARS 只读观察当前小红书页面；绝不执行模型动作",
    )
    observe.add_argument(
        "--observer-config",
        type=Path,
        default=Path(
            os.environ.get(
                "UI_TARS_CONFIG",
                Path.home() / ".config" / "codex" / "xhs-ui-tars.json",
            )
        ).expanduser(),
    )
    observe.add_argument(
        "--allow-cloud",
        action="store_true",
        help="显式允许把当前手机截图发送到非本地模型端点",
    )
    observe.add_argument(
        "--no-model",
        action="store_true",
        help="只运行本地截图、UI 树与风险检查，不调用模型",
    )
    subparsers.add_parser(
        "recovery-status",
        help="查看 UI-TARS 接管、故障记录和经验库状态",
    )
    locate = subparsers.add_parser("locate", help="按文字查找当前页面控件")
    locate.add_argument("text")
    args = parser.parse_args()

    try:
        operator = XhsOperator(args.config)
        guarded_commands = {
            "article-sync",
            "article-fill",
            "article-draft",
            "article-publish",
            "article-schedule",
            "daily-drafts",
        }
        if args.command in guarded_commands:
            from ui_tars_recovery import UiTarsRecoveryAgent

            pending = UiTarsRecoveryAgent().pending_handoffs()
            if pending:
                raise OperatorError(
                    "存在尚未处理的 UI-TARS/Codex 故障接管项，"
                    f"原流程保持停止：{pending[0]}"
                )
        if args.command == "doctor":
            print_json(operator.doctor())
        elif args.command == "begin":
            print_json(operator.begin())
        elif args.command == "end":
            print_json(operator.end())
        elif args.command == "prepare":
            print_json(operator.prepare())
        elif args.command == "article-check":
            result = operator.check_article(args.article_id)
            print_json(result)
            return 0 if result["ok"] else 1
        elif args.command == "article-sync":
            print_json(operator.sync_article(args.article_id))
        elif args.command == "article-fill":
            result = operator.fill_article(args.article_id)
            print_json(result)
            return 0 if result["ok"] else 1
        elif args.command == "article-draft":
            print_json(
                operator.create_draft(
                    args.article_id,
                    replace_existing=args.replace_existing,
                )
            )
        elif args.command == "article-publish":
            print_json(operator.publish_article(args.article_id))
        elif args.command == "article-schedule":
            article = operator.article(args.article_id)
            if (
                article.manifest.get("status") != "scheduled"
                and article.manifest.get("phone_draft_verified_at")
                and article.manifest.get("phone_draft_signature")
            ):
                print_json(operator.schedule_existing_draft(args.article_id))
            else:
                print_json(
                    operator.create_draft(
                        args.article_id,
                        replace_existing=True,
                        schedule_at=str(article.manifest.get("scheduled_at", "")),
                    )
                )
        elif args.command == "article-new":
            print_json(operator.scaffold_article(args.article_id, args.title))
        elif args.command == "daily-check":
            result = operator.daily_check(args.limit, args.date)
            print_json(result)
            return 0 if result["ok"] else 1
        elif args.command == "daily-drafts":
            print_json(operator.daily_drafts(args.limit, args.date))
        elif args.command == "snapshot":
            print_json({"screenshot": str(operator.screenshot(args.name))})
        elif args.command == "observe":
            from ui_tars_observer import run_observation_session

            result = run_observation_session(
                operator,
                config_path=args.observer_config,
                allow_cloud=args.allow_cloud,
                invoke_model=not args.no_model,
                wake=True,
            )
            print_json(result)
            return 0 if result["ok"] else 1
        elif args.command == "recovery-status":
            from ui_tars_recovery import UiTarsRecoveryAgent

            print_json(UiTarsRecoveryAgent().status())
        elif args.command == "locate":
            print_json(
                [
                    {
                        "text": node.text,
                        "resource_id": node.resource_id,
                        "bounds": node.bounds,
                        "center": node.center,
                    }
                    for node in operator.find_nodes(text=args.text)
                ]
            )
        return 0
    except (OperatorError, OSError, json.JSONDecodeError, ET.ParseError) as error:
        print_json({"ok": False, "error": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
