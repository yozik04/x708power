#!/usr/bin/env python3
### Automatic Safe Shutdown

import asyncio
import logging
import signal
import struct
import subprocess
import sys
import typing
from contextlib import AbstractContextManager, ExitStack, suppress
from datetime import datetime
from enum import Enum

import RPi.GPIO as GPIO
import smbus

POWER_LOST_GPIO_IN = 6
BOOT_GPIO_OUT = 12
X708_POWER_OFF_GPIO_OUT = 13
SHUTDOWN_BUTTON_PRESS_GPIO_IN = 5

MIN_VOLTAGE = 3.2
MIN_CAPACITY = 15

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

logger = logging.getLogger("x708power")


class PressAction(Enum):
    SHORT_PRESS = 1
    LONG_PRESS = 2


class PowerLostMonitor(AbstractContextManager):
    def __init__(self, gpio_pin: int, on_state_change: typing.Callable[[bool], None]):
        self._gpio_pin = gpio_pin
        self._on_state_change = on_state_change

    def __enter__(self):
        GPIO.setup(self._gpio_pin, GPIO.IN)
        GPIO.add_event_detect(self._gpio_pin, GPIO.BOTH, callback=self._read_power_lost)

        self._read_power_lost(self._gpio_pin)
        return self

    def __exit__(self, *exc):
        GPIO.remove_event_detect(self._gpio_pin)
        return False

    def _read_power_lost(self, channel: int):
        is_power_lost = GPIO.input(channel)

        logger.warning("Power " + ("lost" if is_power_lost else "OK"))
        self._on_state_change(is_power_lost)


class PowerButtonPressMonitor(AbstractContextManager):
    def __init__(self, gpio_pin: int, on_action: typing.Callable[[PressAction], None]):
        self._loop = asyncio.get_event_loop()
        self._on_action = on_action
        self._release_future = None

        self._gpio_pin = gpio_pin

    def __enter__(self):
        GPIO.setup(self._gpio_pin, GPIO.IN)
        GPIO.add_event_detect(
            self._gpio_pin, GPIO.BOTH, callback=self._on_button_toggle
        )
        return self

    def __exit__(self, *exc):
        GPIO.remove_event_detect(self._gpio_pin)
        return False

    def _on_button_toggle(self, channel):
        if GPIO.input(channel):
            self._loop.call_soon_threadsafe(
                lambda: self._loop.create_task(self._on_press())
            )
        else:
            self._loop.call_soon_threadsafe(
                lambda: self._loop.create_task(self._on_release())
            )

    async def _on_press(self):
        self._release_future = asyncio.Future()
        try:
            await asyncio.wait_for(self._release_future, 2)
            self._on_action(PressAction.SHORT_PRESS)

        except asyncio.TimeoutError:
            self._on_action(PressAction.LONG_PRESS)

    async def _on_release(self):
        if self._release_future and not self._release_future.done():
            self._release_future.set_result(False)


class BatteryLevelMonitor:
    def __init__(self, smbus: smbus.SMBus, device_address: int):
        self._bus = smbus
        self._device_address = device_address
        self.capacity = None
        self.voltage = None

    def read_voltage(self) -> None:
        read = self._bus.read_word_data(self._device_address, 2)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        self.voltage = swapped * 1.25 / 1000 / 16

    def read_capacity(self) -> None:
        read = self._bus.read_word_data(self._device_address, 4)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        self.capacity = swapped / 256

    def read_metrics(self) -> None:
        self.read_voltage()
        self.read_capacity()


class GPIOContextManager(AbstractContextManager):
    def __enter__(self):
        GPIO.setmode(GPIO.BCM)

        GPIO.setup(BOOT_GPIO_OUT, GPIO.OUT)
        GPIO.output(BOOT_GPIO_OUT, 1)
        return self

    def __exit__(self, *exc):
        GPIO.cleanup()
        return False


