"""Reference / mock e-ink display client.

This is a clean, hardware-agnostic reference implementation of the device-side
logic. All hardware-specific behaviour (battery, display, wake timer, deep
sleep, button) is isolated behind :class:`HardwareInterface` so it can be
replaced with a real ESP32-S3 / Arduino / MicroPython implementation later,
while the business logic in :class:`TrmnlClient` stays unchanged.

The mock implementation runs on any PC and uses only the standard library
(Pillow is optional and only used to save a viewable copy of the display).

Usage:
    python client.py                 # single wake cycle (timer)
    python client.py --wake-reason button
    python client.py --loop          # repeat; press Enter to simulate a wake
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s client: %(message)s"
)
logger = logging.getLogger("client")

FIRMWARE_VERSION = "0.1.0"

# Fallback intervals (seconds) used only when the server cannot be reached.
FALLBACK_UNREACHABLE_SECONDS = 30 * 60
FALLBACK_LOW_BATTERY_SECONDS = 6 * 3600
CRITICAL_BATTERY_PERCENT = 5.0


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class ClientConfig:
    device_id: str
    device_token: str
    backend_url: str
    state_file: Path

    @classmethod
    def from_env(cls) -> "ClientConfig":
        backend = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
        return cls(
            device_id=os.environ.get("DEVICE_ID", "m5paper-color-01"),
            device_token=os.environ.get("DEVICE_TOKEN", "change-me-device-token"),
            backend_url=backend,
            state_file=Path(os.environ.get("CLIENT_STATE_FILE", ".client_state.json")),
        )


# --------------------------------------------------------------------------- #
# Hardware abstraction layer
# --------------------------------------------------------------------------- #
class HardwareInterface(ABC):
    """Interface every hardware backend (mock or real) must implement."""

    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def get_wake_reason(self) -> str:
        """One of: timer, button, manual, unknown."""

    @abstractmethod
    def read_battery(self) -> tuple[Optional[float], Optional[int]]:
        """Return (percent, millivolts)."""

    @abstractmethod
    def read_wifi_rssi(self) -> Optional[int]: ...

    @abstractmethod
    def display_image(self, png_bytes: bytes) -> None:
        """Render a 400x600 PNG on the e-ink panel."""

    @abstractmethod
    def display_blank(self) -> None:
        """Clear the panel to a blank frame."""

    @abstractmethod
    def set_wake_timer(self, seconds: int) -> None:
        """Configure the deep-sleep wake timer."""

    @abstractmethod
    def deep_sleep(self) -> None:
        """Enter deep sleep. On a real device this never returns."""


class MockHardware(HardwareInterface):
    """PC-friendly mock implementation."""

    def __init__(
        self,
        wake_reason: str = "timer",
        battery_percent: Optional[float] = None,
        out_dir: Path = Path("mock_display"),
        real_sleep: bool = False,
        sleep_cap_seconds: int = 5,
    ) -> None:
        self._wake_reason = wake_reason
        self._battery_percent = (
            battery_percent if battery_percent is not None else random.uniform(40, 95)
        )
        self._out_dir = out_dir
        self._real_sleep = real_sleep
        self._sleep_cap = sleep_cap_seconds
        self._next_wake = 0

    def init(self) -> None:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[hw] initialized mock hardware")

    def set_wake_reason(self, reason: str) -> None:
        self._wake_reason = reason

    def get_wake_reason(self) -> str:
        return self._wake_reason

    def read_battery(self) -> tuple[Optional[float], Optional[int]]:
        percent = round(self._battery_percent, 1)
        # Rough Li-ion mapping for the mock.
        mv = int(3300 + (4200 - 3300) * (percent / 100.0))
        return percent, mv

    def read_wifi_rssi(self) -> Optional[int]:
        return random.randint(-75, -45)

    def display_image(self, png_bytes: bytes) -> None:
        path = self._out_dir / "current.png"
        path.write_bytes(png_bytes)
        logger.info("[hw] displayed image (%d bytes) -> %s", len(png_bytes), path)

    def display_blank(self) -> None:
        path = self._out_dir / "current.png"
        try:
            from PIL import Image

            Image.new("RGB", (400, 600), (255, 255, 255)).save(path)
        except Exception:
            path.write_bytes(b"")
        logger.info("[hw] displayed blank frame -> %s", path)

    def set_wake_timer(self, seconds: int) -> None:
        self._next_wake = seconds
        logger.info("[hw] wake timer set to %d seconds (%.1f min)", seconds, seconds / 60)

    def deep_sleep(self) -> None:
        logger.info("[hw] entering deep sleep")
        if self._real_sleep:
            time.sleep(min(self._next_wake, self._sleep_cap))


# --------------------------------------------------------------------------- #
# Persistent (RTC-like) client state
# --------------------------------------------------------------------------- #
@dataclass
class ClientState:
    last_image_id: Optional[str] = None
    pending_error: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "ClientState":
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return cls(**data)
            except Exception:
                pass
        return cls()

    def save(self, path: Path) -> None:
        try:
            path.write_text(json.dumps(self.__dict__))
        except Exception as exc:
            logger.warning("could not persist client state: %s", exc)


# --------------------------------------------------------------------------- #
# Client logic
# --------------------------------------------------------------------------- #
class TrmnlClient:
    def __init__(self, hardware: HardwareInterface, config: ClientConfig) -> None:
        self.hw = hardware
        self.config = config
        self.state = ClientState.load(config.state_file)

    def run_once(self) -> int:
        """Perform one wake cycle. Returns the next wake interval in seconds."""
        self.hw.init()
        wake_reason = self.hw.get_wake_reason()
        percent, mv = self.hw.read_battery()
        logger.info("wake_reason=%s battery=%s%% (%smV)", wake_reason, percent, mv)

        # Critical battery: skip rendering, sleep long, report next time.
        if percent is not None and percent <= CRITICAL_BATTERY_PERCENT:
            logger.warning("battery critically low; skipping update")
            self.state.pending_error = "critical_battery"
            self.state.save(self.config.state_file)
            self.hw.set_wake_timer(FALLBACK_LOW_BATTERY_SECONDS)
            self.hw.deep_sleep()
            return FALLBACK_LOW_BATTERY_SECONDS

        response = self._post_status(percent, mv, wake_reason)
        if response is None:
            logger.warning("backend unreachable; using fallback interval")
            self.hw.set_wake_timer(FALLBACK_UNREACHABLE_SECONDS)
            self.hw.deep_sleep()
            return FALLBACK_UNREACHABLE_SECONDS

        next_wake = int(response.get("next_wake_seconds") or FALLBACK_UNREACHABLE_SECONDS)
        action = response.get("action", "noop")
        self._handle_action(action, response, next_wake)

        self.hw.set_wake_timer(next_wake)
        self.hw.deep_sleep()
        return next_wake

    def _handle_action(self, action: str, response: dict, next_wake: int) -> None:
        if action == "draw":
            self._draw_image(response)
        elif action == "blank":
            if response.get("image_url"):
                self._draw_image(response)
            else:
                self.hw.display_blank()
                self.state.last_image_id = None
                self.state.save(self.config.state_file)
        elif action in ("sleep", "noop"):
            logger.info("action=%s: keeping current display", action)
        else:
            logger.warning("unknown action '%s'; treating as noop", action)
        msg = response.get("message")
        if msg:
            logger.info("server message: %s", msg)

    def _draw_image(self, response: dict) -> None:
        image_id = response.get("image_id")
        image_url = response.get("image_url")
        if not image_url:
            logger.warning("draw requested without image_url; keeping display")
            return
        data = self._download_image(image_url)
        if data is None:
            # Keep current content; report the failure on the next status post.
            self.state.pending_error = f"image_download_failed:{image_id}"
            self.state.save(self.config.state_file)
            return
        self.hw.display_image(data)
        self.state.last_image_id = image_id
        self.state.pending_error = None
        self.state.save(self.config.state_file)

    # -- HTTP helpers ------------------------------------------------------ #
    def _post_status(
        self, percent: Optional[float], mv: Optional[int], wake_reason: str
    ) -> Optional[dict]:
        url = f"{self.config.backend_url}/api/device/{self.config.device_id}/status"
        body = {
            "battery_percent": percent,
            "battery_mv": mv,
            "wake_reason": wake_reason,
            "last_image_id": self.state.last_image_id,
            "firmware_version": FIRMWARE_VERSION,
            "wifi_rssi": self.hw.read_wifi_rssi(),
        }
        if self.state.pending_error:
            body["last_error"] = self.state.pending_error
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.device_token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                logger.info("status response: %s", data)
                return data
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            logger.error("status POST failed: %s", exc)
            return None

    def _download_image(self, image_url: str) -> Optional[bytes]:
        url = image_url
        if image_url.startswith("/"):
            url = f"{self.config.backend_url}{image_url}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.config.device_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.error("image download failed: %s", exc)
            return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Mock TRMNL e-ink client")
    parser.add_argument(
        "--wake-reason",
        choices=["timer", "button", "manual", "unknown"],
        default="timer",
    )
    parser.add_argument("--battery", type=float, default=None, help="Battery percent")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Repeat wake cycles; press Enter to simulate a wake (Ctrl+C to exit)",
    )
    parser.add_argument(
        "--real-sleep",
        action="store_true",
        help="Actually sleep between cycles (capped) instead of returning immediately",
    )
    args = parser.parse_args()

    config = ClientConfig.from_env()
    hardware = MockHardware(
        wake_reason=args.wake_reason,
        battery_percent=args.battery,
        real_sleep=args.real_sleep,
    )
    client = TrmnlClient(hardware, config)

    if not args.loop:
        client.run_once()
        return

    logger.info("Loop mode. Press Enter to simulate a button/timer wake (Ctrl+C exits).")
    try:
        while True:
            line = input("[press Enter to wake, 'b'+Enter for button] ").strip().lower()
            hardware.set_wake_reason("button" if line == "b" else "timer")
            client.run_once()
    except (KeyboardInterrupt, EOFError):
        logger.info("exiting loop")


if __name__ == "__main__":
    main()
