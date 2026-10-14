from __future__ import annotations

from dataclasses import dataclass
import math


RC_MIN = 172
RC_MAX = 1811
RC_CENTER = 992


def raw_to_microseconds(value: int) -> int:
    """Convert common 11-bit RC units to approximately 988..2012 us."""
    return round(988 + (value - RC_MIN) * 1024 / (RC_MAX - RC_MIN))


@dataclass(frozen=True, slots=True)
class ChannelFrame:
    protocol: str
    channels: tuple[int, ...]
    raw_frame: bytes
    failsafe: bool = False
    frame_lost: bool = False
    digital_channel_17: bool = False
    digital_channel_18: bool = False
    channel_min: int = RC_MIN
    channel_max: int = RC_MAX
    microseconds_override: tuple[int, ...] | None = None
    percent_override: tuple[float, ...] | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    frame_name: str = ""

    @property
    def microseconds(self) -> tuple[int, ...]:
        if self.microseconds_override is not None:
            return self.microseconds_override
        return tuple(raw_to_microseconds(value) for value in self.channels)


@dataclass(frozen=True, slots=True)
class ProtocolPacket:
    protocol: str
    raw_frame: bytes
    frame_name: str
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class ParserStats:
    bytes_received: int = 0
    valid_frames: int = 0
    channel_frames: int = 0
    invalid_frames: int = 0
    bytes_dropped: int = 0

    def snapshot(self) -> "ParserStats":
        return ParserStats(
            bytes_received=self.bytes_received,
            valid_frames=self.valid_frames,
            channel_frames=self.channel_frames,
            invalid_frames=self.invalid_frames,
            bytes_dropped=self.bytes_dropped,
        )


def _unpack_16x11_lsb(data: bytes) -> tuple[int, ...]:
    if len(data) < 22:
        raise ValueError("22 bytes are required for 16 packed 11-bit channels")

    bit_buffer = int.from_bytes(data[:22], byteorder="little")
    return tuple((bit_buffer >> (channel * 11)) & 0x7FF for channel in range(16))


def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc16_ccitt(data: bytes, initial: int = 0) -> int:
    crc = initial
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def crc16_x25(data: bytes, initial: int = 0xFFFF) -> int:
    crc = initial
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp ^= (tmp << 4) & 0xFF
        crc = (
            (crc >> 8)
            ^ (tmp << 8)
            ^ (tmp << 3)
            ^ (tmp >> 4)
        ) & 0xFFFF
    return crc


def frsky_checksum_is_good(data: bytes) -> bool:
    checksum = 0
    for byte in data:
        checksum += byte
        checksum += checksum >> 8
        checksum &= 0xFF
    return checksum == 0xFF