class PowerController:
    def __init__(self, battery: BatteryLevelMonitor):
        self.is_power_lost = False
        self.in_power_mode_since = datetime.now()
        self.battery = battery
        self._previous_battery_capacity = -1

        self._terminate_event = asyncio.Future()

        signal.signal(signal.SIGINT, lambda signum, frame: self.stop())
        signal.signal(signal.SIGTERM, lambda signum, frame: self.stop())

    def on_power_loss_change(self, is_lost: bool) -> None:
        self.is_power_lost = is_lost
        self.in_power_mode_since = datetime.now()

    def on_power_button_press(self, action: PressAction):
        if not self._terminate_event.done():
            if action == PressAction.SHORT_PRESS:
                self.reboot("short button press")
                return

            elif action == PressAction.LONG_PRESS:
                self.shutdown("long button press")
                return

    @property
    def is_low_battery(self):
        low_battery = False

        if self.battery.capacity < MIN_CAPACITY:
            logger.warning("Battery capacity is under %d%%" % MIN_CAPACITY)
            low_battery = True

        if self.battery.voltage < MIN_VOLTAGE:
            logger.warning("Battery voltage is under %.1fV" % MIN_VOLTAGE)
            low_battery = True

        return low_battery

    def log_status(self):
        if abs(self.battery.capacity - self._previous_battery_capacity) >= 1:
            self._previous_battery_capacity = self.battery.capacity
            logger.info(
                f"Battery: %d%% %.1fV" % (self.battery.capacity, self.battery.voltage)
            )

    def log_power_lost_duration(self):
        seconds = int((datetime.now() - self.in_power_mode_since).total_seconds())
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)

        locals_ = locals()
        magnitudes_str = (
            "{n} {magnitude}".format(n=int(locals_[magnitude]), magnitude=magnitude)
            for magnitude in ("days", "hours", "minutes", "seconds")
            if locals_[magnitude]
        )
        logger.info(
            "Shutdown initiated after " + (", ".join(magnitudes_str)) + " on battery"
        )

    async def loop(self):
        while not self._terminate_event.done():
            self.battery.read_metrics()
            self.log_status()
            if self.is_power_lost and self.is_low_battery:
                logger.info("Initiating shutdown")
                self.log_power_lost_duration()

                await self.initiate_shutdown()

            with suppress(asyncio.TimeoutError):
                return await asyncio.wait_for(
                    asyncio.shield(self._terminate_event), timeout=5
                )  # Will return when shutdown_event

    @staticmethod
    async def initiate_shutdown():
        # Programmatically holding power button to initiate shut down
        GPIO.setup(X708_POWER_OFF_GPIO_OUT, GPIO.OUT)
        GPIO.output(X708_POWER_OFF_GPIO_OUT, 1)
        await asyncio.sleep(3)
        GPIO.output(X708_POWER_OFF_GPIO_OUT, 0)

    def shutdown(self, reason: str):
        logger.warning(f"Shutting down ({reason})")
        subprocess.call(["shutdown", "now"])
        self.stop()

    def reboot(self, reason: str):
        logger.info(f"Rebooting ({reason})")
        subprocess.call(["reboot"])
        self.stop()

    def stop(self):
        logger.info("Terminating")
        self._terminate_event.set_result(True)


def run():
    with ExitStack() as stack:
        bus = smbus.SMBus(1)  # 0 = /dev/i2c-0 (port I2C0), 1 = /dev/i2c-1 (port I2C1)
        battery_monitor = BatteryLevelMonitor(bus, device_address=0x36)

        controller = PowerController(battery_monitor)
        stack.enter_context(GPIOContextManager())
        stack.enter_context(
            PowerLostMonitor(POWER_LOST_GPIO_IN, controller.on_power_loss_change)
        )
        stack.enter_context(
            PowerButtonPressMonitor(
                SHUTDOWN_BUTTON_PRESS_GPIO_IN, controller.on_power_button_press
            )
        )

        event_loop = asyncio.get_event_loop()
        event_loop.run_until_complete(controller.loop())


if __name__ == "__main__":
    run()
