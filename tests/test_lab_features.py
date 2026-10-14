import tempfile
import unittest
from pathlib import Path

from rc_reader.analysis import CalibrationWizard, DualComparator, SignalAnalyzer
from rc_reader.encoder import OUTPUT_PROTOCOLS, encode_channels
from rc_reader.inspector import inspect_packet
from rc_reader.protocols import (
    CRSFParser,
    ChannelFrame,
    IBUSParser,
    SBUSParser,
    SUMDParser,
    crc8_dvb_s2,
)
from rc_reader.reports import export_html_report, export_pdf_report


def make_channel_frame(
    values: tuple[int, ...],
    *,
    failsafe: bool = False,
    frame_lost: bool = False,
    metadata: tuple[tuple[str, str], ...] = (),
) -> ChannelFrame:
    return ChannelFrame(
        protocol="TEST",
        channels=values,
        raw_frame=b"\x01\x02",
        channel_min=1000,
        channel_max=2000,
        microseconds_override=values,
        failsafe=failsafe,
        frame_lost=frame_lost,
        metadata=metadata,
    )


def make_crsf(frame_type: int, payload: bytes) -> bytes:
    body = bytes([frame_type]) + payload
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8_dvb_s2(body)])


class SignalAnalyzerTests(unittest.TestCase):
    def test_tracks_dropouts_failsafe_and_channel_metrics(self) -> None:
        analyzer = SignalAnalyzer(dropout_threshold_ms=40)
        analyzer.ingest(make_channel_frame((1000, 1500)), timestamp=1.0)
        alerts = analyzer.ingest(
            make_channel_frame((2000, 1510), failsafe=True, frame_lost=True),
            timestamp=1.1,
        )

        self.assertEqual(analyzer.dropout_count, 1)
        self.assertEqual(analyzer.failsafe_count, 1)
        self.assertEqual(analyzer.frame_lost_count, 1)
        self.assertEqual(analyzer.channel_metrics(0).minimum_us, 1000)
        self.assertEqual(analyzer.channel_metrics(0).maximum_us, 2000)
        self.assertEqual({alert.level for alert in alerts}, {"DROP", "FAILSAFE", "LOST"})

    def test_low_lq_alert(self) -> None:
        analyzer = SignalAnalyzer(lq_alert_percent=30)
        alerts = analyzer.ingest(
            make_channel_frame((1500,), metadata=(("Link Quality", "12 %"),)),
            timestamp=10.0,
        )
        self.assertEqual(alerts[-1].level, "LOW LQ")


class CalibrationAndComparisonTests(unittest.TestCase):
    def test_maps_role_to_channel_with_largest_movement(self) -> None:
        wizard = CalibrationWizard(4)
        wizard.begin("Roll")
        wizard.ingest(make_channel_frame((1500, 1000, 1500, 1500)))
        wizard.ingest(make_channel_frame((1510, 2000, 1500, 1500)))
        result = wizard.finish()

        self.assertEqual(result, ("Roll", 2, 1000, 2000))

    def test_compares_two_receivers(self) -> None:
        comparator = DualComparator(4)
        comparator.ingest("primary", make_channel_frame((1000, 1500)), timestamp=1.0)
        comparator.ingest("secondary", make_channel_frame((1010, 1490)), timestamp=1.002)

        latency, deltas = comparator.compare() or (0, ())
        self.assertAlmostEqual(latency, 2.0)
        self.assertEqual(deltas, (10, -10))


class EncoderTests(unittest.TestCase):
    def test_output_encoders_generate_valid_frames(self) -> None:
        values = tuple(1000 + index * 50 for index in range(16))
        parsers = {
            "CRSF": CRSFParser(),
            "SBUS": SBUSParser(),
            "iBUS": IBUSParser(),
            "SUMD": SUMDParser(),
        }
        for protocol in OUTPUT_PROTOCOLS:
            with self.subTest(protocol=protocol):
                frames = parsers[protocol].feed(encode_channels(protocol, values))
                self.assertEqual(len(frames), 1)
                self.assertGreaterEqual(len(frames[0].channels), 14)


class InspectorAndTelemetryTests(unittest.TestCase):
    def test_inspector_exposes_sbus_fields(self) -> None:
        raw = encode_channels("SBUS", (1500,) * 16)
        frame = SBUSParser().feed(raw)[0]
        fields = inspect_packet(frame)
        self.assertIn("Packed channels", {field.name for field in fields})
        self.assertIn("Flags", {field.name for field in fields})

    def test_crsf_battery_packet_is_available_to_inspector(self) -> None:
        parser = CRSFParser()
        payload = (
            (168).to_bytes(2, "big")
            + (123).to_bytes(2, "big")
            + (456).to_bytes(3, "big")
            + bytes([75])
        )
        self.assertEqual(parser.feed(make_crsf(0x08, payload)), [])
        packets = parser.drain_packets()

        self.assertEqual(len(packets), 1)
        self.assertIn(("Battery voltage", "16.8 V"), packets[0].metadata)
        self.assertIn("Frame type", {field.name for field in inspect_packet(packets[0])})


class ReportTests(unittest.TestCase):
    def test_exports_html_and_pdf(self) -> None:
        analyzer = SignalAnalyzer()
        analyzer.ingest(make_channel_frame((1000, 1500)), timestamp=1.0)
        snapshot = analyzer.snapshot()
        metrics = [analyzer.channel_metrics(0), analyzer.channel_metrics(1)]

        with tempfile.TemporaryDirectory() as directory:
            html_path = Path(directory) / "report.html"
            pdf_path = Path(directory) / "report.pdf"
            export_html_report(html_path, snapshot, metrics, analyzer.alerts)
            export_pdf_report(pdf_path, snapshot, metrics, analyzer.alerts)

            self.assertIn("RC Reader", html_path.read_text(encoding="utf-8"))
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-1.4"))
            self.assertGreater(pdf_path.stat().st_size, 500)


if __name__ == "__main__":
    unittest.main()