class CRSFParser:
    """Streaming parser for CRSF frames containing RC_CHANNELS_PACKED (0x16)."""

    RC_CHANNELS_PACKED = 0x16
    SUBSET_RC_CHANNELS_PACKED = 0x17
    LINK_STATISTICS = 0x14
    LINK_STATISTICS_RX = 0x1C
    LINK_STATISTICS_TX = 0x1D
    GPS = 0x02
    VARIO = 0x07
    BATTERY_SENSOR = 0x08
    BARO_ALTITUDE = 0x09
    ATTITUDE = 0x1E
    FLIGHT_MODE = 0x21
    DEVICE_INFO = 0x29
    MAX_LENGTH = 62
    # Standard CRSF device/broadcast addresses. Restricting sync bytes makes
    # recovery from a noisy or incorrectly configured UART much faster.
    ADDRESSES = frozenset(
        {
            0x00,
            0x10,
            0x12,
            0x80,
            0xC0,
            0xC2,
            0xC4,
            0xC8,
            0xCA,
            0xCC,
            0xCE,
            0xEA,
            0xEC,
            0xEE,
        }
    )

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()
        self._metadata: dict[str, str] = {}
        self._subset_values = [1024] * 32
        self._subset_us = [1500] * 32
        self._packets: list[ProtocolPacket] = []

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()
        self._metadata.clear()
        self._subset_values = [1024] * 32
        self._subset_us = [1500] * 32
        self._packets.clear()

    def drain_packets(self) -> tuple[ProtocolPacket, ...]:
        packets = tuple(self._packets)
        self._packets.clear()
        return packets

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while len(self.buffer) >= 2:
            if self.buffer[0] not in self.ADDRESSES:
                self._drop(1)
                continue

            length = self.buffer[1]
            if length < 2 or length > self.MAX_LENGTH:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            total_length = length + 2
            if len(self.buffer) < total_length:
                break

            candidate = bytes(self.buffer[:total_length])
            if crc8_dvb_s2(candidate[2:-1]) != candidate[-1]:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            del self.buffer[:total_length]
            self.stats.valid_frames += 1
            frame_type = candidate[2]
            payload = candidate[3:-1]

            if frame_type == self.RC_CHANNELS_PACKED and len(payload) == 22:
                decoded.append(
                    ChannelFrame(
                        protocol="CRSF",
                        channels=_unpack_16x11_lsb(payload),
                        raw_frame=candidate,
                        metadata=tuple(self._metadata.items()),
                        frame_name="RC_CHANNELS_PACKED 0x16",
                    )
                )
                self.stats.channel_frames += 1
            elif frame_type == self.SUBSET_RC_CHANNELS_PACKED and len(payload) >= 3:
                frame = self._decode_subset(candidate, payload)
                if frame is not None:
                    decoded.append(frame)
                    self.stats.channel_frames += 1
            elif frame_type == self.LINK_STATISTICS and len(payload) >= 10:
                self._metadata.update(
                    {
                        "Uplink RSSI 1": f"-{payload[0]} dBm",
                        "Uplink RSSI 2": f"-{payload[1]} dBm",
                        "Link Quality": f"{payload[2]} %",
                        "Uplink SNR": f"{int.from_bytes(payload[3:4], 'big', signed=True)} dB",
                        "RF Mode": str(payload[5]),
                        "TX Power": str(payload[6]),
                        "Downlink RSSI": f"-{payload[7]} dBm",
                        "Downlink LQ": f"{payload[8]} %",
                        "Downlink SNR": f"{int.from_bytes(payload[9:10], 'big', signed=True)} dB",
                    }
                )
                self._add_packet(candidate, "LINK_STATISTICS 0x14")
            elif frame_type == self.LINK_STATISTICS_RX and len(payload) >= 5:
                self._metadata.update(
                    {
                        "RX RSSI": f"-{payload[0]} dBm",
                        "RX RSSI %": f"{payload[1]} %",
                        "RX Link Quality": f"{payload[2]} %",
                        "RX SNR": f"{int.from_bytes(payload[3:4], 'big', signed=True)} dB",
                        "Uplink RF Power": str(payload[4]),
                    }
                )
                self._add_packet(candidate, "LINK_STATISTICS_RX 0x1C")
            elif frame_type == self.LINK_STATISTICS_TX and len(payload) >= 6:
                self._metadata.update(
                    {
                        "TX RSSI": f"-{payload[0]} dBm",
                        "TX RSSI %": f"{payload[1]} %",
                        "TX Link Quality": f"{payload[2]} %",
                        "TX SNR": f"{int.from_bytes(payload[3:4], 'big', signed=True)} dB",
                        "Downlink RF Power": str(payload[4]),
                        "Uplink FPS": f"{payload[5] * 10}",
                    }
                )
                self._add_packet(candidate, "LINK_STATISTICS_TX 0x1D")
            elif frame_type == self.GPS and len(payload) >= 15:
                latitude = int.from_bytes(payload[0:4], "big", signed=True) / 1e7
                longitude = int.from_bytes(payload[4:8], "big", signed=True) / 1e7
                self._metadata.update(
                    {
                        "GPS": f"{latitude:.7f}, {longitude:.7f}",
                        "Ground speed": f"{int.from_bytes(payload[8:10], 'big') / 10:.1f} km/h",
                        "Heading": f"{int.from_bytes(payload[10:12], 'big') / 100:.2f}°",
                        "Altitude": f"{int.from_bytes(payload[12:14], 'big') - 1000} m",
                        "Satellites": str(payload[14]),
                    }
                )
                self._add_packet(candidate, "GPS 0x02")
            elif frame_type == self.VARIO and len(payload) >= 2:
                self._metadata["Vario"] = (
                    f"{int.from_bytes(payload[:2], 'big', signed=True) / 100:.2f} m/s"
                )
                self._add_packet(candidate, "VARIO 0x07")
            elif frame_type == self.BATTERY_SENSOR and len(payload) >= 8:
                capacity = int.from_bytes(payload[4:7], "big")
                self._metadata.update(
                    {
                        "Battery voltage": f"{int.from_bytes(payload[0:2], 'big') / 10:.1f} V",
                        "Battery current": f"{int.from_bytes(payload[2:4], 'big') / 10:.1f} A",
                        "Consumed": f"{capacity} mAh",
                        "Battery remaining": f"{payload[7]} %",
                    }
                )
                self._add_packet(candidate, "BATTERY_SENSOR 0x08")
            elif frame_type == self.BARO_ALTITUDE and len(payload) >= 2:
                encoded = int.from_bytes(payload[:2], "big")
                altitude = (encoded - 10000) / 10 if encoded < 0x8000 else (encoded - 0x8000) * 100
                self._metadata["Barometric altitude"] = f"{altitude:.1f} m"
                self._add_packet(candidate, "BARO_ALTITUDE 0x09")
            elif frame_type == self.ATTITUDE and len(payload) >= 6:
                pitch, roll, yaw = (
                    int.from_bytes(payload[index : index + 2], "big", signed=True)
                    / 10000
                    * 180
                    / math.pi
                    for index in (0, 2, 4)
                )
                self._metadata.update(
                    {
                        "Pitch": f"{pitch:.2f}°",
                        "Roll": f"{roll:.2f}°",
                        "Yaw": f"{yaw:.2f}°",
                    }
                )
                self._add_packet(candidate, "ATTITUDE 0x1E")
            elif frame_type == self.FLIGHT_MODE and payload:
                mode = payload.split(b"\x00", 1)[0].decode("ascii", "replace")
                self._metadata["Flight mode"] = mode
                self._add_packet(candidate, "FLIGHT_MODE 0x21")
            elif frame_type == self.DEVICE_INFO and len(payload) >= 3:
                name = payload[2:].split(b"\x00", 1)[0].decode("ascii", "replace")
                if name:
                    self._metadata["CRSF device"] = name
                self._add_packet(candidate, "DEVICE_INFO 0x29")

        return decoded

    def _add_packet(self, candidate: bytes, frame_name: str) -> None:
        self._packets.append(
            ProtocolPacket(
                protocol="CRSF",
                raw_frame=candidate,
                frame_name=frame_name,
                metadata=tuple(self._metadata.items()),
            )
        )

    def _decode_subset(self, candidate: bytes, payload: bytes) -> ChannelFrame | None:
        config = payload[0]
        start_channel = config & 0x1F
        bits = 10 + ((config >> 5) & 0x03)
        packed = payload[1:]
        count = min((len(packed) * 8) // bits, 32 - start_channel)
        if count <= 0 or start_channel >= 32:
            return None

        packed_value = int.from_bytes(packed, "little")
        mask = (1 << bits) - 1
        for index in range(count):
            value = (packed_value >> (index * bits)) & mask
            channel = start_channel + index
            self._subset_values[channel] = value
            self._subset_us[channel] = round(988 + value * 1024 / mask)

        output_count = max(16, start_channel + count)
        channels = tuple(self._subset_values[:output_count])
        microseconds = tuple(self._subset_us[:output_count])
        percentages = tuple(
            max(0.0, min(100.0, (value - 988) / 10.24))
            for value in microseconds
        )
        return ChannelFrame(
            protocol="CRSF",
            channels=channels,
            raw_frame=candidate,
            channel_min=0,
            channel_max=mask,
            microseconds_override=microseconds,
            percent_override=percentages,
            metadata=tuple(self._metadata.items()),
            frame_name=f"SUBSET_RC_CHANNELS 0x17 • {bits}-bit • CH{start_channel + 1}+",
        )

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count


class SBUSParser:
    """Streaming parser for standard 25-byte SBUS/SBUS2 channel frames."""

    FRAME_LENGTH = 25
    START_BYTE = 0x0F
    END_BYTES = frozenset({0x00, 0x04, 0x14, 0x24, 0x34})

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while self.buffer:
            try:
                start = self.buffer.index(self.START_BYTE)
            except ValueError:
                self._drop(len(self.buffer))
                break

            if start:
                self._drop(start)

            if len(self.buffer) < self.FRAME_LENGTH:
                break

            candidate = bytes(self.buffer[: self.FRAME_LENGTH])
            flags = candidate[23]
            if candidate[24] not in self.END_BYTES or flags & 0xF0:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            del self.buffer[: self.FRAME_LENGTH]
            self.stats.valid_frames += 1
            decoded.append(
                ChannelFrame(
                    protocol="SBUS",
                    channels=_unpack_16x11_lsb(candidate[1:23]),
                    raw_frame=candidate,
                    digital_channel_17=bool(flags & 0x01),
                    digital_channel_18=bool(flags & 0x02),
                    frame_lost=bool(flags & 0x04),
                    failsafe=bool(flags & 0x08),
                )
            )
            self.stats.channel_frames += 1

        return decoded

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count


class IBUSParser:
    """Streaming parser for 32-byte FlySky/Turnigy iBUS servo frames."""

    FRAME_LENGTH = 32
    COMMAND = 0x40
    CHANNEL_COUNT = 18

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while len(self.buffer) >= 2:
            try:
                start = self.buffer.index(self.FRAME_LENGTH)
            except ValueError:
                self._drop(len(self.buffer))
                break

            if start:
                self._drop(start)
            if len(self.buffer) < self.FRAME_LENGTH:
                break
            if self.buffer[1] != self.COMMAND:
                self._drop(1)
                continue

            candidate = bytes(self.buffer[: self.FRAME_LENGTH])
            received_checksum = int.from_bytes(candidate[-2:], "little")
            expected_checksum = (0xFFFF - sum(candidate[:-2])) & 0xFFFF
            if received_checksum != expected_checksum:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            del self.buffer[: self.FRAME_LENGTH]
            primary_channels = [
                int.from_bytes(candidate[2 + index * 2 : 4 + index * 2], "little") & 0x0FFF
                for index in range(14)
            ]
            extended_channels = []
            for offset in range(3, 22, 6):
                extended_channels.append(
                    ((candidate[offset] & 0xF0) >> 4)
                    | (candidate[offset + 2] & 0xF0)
                    | ((candidate[offset + 4] & 0xF0) << 4)
                )
            has_extended_channels = any(extended_channels)
            channels = tuple(
                primary_channels
                + (extended_channels[:4] if has_extended_channels else [])
            )
            decoded.append(
                ChannelFrame(
                    protocol="iBUS",
                    channels=channels,
                    raw_frame=candidate,
                    channel_min=1000,
                    channel_max=2000,
                    microseconds_override=channels,
                    frame_name=(
                        "iBUS SERVO • 18-channel extended"
                        if has_extended_channels
                        else "iBUS SERVO • 14-channel"
                    ),
                )
            )
            self.stats.valid_frames += 1
            self.stats.channel_frames += 1

        return decoded

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count


class SUMDParser:
    """Streaming parser for Graupner SUMD V1/V3 frames (up to 32 channels)."""

    SYNC_BYTE = 0xA8
    MAX_CHANNELS = 32
    VALID_STATES = frozenset({0x01, 0x03, 0x81})

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while self.buffer:
            try:
                start = self.buffer.index(self.SYNC_BYTE)
            except ValueError:
                self._drop(len(self.buffer))
                break

            if start:
                self._drop(start)
            if len(self.buffer) < 3:
                break

            state = self.buffer[1]
            channel_count = self.buffer[2]
            if state not in self.VALID_STATES or not 1 <= channel_count <= self.MAX_CHANNELS:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            frame_length = 3 + channel_count * 2 + 2
            if len(self.buffer) < frame_length:
                break

            candidate = bytes(self.buffer[:frame_length])
            received_crc = int.from_bytes(candidate[-2:], "big")
            if crc16_ccitt(candidate[:-2]) != received_crc:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            del self.buffer[:frame_length]
            channels = tuple(
                int.from_bytes(candidate[3 + index * 2 : 5 + index * 2], "big")
                for index in range(channel_count)
            )
            microseconds = tuple(round(value / 8) for value in channels)
            decoded.append(
                ChannelFrame(
                    protocol="SUMD",
                    channels=channels,
                    raw_frame=candidate,
                    failsafe=state == 0x81,
                    channel_min=8000,
                    channel_max=16000,
                    microseconds_override=microseconds,
                )
            )
            self.stats.valid_frames += 1
            self.stats.channel_frames += 1

        return decoded

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count


class SpektrumParser:
    """Parser for 16-byte Spektrum satellite DSM2/DSMX channel packets."""

    FRAME_LENGTH = 16
    CHANNEL_COUNT = 12

    def __init__(self, resolution: int) -> None:
        if resolution not in (1024, 2048):
            raise ValueError("Spektrum resolution must be 1024 or 2048")
        self.resolution = resolution
        self.buffer = bytearray()
        self.stats = ParserStats()
        self._channels = [resolution // 2] * self.CHANNEL_COUNT
        self._highest_channel = 6

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()
        self._channels = [self.resolution // 2] * self.CHANNEL_COUNT
        self._highest_channel = 6

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while len(self.buffer) >= self.FRAME_LENGTH:
            candidate = bytes(self.buffer[: self.FRAME_LENGTH])
            values = self._decode_words(candidate)
            channel_ids = [channel for channel, _value in values]
            if (
                len(values) != 7
                or len(set(channel_ids)) != 7
                or any(channel >= self.CHANNEL_COUNT for channel in channel_ids)
            ):
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            del self.buffer[: self.FRAME_LENGTH]
            for channel, value in values:
                self._channels[channel] = value
                self._highest_channel = max(self._highest_channel, channel)

            count = self._highest_channel + 1
            channels = tuple(self._channels[:count])
            if self.resolution == 1024:
                protocol = "DSM2 1024"
                microseconds = tuple(round(988 + value) for value in channels)
                range_min, range_max = 12, 1012
            else:
                protocol = "DSMX 2048"
                microseconds = tuple(round(988 + value / 2) for value in channels)
                range_min, range_max = 24, 2024

            decoded.append(
                ChannelFrame(
                    protocol=protocol,
                    channels=channels,
                    raw_frame=candidate,
                    channel_min=range_min,
                    channel_max=range_max,
                    microseconds_override=microseconds,
                )
            )
            self.stats.valid_frames += 1
            self.stats.channel_frames += 1

        return decoded

    def _decode_words(self, frame: bytes) -> list[tuple[int, int]]:
        decoded: list[tuple[int, int]] = []
        for offset in range(2, self.FRAME_LENGTH, 2):
            word = int.from_bytes(frame[offset : offset + 2], "big")
            if self.resolution == 1024:
                channel = (word >> 10) & 0x3F
                value = word & 0x03FF
            else:
                channel = (word >> 11) & 0x0F
                value = word & 0x07FF
            decoded.append((channel, value))
        return decoded

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count


class FPortParser:
    """FrSky F.Port v1 control-frame parser with byte unescaping."""

    MARKER = 0x7E
    ESCAPE = 0x7D
    ESCAPE_MASK = 0x20
    CONTROL_TYPE = 0x00
    CONTROL_FRAME_LENGTH = 25

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()
        self._in_frame = False
        self._escaped = False

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()
        self._in_frame = False
        self._escaped = False

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        decoded: list[ChannelFrame] = []
        for byte in data:
            if byte == self.MARKER:
                if self._in_frame and self.buffer:
                    frame = self._process(bytes(self.buffer))
                    if frame is not None:
                        decoded.append(frame)
                self.buffer.clear()
                self._in_frame = True
                self._escaped = False
                continue
            if not self._in_frame:
                self.stats.bytes_dropped += 1
                continue
            if self._escaped:
                self.buffer.append(byte ^ self.ESCAPE_MASK)
                self._escaped = False
            elif byte == self.ESCAPE:
                self._escaped = True
            elif len(self.buffer) < 64:
                self.buffer.append(byte)
            else:
                self.stats.invalid_frames += 1
                self.stats.bytes_dropped += len(self.buffer) + 1
                self.buffer.clear()
                self._in_frame = False
        return decoded

    def _process(self, candidate: bytes) -> ChannelFrame | None:
        if len(candidate) < 4 or candidate[0] != len(candidate) - 2:
            self.stats.invalid_frames += 1
            return None
        if not frsky_checksum_is_good(candidate):
            self.stats.invalid_frames += 1
            return None

        self.stats.valid_frames += 1
        if candidate[1] != self.CONTROL_TYPE or candidate[0] != self.CONTROL_FRAME_LENGTH:
            return None
        if len(candidate) < 27:
            self.stats.invalid_frames += 1
            return None

        channels = _unpack_16x11_lsb(candidate[2:24])
        flags = candidate[24]
        rssi = candidate[25]
        self.stats.channel_frames += 1
        return ChannelFrame(
            protocol="F.Port",
            channels=channels,
            raw_frame=bytes([self.MARKER]) + candidate + bytes([self.MARKER]),
            digital_channel_17=bool(flags & 0x01),
            digital_channel_18=bool(flags & 0x02),
            frame_lost=bool(flags & 0x04),
            failsafe=bool(flags & 0x08),
            metadata=(("RSSI", f"{rssi} %"),),
            frame_name="F.Port CONTROL 0x00",
        )


class GHSTParser:
    """ImmersionRC Ghost RC-channel parser (legacy and 12-bit frames)."""

    ADDRESS_FC = 0x82
    MAX_FRAME_LENGTH = 14
    CONTROL_TYPES = frozenset(range(0x10, 0x20)) | frozenset(range(0x30, 0x40))

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()
        self._channels = [2048] * 4 + [512] * 12
        self._metadata: dict[str, str] = {}

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()
        self._channels = [2048] * 4 + [512] * 12
        self._metadata.clear()

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while len(self.buffer) >= 3:
            try:
                start = self.buffer.index(self.ADDRESS_FC)
            except ValueError:
                self._drop(len(self.buffer))
                break
            if start:
                self._drop(start)

            length = self.buffer[1]
            total = length + 2
            if length < 2 or total > self.MAX_FRAME_LENGTH:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue
            if len(self.buffer) < total:
                break

            candidate = bytes(self.buffer[:total])
            if crc8_dvb_s2(candidate[2:-1]) != candidate[-1]:
                self.stats.invalid_frames += 1
                self._drop(1)
                continue

            del self.buffer[:total]
            self.stats.valid_frames += 1
            frame_type = candidate[2]
            payload = candidate[3:-1]
            if frame_type not in self.CONTROL_TYPES or len(payload) < 6:
                continue

            primary_bits = int.from_bytes(payload[:6], "little")
            primary = [(primary_bits >> (index * 12)) & 0x0FFF for index in range(4)]
            is_legacy = 0x10 <= frame_type <= 0x1F
            if is_legacy:
                primary = [max(0, ((5 * value) >> 2) - 430) for value in primary]
            self._channels[:4] = primary

            low_type = frame_type & 0x0F
            start_index = {0x0: 4, 0x1: 8, 0x2: 12}.get(low_type)
            if start_index is not None and len(payload) >= 10:
                for index, value in enumerate(payload[6:10]):
                    converted = max(0, 5 * value - 108) if is_legacy else value << 2
                    self._channels[start_index + index] = converted
            elif low_type == 0x3 and len(payload) >= 10:
                tx_power = int.from_bytes(payload[9:10], "big", signed=True)
                self._metadata.update(
                    {
                        "Link Quality": f"{payload[6]} %",
                        "RSSI": f"-{payload[7]} dBm",
                        "RF Mode": str(payload[8]),
                        "TX Power": f"{tx_power} dBm",
                    }
                )

            channels = tuple(self._channels)
            microseconds = tuple(
                round(988 + value / 4) if index < 4 else round(988 + value)
                for index, value in enumerate(channels)
            )
            percentages = tuple(
                max(0.0, min(100.0, (value - 988) / 10.24))
                for value in microseconds
            )
            decoded.append(
                ChannelFrame(
                    protocol="GHST",
                    channels=channels,
                    raw_frame=candidate,
                    channel_min=0,
                    channel_max=4096,
                    microseconds_override=microseconds,
                    percent_override=percentages,
                    metadata=tuple(self._metadata.items()),
                    frame_name=f"GHST RC 0x{frame_type:02X}",
                )
            )
            self.stats.channel_frames += 1

        return decoded

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count


class MAVLinkParser:
    """MAVLink v1/v2 parser for RC_CHANNELS and RC_CHANNELS_RAW messages."""

    CRC_EXTRAS = {34: 237, 35: 244, 65: 118}

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.stats = ParserStats()

    def reset(self) -> None:
        self.buffer.clear()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[ChannelFrame]:
        self.stats.bytes_received += len(data)
        self.buffer.extend(data)
        decoded: list[ChannelFrame] = []

        while self.buffer:
            starts = [
                index
                for index in (
                    self.buffer.find(bytes([0xFE])),
                    self.buffer.find(bytes([0xFD])),
                )
                if index >= 0
            ]
            if not starts:
                self._drop(len(self.buffer))
                break
            start = min(starts)
            if start:
                self._drop(start)
            if len(self.buffer) < 8:
                break

            magic = self.buffer[0]
            payload_length = self.buffer[1]
            if magic == 0xFE:
                header_length = 6
                message_id = self.buffer[5]
                signed_length = 0
            else:
                if len(self.buffer) < 12:
                    break
                header_length = 10
                message_id = int.from_bytes(self.buffer[7:10], "little")
                signed_length = 13 if self.buffer[2] & 0x01 else 0

            total = header_length + payload_length + 2 + signed_length
            if len(self.buffer) < total:
                break
            candidate = bytes(self.buffer[:total])
            del self.buffer[:total]

            crc_extra = self.CRC_EXTRAS.get(message_id)
            if crc_extra is None:
                self.stats.valid_frames += 1
                continue
            checksum_offset = header_length + payload_length
            received_crc = int.from_bytes(candidate[checksum_offset : checksum_offset + 2], "little")
            calculated_crc = crc16_x25(candidate[1:checksum_offset] + bytes([crc_extra]))
            if received_crc != calculated_crc:
                self.stats.invalid_frames += 1
                continue

            self.stats.valid_frames += 1
            payload = candidate[header_length:checksum_offset]
            frame = self._decode_channels(candidate, payload, message_id, magic)
            if frame is not None:
                decoded.append(frame)
                self.stats.channel_frames += 1

        return decoded

    def _decode_channels(
        self, candidate: bytes, payload: bytes, message_id: int, magic: int
    ) -> ChannelFrame | None:
        if message_id == 65 and len(payload) >= 22:
            available = min(18, (len(payload) - 4) // 2)
            channels = tuple(
                int.from_bytes(payload[4 + index * 2 : 6 + index * 2], "little")
                for index in range(available)
            )
            channel_count = payload[40] if len(payload) > 40 else available
            rssi = payload[41] if len(payload) > 41 else 255
            frame_name = "MAVLink RC_CHANNELS (65)"
            port = None
            percentages = None
        elif message_id == 34 and len(payload) >= 22:
            scaled_values = tuple(
                int.from_bytes(
                    payload[4 + index * 2 : 6 + index * 2],
                    "little",
                    signed=True,
                )
                for index in range(8)
            )
            inactive = tuple(
                index + 1 for index, value in enumerate(scaled_values) if value == 32767
            )
            channels = tuple(0 if value == 32767 else value for value in scaled_values)
            microseconds = tuple(
                1500 if value == 32767 else round(1500 + value / 20)
                for value in scaled_values
            )
            percentages = tuple(
                50.0
                if value == 32767
                else max(0.0, min(100.0, (value + 10000) / 200))
                for value in scaled_values
            )
            port = payload[20]
            rssi = payload[21]
            channel_count = 8 - len(inactive)
            frame_name = "MAVLink RC_CHANNELS_SCALED (34)"
        elif message_id == 35 and len(payload) >= 22:
            channels = tuple(
                int.from_bytes(payload[4 + index * 2 : 6 + index * 2], "little")
                for index in range(8)
            )
            channel_count = 8
            port = payload[20]
            rssi = payload[21]
            frame_name = "MAVLink RC_CHANNELS_RAW (35)"
            percentages = None
        else:
            return None

        system_id = candidate[3] if magic == 0xFE else candidate[5]
        component_id = candidate[4] if magic == 0xFE else candidate[6]
        metadata = [
            ("System ID", str(system_id)),
            ("Component ID", str(component_id)),
            ("Channel count", str(channel_count)),
        ]
        if port is not None:
            metadata.append(("RC port", str(port)))
        if message_id == 34 and inactive:
            metadata.append(
                ("Inactive channels", ", ".join(f"CH{index}" for index in inactive))
            )
        if rssi != 255:
            metadata.append(("RSSI", str(rssi)))
        if message_id == 34:
            return ChannelFrame(
                protocol="MAVLink",
                channels=channels,
                raw_frame=candidate,
                channel_min=-10000,
                channel_max=10000,
                microseconds_override=microseconds,
                percent_override=percentages,
                metadata=tuple(metadata),
                frame_name=frame_name,
            )
        return ChannelFrame(
            protocol="MAVLink",
            channels=channels,
            raw_frame=candidate,
            channel_min=1000,
            channel_max=2000,
            microseconds_override=channels,
            metadata=tuple(metadata),
            frame_name=frame_name,
        )

    def _drop(self, count: int) -> None:
        del self.buffer[:count]
        self.stats.bytes_dropped += count
