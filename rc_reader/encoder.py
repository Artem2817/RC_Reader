from __future__ import annotations

from .protocols import crc8_dvb_s2, crc16_ccitt


OUTPUT_PROTOCOLS = ("CRSF", "SBUS", "iBUS", "SUMD")


def _us_to_crsf(value: int) -> int:
    return max(0, min(0x7FF, round(172 + (value - 988) * (1811 - 172) / 1024)))


def _pack_16x11(values: list[int]) -> bytes:
    padded = (values + [992] * 16)[:16]
    packed = sum((value & 0x7FF) << (index * 11) for index, value in enumerate(padded))
    return packed.to_bytes(22, "little")


def encode_channels(protocol: str, microseconds: tuple[int, ...] | list[int]) -> bytes:
    normalized = protocol.strip().upper()
    values_us = [max(750, min(2250, int(value))) for value in microseconds]
    if normalized == "CRSF":
        body = bytes([0x16]) + _pack_16x11([_us_to_crsf(value) for value in values_us])
        return bytes([0xC8, len(body) + 1]) + body + bytes([crc8_dvb_s2(body)])
    if normalized == "SBUS":
        payload = _pack_16x11([_us_to_crsf(value) for value in values_us])
        return bytes([0x0F]) + payload + b"\x00\x00"
    if normalized == "IBUS":
        channels = (values_us + [1500] * 14)[:14]
        body = bytes([0x20, 0x40]) + b"".join(
            max(1000, min(2000, value)).to_bytes(2, "little")
            for value in channels
        )
        checksum = (0xFFFF - sum(body)) & 0xFFFF
        return body + checksum.to_bytes(2, "little")
    if normalized == "SUMD":
        channels = (values_us + [1500] * 16)[:16]
        body = bytes([0xA8, 0x01, len(channels)]) + b"".join(
            (value * 8).to_bytes(2, "big") for value in channels
        )
        return body + crc16_ccitt(body).to_bytes(2, "big")
    raise ValueError(f"Unsupported output protocol: {protocol}")
