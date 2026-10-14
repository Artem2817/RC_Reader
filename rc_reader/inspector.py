from __future__ import annotations

from dataclasses import dataclass

from .protocols import ChannelFrame, ProtocolPacket


@dataclass(frozen=True, slots=True)
class PacketField:
    offset: str
    length: int
    name: str
    value: str
    raw_hex: str


def _field(raw: bytes, offset: int, length: int, name: str, value: object) -> PacketField:
    return PacketField(
        str(offset),
        length,
        name,
        str(value),
        raw[offset : offset + length].hex(" ").upper(),
    )


def inspect_packet(packet: ChannelFrame | ProtocolPacket) -> tuple[PacketField, ...]:
    raw = packet.raw_frame
    protocol = packet.protocol.upper()
    fields: list[PacketField] = []
    if not raw:
        return ()

    if protocol == "CRSF" and len(raw) >= 4:
        fields.extend(
            (
                _field(raw, 0, 1, "Destination", f"0x{raw[0]:02X}"),
                _field(raw, 1, 1, "Frame length", raw[1]),
                _field(raw, 2, 1, "Frame type", f"0x{raw[2]:02X}"),
            )
        )
        if len(raw) > 4:
            fields.append(_field(raw, 3, len(raw) - 4, "Payload", f"{len(raw) - 4} bytes"))
        fields.append(_field(raw, len(raw) - 1, 1, "CRC8 DVB-S2", "OK"))
    elif protocol in ("SBUS", "F.PORT"):
        marker_offset = 1 if protocol == "F.PORT" and raw[0] == 0x7E else 0
        if protocol == "F.PORT":
            fields.append(_field(raw, 0, 1, "Frame marker", "0x7E"))
            fields.append(_field(raw, marker_offset, 1, "Length", raw[marker_offset]))
            fields.append(_field(raw, marker_offset + 1, 1, "Type", "CONTROL"))
            channel_offset = marker_offset + 2
        else:
            fields.append(_field(raw, 0, 1, "Start", "0x0F"))
            channel_offset = 1
        if len(raw) >= channel_offset + 22:
            fields.append(_field(raw, channel_offset, 22, "Packed channels", "16 × 11-bit LSB"))
        if protocol == "SBUS" and len(raw) >= 25:
            fields.append(_field(raw, 23, 1, "Flags", f"0b{raw[23]:08b}"))
            fields.append(_field(raw, 24, 1, "End", f"0x{raw[24]:02X}"))
        elif protocol == "F.PORT" and len(raw) >= 29:
            fields.append(_field(raw, 25, 1, "Flags", f"0b{raw[25]:08b}"))
            fields.append(_field(raw, 26, 1, "RSSI", raw[26]))
            fields.append(_field(raw, 27, 1, "Checksum", "OK"))
            fields.append(_field(raw, 28, 1, "Frame marker", "0x7E"))
    elif protocol == "IBUS" and len(raw) >= 4:
        fields.extend(
            (
                _field(raw, 0, 1, "Length", raw[0]),
                _field(raw, 1, 1, "Command", f"0x{raw[1]:02X}"),
                _field(raw, 2, max(0, len(raw) - 4), "Channel slots", "14 × 16-bit LE"),
                _field(raw, len(raw) - 2, 2, "Checksum", "OK"),
            )
        )
    elif protocol == "SUMD" and len(raw) >= 5:
        fields.extend(
            (
                _field(raw, 0, 1, "Sync", "0xA8"),
                _field(raw, 1, 1, "Status", f"0x{raw[1]:02X}"),
                _field(raw, 2, 1, "Channel count", raw[2]),
                _field(raw, 3, max(0, len(raw) - 5), "Channels", "16-bit BE"),
                _field(raw, len(raw) - 2, 2, "CRC16 CCITT", "OK"),
            )
        )
    elif protocol.startswith("DSM") and len(raw) == 16:
        fields.append(_field(raw, 0, 2, "Header / fades", int.from_bytes(raw[:2], "big")))
        for index in range(7):
            fields.append(
                _field(raw, 2 + index * 2, 2, f"Channel word {index + 1}", "ID + value")
            )
    elif protocol == "GHST" and len(raw) >= 4:
        fields.extend(
            (
                _field(raw, 0, 1, "Address", f"0x{raw[0]:02X}"),
                _field(raw, 1, 1, "Length", raw[1]),
                _field(raw, 2, 1, "Type", f"0x{raw[2]:02X}"),
                _field(raw, 3, max(0, len(raw) - 4), "Payload", "RC / link data"),
                _field(raw, len(raw) - 1, 1, "CRC8 DVB-S2", "OK"),
            )
        )
    elif protocol == "MAVLINK" and len(raw) >= 8:
        v2 = raw[0] == 0xFD
        fields.append(_field(raw, 0, 1, "Magic", "MAVLink 2" if v2 else "MAVLink 1"))
        fields.append(_field(raw, 1, 1, "Payload length", raw[1]))
        header_length = 10 if v2 else 6
        message_id = int.from_bytes(raw[7:10], "little") if v2 else raw[5]
        fields.append(
            _field(raw, 7 if v2 else 5, 3 if v2 else 1, "Message ID", message_id)
        )
        fields.append(_field(raw, header_length, raw[1], "Payload", f"{raw[1]} bytes"))
        fields.append(_field(raw, header_length + raw[1], 2, "CRC X25 + Extra", "OK"))
    else:
        fields.append(_field(raw, 0, len(raw), "Raw frame", f"{len(raw)} bytes"))

    for key, value in packet.metadata:
        fields.append(PacketField("—", 0, key, value, ""))
    return tuple(fields)
