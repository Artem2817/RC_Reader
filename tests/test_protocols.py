import unittest

from rc_reader.protocols import (
    CRSFParser,
    IBUSParser,
    SBUSParser,
    SUMDParser,
    SpektrumParser,
    crc8_dvb_s2,
    crc16_ccitt,
    raw_to_microseconds,
)
from rc_reader.serial_worker import parse_uart_format


def pack_channels(channels: list[int]) -> bytes:
    packed = sum((value & 0x7FF) << (index * 11) for index, value in enumerate(channels))
    return packed.to_bytes(22, byteorder="little")


def make_crsf(channels: list[int]) -> bytes:
    body = bytes([0x16]) + pack_channels(channels)
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8_dvb_s2(body)])


def make_sbus(channels: list[int], flags: int = 0) -> bytes:
    return bytes([0x0F]) + pack_channels(channels) + bytes([flags, 0x00])


def make_ibus(channels: list[int]) -> bytes:
    body = bytes([0x20, 0x40]) + b"".join(
        value.to_bytes(2, "little") for value in channels
    )
    checksum = (0xFFFF - sum(body)) & 0xFFFF
    return body + checksum.to_bytes(2, "little")


def make_sumd(channels: list[int], state: int = 0x01) -> bytes:
    body = bytes([0xA8, state, len(channels)]) + b"".join(
        value.to_bytes(2, "big") for value in channels
    )
    return body + crc16_ccitt(body).to_bytes(2, "big")


def make_spektrum(channel_values: list[tuple[int, int]], resolution: int) -> bytes:
    words = []
    for channel, value in channel_values:
        shift = 10 if resolution == 1024 else 11
        words.append(((channel << shift) | value).to_bytes(2, "big"))
    return b"\x00\xB2" + b"".join(words)


class CRSFParserTests(unittest.TestCase):
    def test_decodes_split_frame_after_noise(self) -> None:
        channels = [172, 992, 1811] + [500 + index * 20 for index in range(13)]
        encoded = make_crsf(channels)
        parser = CRSFParser()

        self.assertEqual(parser.feed(b"\xFF\x55" + encoded[:8]), [])
        frames = parser.feed(encoded[8:])

        self.assertEqual(len(frames), 1)
        self.assertEqual(list(frames[0].channels), channels)
        self.assertEqual(parser.stats.channel_frames, 1)
        self.assertEqual(parser.stats.bytes_dropped, 2)

    def test_rejects_bad_crc_and_recovers(self) -> None:
        channels = [992] * 16
        damaged = bytearray(make_crsf(channels))
        damaged[-1] ^= 0x01
        parser = CRSFParser()

        frames = parser.feed(bytes(damaged) + make_crsf(channels))

        self.assertEqual(len(frames), 1)
        self.assertGreaterEqual(parser.stats.invalid_frames, 1)


class SBUSParserTests(unittest.TestCase):
    def test_decodes_flags_and_channels(self) -> None:
        channels = [172, 992, 1811] + [1000] * 13
        parser = SBUSParser()

        frames = parser.feed(b"\xAA\xBB" + make_sbus(channels, flags=0x0D))

        self.assertEqual(len(frames), 1)
        self.assertEqual(list(frames[0].channels), channels)
        self.assertTrue(frames[0].digital_channel_17)
        self.assertTrue(frames[0].frame_lost)
        self.assertTrue(frames[0].failsafe)

    def test_handles_multiple_and_partial_frames(self) -> None:
        channels = [i * 100 for i in range(16)]
        encoded = make_sbus(channels)
        parser = SBUSParser()

        self.assertEqual(parser.feed(encoded[:20]), [])
        frames = parser.feed(encoded[20:] + encoded)

        self.assertEqual(len(frames), 2)
        self.assertEqual(list(frames[-1].channels), channels)


class IBUSParserTests(unittest.TestCase):
    def test_decodes_channels_and_checksum(self) -> None:
        channels = [1000 + index * 50 for index in range(14)]
        encoded = make_ibus(channels)
        parser = IBUSParser()

        self.assertEqual(parser.feed(b"\x99" + encoded[:11]), [])
        frames = parser.feed(encoded[11:])

        self.assertEqual(len(frames), 1)
        self.assertEqual(list(frames[0].channels), channels)
        self.assertEqual(list(frames[0].microseconds), channels)

    def test_rejects_bad_checksum(self) -> None:
        encoded = bytearray(make_ibus([1500] * 14))
        encoded[-1] ^= 1
        parser = IBUSParser()

        self.assertEqual(parser.feed(encoded), [])
        self.assertEqual(parser.stats.invalid_frames, 1)


class SUMDParserTests(unittest.TestCase):
    def test_decodes_32_channels_and_failsafe(self) -> None:
        channels = [8000 + index * 250 for index in range(32)]
        encoded = make_sumd(channels, state=0x81)
        parser = SUMDParser()

        frames = parser.feed(encoded[:30])
        self.assertEqual(frames, [])
        frames = parser.feed(encoded[30:])

        self.assertEqual(len(frames), 1)
        self.assertEqual(list(frames[0].channels), channels)
        self.assertTrue(frames[0].failsafe)
        self.assertEqual(frames[0].microseconds[0], 1000)


class SpektrumParserTests(unittest.TestCase):
    def test_decodes_dsm2_1024_after_noise(self) -> None:
        values = [(index, 300 + index * 50) for index in range(7)]
        parser = SpektrumParser(1024)

        frames = parser.feed(b"\xFF\xFE" + make_spektrum(values, 1024))

        self.assertEqual(len(frames), 1)
        self.assertEqual(list(frames[0].channels), [value for _channel, value in values])
        self.assertEqual(frames[0].microseconds[0], 1288)

    def test_dsmx_accumulates_channels_across_packets(self) -> None:
        parser = SpektrumParser(2048)
        first = [(index, 600 + index * 20) for index in range(7)]
        second = [(index, 800 + index * 20) for index in range(5, 12)]

        frames = parser.feed(
            make_spektrum(first, 2048) + make_spektrum(second, 2048)
        )

        self.assertEqual(len(frames), 2)
        self.assertEqual(len(frames[-1].channels), 12)
        self.assertEqual(frames[-1].channels[11], 1020)


class ConversionTests(unittest.TestCase):
    def test_common_endpoints(self) -> None:
        self.assertEqual(raw_to_microseconds(172), 988)
        self.assertEqual(raw_to_microseconds(1811), 2012)

    def test_uart_formats(self) -> None:
        self.assertEqual(parse_uart_format("8n1"), (8, "N", 1))
        self.assertEqual(parse_uart_format(" 8E2 "), (8, "E", 2))
        with self.assertRaises(ValueError):
            parse_uart_format("8X3")


if __name__ == "__main__":
    unittest.main()
