from __future__ import annotations

from html import escape
import json
from pathlib import Path
from typing import Iterable

from .analysis import Alert, ChannelMetrics


def export_html_report(
    path: str | Path,
    snapshot: dict[str, object],
    channels: Iterable[ChannelMetrics],
    alerts: Iterable[Alert],
) -> None:
    channel_rows = "\n".join(
        "<tr>"
        f"<td>CH {item.channel:02}</td><td>{item.current_us}</td>"
        f"<td>{item.minimum_us}</td><td>{item.maximum_us}</td>"
        f"<td>{item.mean_us:.1f}</td><td>{item.jitter_us:.2f}</td>"
        f"<td>{item.noise_us:.2f}</td><td>{escape(item.kind)}</td>"
        "</tr>"
        for item in channels
        if item.samples
    )
    alert_rows = "\n".join(
        f"<tr><td>{item.timestamp:.3f}</td><td>{escape(item.level)}</td>"
        f"<td>{escape(item.message)}</td></tr>"
        for item in alerts
    ) or "<tr><td colspan='3'>Событий не обнаружено</td></tr>"
    summary_cards = "\n".join(
        f"<div class='card'><span>{escape(str(key))}</span><strong>{escape(str(value))}</strong></div>"
        for key, value in snapshot.items()
        if key != "protocols"
    )
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>RC Reader Report</title>
<style>
body{{font:14px 'Segoe UI',Arial;background:#09111f;color:#e7edf7;margin:32px}}
h1,h2{{color:#60a5fa}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.card{{background:#17243a;padding:14px;border:1px solid #263650;border-radius:8px}}
.card span{{display:block;color:#8fa1b9}} .card strong{{font-size:20px}}
table{{width:100%;border-collapse:collapse;background:#0f1a2b;margin:12px 0 26px}}
th,td{{padding:8px;border:1px solid #263650;text-align:left}} th{{color:#8fa1b9}}
</style></head><body>
<h1>RC Reader — Signal Quality Report</h1>
<div class="grid">{summary_cards}</div>
<h2>Protocols</h2><pre>{escape(json.dumps(snapshot.get("protocols", {}), ensure_ascii=False, indent=2))}</pre>
<h2>Channel metrics</h2>
<table><tr><th>Channel</th><th>Current µs</th><th>Min</th><th>Max</th>
<th>Mean</th><th>Jitter µs</th><th>Center noise</th><th>Type</th></tr>{channel_rows}</table>
<h2>Events</h2><table><tr><th>Time</th><th>Level</th><th>Message</th></tr>{alert_rows}</table>
</body></html>"""
    Path(path).write_text(document, encoding="utf-8")


def export_pdf_report(
    path: str | Path,
    snapshot: dict[str, object],
    channels: Iterable[ChannelMetrics],
    alerts: Iterable[Alert],
) -> None:
    """Write a compact dependency-free PDF using built-in Helvetica."""
    lines = ["RC Reader - Signal Quality Report", ""]
    lines.extend(
        f"{key}: {value}"
        for key, value in snapshot.items()
        if key != "protocols"
    )
    lines.append(f"protocols: {snapshot.get('protocols', {})}")
    lines.extend(("", "Channel   Current   Min   Max   Jitter   Noise   Type"))
    for item in channels:
        if item.samples:
            lines.append(
                f"CH{item.channel:02}    {item.current_us:4}    {item.minimum_us:4}  "
                f"{item.maximum_us:4}   {item.jitter_us:6.2f}  "
                f"{item.noise_us:6.2f}  {item.kind}"
            )
    lines.extend(("", "Events"))
    lines.extend(
        f"{item.timestamp:.3f} [{item.level}] {item.message}"
        for item in alerts
    )
    _write_simple_pdf(Path(path), lines)


def _write_simple_pdf(path: Path, lines: list[str]) -> None:
    pages = [lines[index : index + 48] for index in range(0, len(lines), 48)] or [[]]
    objects: list[bytes] = []

    def add_object(content: bytes) -> int:
        objects.append(content)
        return len(objects)

    font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    pages_id = 2 + len(pages) * 2
    for page_lines in pages:
        commands = [b"BT /F1 10 Tf 46 800 Td 13 TL"]
        for index, line in enumerate(page_lines):
            safe = (
                line.encode("ascii", "replace")
                .replace(b"\\", b"\\\\")
                .replace(b"(", b"\\(")
                .replace(b")", b"\\)")
            )
            commands.append((b"" if index == 0 else b"T* ") + b"(" + safe + b") Tj")
        commands.append(b"ET")
        stream = b"\n".join(commands)
        content_id = add_object(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        content_ids.append(content_id)
        page_id = add_object(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(page_id)
    add_object(
        (
            f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
            + " ".join(f"{page_id} 0 R" for page_id in page_ids)
            + "] >>"
        ).encode()
    )
    catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, content in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(content)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(output)
