import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from rc_reader.core.generator import GeneratorProfile, RCGenerator, load_profile, save_profile
from rc_reader.core.ports import transmission_warning
from rc_reader.core.protocol_registry import REGISTRY, protocol_names


class ProtocolRegistryTests(unittest.TestCase):
    def test_every_transmitter_produces_a_valid_binary_frame(self) -> None:
        values = [1500] * 16
        for name in protocol_names(tx=True):
            with self.subTest(protocol=name):
                adapter = REGISTRY[name]
                frame = adapter.encode(values)
                self.assertIsInstance(frame, bytes)
                self.assertTrue(adapter.validate(frame))
                self.assertNotIn(b"1500", frame)

    def test_known_ibus_neutral_vector(self) -> None:
        frame = REGISTRY["iBUS"].encode([1500] * 14)
        expected = bytes.fromhex(
            "20 40 " + "DC 05 " * 14 + "51 F3"
        )
        self.assertEqual(frame, expected)

    def test_capabilities_are_honest(self) -> None:
        self.assertTrue(REGISTRY["CRSF"].config.tx)
        self.assertFalse(REGISTRY["F.Port"].config.tx)
        self.assertFalse(REGISTRY["DSMX 2048"].config.tx)
        self.assertTrue(REGISTRY["SBUS"].config.inverted)
        self.assertIn("hardware inverter", transmission_warning("SBUS", "CH340/CH341"))


class GeneratorProfileTests(unittest.TestCase):
    def test_profile_round_trip_and_range_normalization(self) -> None:
        profile = GeneratorProfile(
            name="bench",
            protocol="iBUS",
            baudrate=115200,
            uart_format="8N1",
            frame_rate=100,
            channels=[500, 1500, 3000],
            enabled=[True, False, True],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            save_profile(path, profile)
            restored = load_profile(path)
        self.assertEqual(restored.channels, [750, 1500, 2250])
        self.assertEqual(restored.enabled, [True, False, True])
        self.assertEqual(restored.protocol, "iBUS")

    def test_rejects_rx_only_protocol_for_generator(self) -> None:
        profile = GeneratorProfile("bad", "F.Port", 115200, "8N1", 100, [1500], [True])
        with self.assertRaises(ValueError):
            profile.normalized()

    def test_generator_opens_writes_and_closes_port(self) -> None:
        writes: list[bytes] = []
        closed: list[bool] = []

        class FakeSerial:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def write(self, data: bytes) -> None:
                writes.append(data)

            def cancel_write(self) -> None:
                pass

            def close(self) -> None:
                closed.append(True)

        events: Queue = Queue()
        generator = RCGenerator(events)
        profile = GeneratorProfile("loop", "iBUS", 115200, "8N1", 100, [1500] * 14, [True] * 14)
        with patch.dict("sys.modules", {"serial": types.SimpleNamespace(Serial=FakeSerial)}):
            generator.start("COM_TEST", profile)
            time.sleep(0.06)
            generator.stop()
            generator._thread.join(timeout=1.0)  # type: ignore[union-attr]
        self.assertGreaterEqual(len(writes), 2)
        self.assertTrue(all(frame.startswith(b"\x20\x40") for frame in writes))
        self.assertEqual(closed, [True])

    def test_generator_reports_open_error(self) -> None:
        class BrokenSerial:
            def __init__(self, **kwargs):
                raise OSError("adapter removed")

        events: Queue = Queue()
        generator = RCGenerator(events)
        profile = GeneratorProfile("fault", "iBUS", 115200, "8N1", 100, [1500] * 14, [True] * 14)
        with patch.dict("sys.modules", {"serial": types.SimpleNamespace(Serial=BrokenSerial)}):
            generator.start("COM_MISSING", profile)
            generator._thread.join(timeout=1.0)  # type: ignore[union-attr]
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait()[0])
        self.assertEqual(emitted, ["tx_error", "tx_disconnected"])


if __name__ == "__main__":
    unittest.main()
