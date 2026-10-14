import json
import tempfile
import unittest
from pathlib import Path

from rc_reader.protocols import (
    CRSFParser,
    ChannelFrame,
    FPortParser,
    GHSTParser,
    MAVLinkParser,
    crc8_dvb_s2,
    crc16_x25,
    frsky_checksum_is_good,
)
from rc_reader.profiles import PROTOCOL_SPECS, SCAN_BAUDRATES
from rc_reader.serial_worker import UART_FORMATS, ScanWorker, create_parser
from rc_reader.session import export_csv, frame_from_dict, frame_to_dict


def pack_bits(values: list[int], bits: int) -> bytes:
    packed = sum(
        (value & ((1 << bits) - 1)) << (index * bits)
        for index, value in enumerate(values)
    )
    return packed.to_bytes((len(values) * bits + 7) // 8, "little")


def make_crsf_frame(frame_type: int, payload: bytes) -> bytes:
    body = bytes([frame_type]) + payload
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8_dvb_s2(body)])


def make_fport(channels: list[int], flags: int = 0, rssi: int = 77) -> bytes:
    candidate = bytearray([25, 0])
    candidate.extend(pack_bits(channels, 11))
    candidate.extend((flags, rssi))
    checksum = 0
    for byte in candidate:
        checksum += byte
        checksum += checksum >> 8
        checksum &= 0xFF
    candidate.append(0xFF - checksum)
    assert frsky_checksum_is_good(candidate)

    escaped = bytearray([0x7E])
    for byte in candidate:
        if byte in (0x7D, 0x7E):
            escaped.extend((0x7D, byte ^ 0x20))
        else:
            escaped.append(byte)
    escaped.append(0x7E)
    return bytes(escaped)


def make_ghst(frame_type: int, primary: list[int], aux: list[int]) -> bytes:
    payload = pack_bits(primary, 12) + bytes(aux)
    body = bytes([frame_type]) + payload
    return bytes([0x82, len(body) + 1]) + body + bytes([crc8_dvb_s2(body)])


def make_mavlink_v1(message_id: int, payload: bytes, crc_extra: int) -> bytes:
    header_without_magic = bytes([len(payload), 7, 1, 2, message_id])
    checksum = crc16_x25(header_without_magic + payload + bytes([crc_extra]))
    return (
        bytes([0xFE])
        + header_without_magic
        + payload
        + checksum.to_bytes(2, "little")
    )


class CRSFAdvancedTests(unittest.TestCase):
    def test_subset_channels_and_link_metadata(self) -> None:
        parser = CRSFParser()
        link_stats = bytes([52, 54, 96, 7, 1, 2, 3, 62, 88, 0xFC])
        parser.feed(make_crsf_frame(0x14, link_stats))

        values = [0, 512, 1023]
        subset_payload = bytes([0x00]) + pack_bits(values, 10)
        frames = parser.feed(make_crsf_frame(0x17, subset_payload))

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].channels[:3], tuple(values))
        self.assertEqual(frames[0].microseconds[:3], (988, 1501, 2012))
        self.assertIn(("Link Quality", "96 %"), frames[0].metadata)
        self.assertIn("10-bit", frames[0].frame_name)


class FPortParserTests(unittest.TestCase):
    def test_unescapes_and_decodes_control_frame(self) -> None:
        channels = [172, 992, 1811] + [1000] * 13
        encoded = make_fport(channels, flags=0x0D, rssi=81)
        parser = FPortParser()

        self.assertEqual(parser.feed(b"\x00\x55" + encoded[:10]), [])
        frames = parser.feed(encoded[10:])

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].channels, tuple(channels))
        self.assertTrue(frames[0].failsafe)
        self.assertTrue(frames[0].frame_lost)
        self.assertEqual(frames[0].metadata, (("RSSI", "81 %"),))


class GHSTParserTests(unittest.TestCase):
    def test_decodes_primary_and_aux_channels(self) -> None:
        primary = [0, 1024, 2048, 4095]
        parser = GHSTParser()
        frames = parser.feed(
            b"\x01\x02" + make_ghst(0x30, primary, [0, 64, 128, 255])
        )

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].channels[:4], tuple(primary))
        self.assertEqual(frames[0].channels[4:8], (0, 256, 512, 1020))
        self.assertEqual(frames[0].microseconds[0], 988)
        self.assertEqual(frames[0].microseconds[3], 2012)


