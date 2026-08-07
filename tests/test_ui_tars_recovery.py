from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ui_tars_recovery import (  # noqa: E402
    ExperienceStore,
    RecoveryPolicyError,
    UiTarsRecoveryAgent,
    parse_model_action,
)
from xhs_operator import parse_adb_devices, select_target_device  # noqa: E402


class FakeNode:
    def __init__(
        self,
        *,
        text: str = "",
        resource_id: str = "",
        bounds: tuple[int, int, int, int] = (0, 0, 100, 100),
    ):
        self.text = text
        self.content_desc = ""
        self.resource_id = resource_id
        self.bounds = bounds
        self.clickable = True


class UiTarsRecoveryTests(unittest.TestCase):
    def test_experience_promotes_after_two_successes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ExperienceStore(Path(temp))
            first = store.save_candidate(
                failure_kind="missing_text",
                target="高级选项",
                fingerprint="page",
                error_signature="error",
                actions=[{"action_type": "back"}],
                evidence={"run": 1},
            )
            second = store.save_candidate(
                failure_kind="missing_text",
                target="高级选项",
                fingerprint="page",
                error_signature="error",
                actions=[{"action_type": "back"}],
                evidence={"run": 2},
            )
            self.assertEqual(first.parent.name, "candidates")
            self.assertEqual(second.parent.name, "approved")

    def test_policy_blocks_publish_action(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "enabled": False,
                        "recovery_enabled": True,
                        "experience_root": str(Path(temp) / "experience"),
                    }
                ),
                encoding="utf-8",
            )
            agent = UiTarsRecoveryAgent(config)
            with self.assertRaises(RecoveryPolicyError):
                agent._validate_action(
                    {"action_type": "tap_text", "text": "发布"},
                    [FakeNode(text="发布")],
                )

    def test_native_ui_tars_click_is_normalized(self):
        action = parse_model_action(
            "Action: click(start_box='(546,1148)')",
            screenshot_width=1080,
            screenshot_height=2280,
        )
        self.assertEqual(action["action_type"], "tap_point")
        self.assertAlmostEqual(action["point"][0], 500, delta=3)

    def test_usb_oneplus_is_selected_and_network_device_ignored(self):
        devices = parse_adb_devices(
            "List of devices attached\n"
            "USB123 device usb:1-1 product:x model:ONEPLUS_A6003 transport_id:1\n"
            "192.168.1.9:5555 device product:y model:OTHER transport_id:2\n"
        )
        selected, ignored = select_target_device(
            devices,
            expected_model="ONEPLUS A6003",
            expected_transport="usb",
        )
        self.assertEqual(selected, "USB123")
        self.assertEqual(ignored, ["192.168.1.9:5555"])


if __name__ == "__main__":
    unittest.main()
