# RC Reader parser plugins

Place external parser modules (`*.py`) in this directory next to
`RC_Reader.exe`. A plugin must expose:

```python
PROTOCOL_SPEC = {
    "name": "My Protocol",
    "baudrate": 115200,
    "uart_format": "8N1",
    "wiring_note": "RX adapter <- receiver TX",
}


def create_parser():
    return MyStreamingParser()
```

The parser object must provide `feed(data: bytes) -> list[ChannelFrame]`, a
`stats` attribute compatible with `ParserStats`, and preferably `reset()`.
Plugins are executable Python code; only install modules from trusted sources.