class MAVLinkParserTests(unittest.TestCase):
    def test_decodes_rc_channels_v1(self) -> None:
        channels = [1000 + index * 25 for index in range(18)]
        payload = (
            (123456).to_bytes(4, "little")
            + b"".join(value.to_bytes(2, "little") for value in channels)
            + bytes([18, 73])
        )
        encoded = make_mavlink_v1(65, payload, 118)
        parser = MAVLinkParser()

        self.assertEqual(parser.feed(encoded[:9]), [])
        frames = parser.feed(encoded[9:])

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].channels, tuple(channels))
        self.assertIn(("System ID", "1"), frames[0].metadata)
        self.assertIn(("RSSI", "73"), frames[0].metadata)

    def test_rejects_bad_crc(self) -> None:
        payload = bytes(42)
        encoded = bytearray(make_mavlink_v1(65, payload, 118))
        encoded[-1] ^= 0x80
        parser = MAVLinkParser()

        self.assertEqual(parser.feed(encoded), [])
        self.assertEqual(parser.stats.invalid_frames, 1)

    def test_decodes_rc_channels_scaled(self) -> None:
        scaled = [-10000, -5000, 0, 5000, 10000, 32767, 2500, -2500]
        payload = (
            (987654).to_bytes(4, "little")
            + b"".join(value.to_bytes(2, "little", signed=True) for value in scaled)
            + bytes([1, 64])
        )
        encoded = make_mavlink_v1(34, payload, 237)
        frames = MAVLinkParser().feed(encoded)

        self.assertEqual(len(frames), 1)
        self.assertEqual(
            frames[0].microseconds,
            (1000, 1250, 1500, 1750, 2000, 1500, 1625, 1375),
        )
        self.assertEqual(frames[0].percent_override[:5], (0.0, 25.0, 50.0, 75.0, 100.0))
        self.assertIn(("RC port", "1"), frames[0].metadata)
        self.assertIn(("Inactive channels", "CH6"), frames[0].metadata)


class SessionTests(unittest.TestCase):
    def test_json_round_trip_and_csv_export(self) -> None:
        frame = ChannelFrame(
            protocol="TEST",
            channels=(172, 992),
            raw_frame=b"\x01\x02",
            microseconds_override=(988, 1500),
            percent_override=(0.0, 50.0),
            metadata=(("RSSI", "90 %"),),
            frame_name="test frame",
        )
        restored = frame_from_dict(
            json.loads(json.dumps(frame_to_dict(frame, 1.25)))
        )
        self.assertEqual(restored, frame)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "channels.csv"
            export_csv(target, [(1.25, frame)])
            contents = target.read_text(encoding="utf-8-sig")
            self.assertIn("ch1_raw,ch1_us", contents)
            self.assertIn("1.250000,TEST,test frame,0,0,172,988,992,1500", contents)

    def test_parser_factory_covers_every_ui_protocol(self) -> None:
        for name in (
            "CRSF",
            "GHST",
            "SBUS",
            "F.Port",
            "iBUS",
            "SUMD",
            "DSM2 1024",
            "DSMX 2048",
            "MAVLink",
        ):
            with self.subTest(protocol=name):
                self.assertIsNotNone(create_parser(name))


class ExhaustiveScanTests(unittest.TestCase):
    def test_covers_every_baud_and_uart_format(self) -> None:
        scanner = ScanWorker(
            "COM_TEST",
            events=None,  # type: ignore[arg-type]
            preferred_baudrate=333333,
            preferred_uart_format="8O2",
        )
        attempts = scanner._build_attempts()

        self.assertEqual(attempts[0], (333333, "8O2"))
        for baudrate in (333333, *SCAN_BAUDRATES):
            for uart_format in UART_FORMATS:
                self.assertIn((baudrate, uart_format), attempts)

    def test_detects_sbus_on_nonstandard_serial_settings(self) -> None:
        channels = [172, 992, 1811] + [1000] * 13
        sbus_frame = bytes([0x0F]) + pack_bits(channels, 11) + bytes([0, 0])
        parsers = {
            spec.name: create_parser(spec.name)
            for spec in PROTOCOL_SPECS
        }
        for _ in range(3):
            for parser in parsers.values():
                parser.feed(sbus_frame)

        # The scanner runs this same parser bank while the port is configured
        # to each candidate combination, including 115200 8N1.
        self.assertEqual(
            ScanWorker._find_confident_protocol(parsers),
            "SBUS",
        )


if __name__ == "__main__":
    unittest.main()
