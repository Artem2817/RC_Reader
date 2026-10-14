# RC Reader

RC Reader is a Windows desktop workstation for receiving, detecting, inspecting and generating serial RC protocols. The application uses a Qt 6 interface and keeps UART I/O, protocol parsing and frame generation out of the GUI thread.

## Features

- live display of the channel count actually supplied by the receiver, up to 32 channels;
- frame rate, valid/error counters, frame-lost and failsafe state;
- asynchronous protocol and UART auto-detection with multi-frame confidence checks;
- bounded RX/TX HEX log, timestamps and decoded packet fields;
- periodic binary RC generation independent of GUI refresh timing;
- Manual, Sweep, Step, Preset and Sequence generator modes;
- JSON generator profiles;
- explicit warning when inverted or half-duplex signalling needs additional hardware;
- CH340/CH341, CP210x, FTDI and CDC/standard serial-port identification.

## Protocol support

| Protocol | RX | TX | Default UART | Notes |
|---|:---:|:---:|---|---|
| CRSF / ELRS | yes | yes | 420000 8N1 | ELRS uses CRSF on the wire |
| SBUS | yes | yes | 100000 8E2 | normally requires hardware inversion |
| SBUS2 | RC frames | yes | 100000 8E2 | SBUS2 telemetry slots are not generated |
| iBUS | yes | yes | 115200 8N1 | 14-channel TX, 14/18-channel RX |
| SUMD | yes | yes | 115200 8N1 | CRC16, up to 32-channel RX |
| F.Port | yes | no | 115200 8N1 | inverted half-duplex hardware is often required |
| GHST | yes | no | 420000 8N1 | half-duplex wiring |
| DSM2 1024 | yes | no | 125000 8N1 | 3.3 V satellite interface |
| DSMX 2048 | yes | no | 125000 8N1 | 3.3 V satellite interface |
| MAVLink 1/2 RC messages | yes | no | 115200 8N1 | RC_CHANNELS family |

SRXL2 is not advertised because the repository does not yet contain a verified parser or encoder. RX-only protocols are hidden from the Generator page rather than simulated.

## Installation and launch

The ready-to-run Windows application does not require Python:

```powershell
.\dist\RC_Reader.exe
```

You can also double-click `start.bat`. To run from source, Python 3.10 or newer is required:

```powershell
python -m pip install -r requirements.txt
python main.py
```

`start.bat` runs the source entry point. A standalone Windows build is available as `dist\\RC_Reader.exe`.

To rebuild the executable:

```powershell
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --clean --noconfirm RC_Reader.spec
```

## Safe transmission

TX never starts automatically. Select a port and protocol, set safe channel values, then press **Start TX** and confirm the hardware warning. **EMERGENCY STOP** halts the generator immediately, and closing the application also requests an immediate stop. Transmission is never resumed after reconnecting an adapter.

Most CH340/CH341 adapters can transmit ordinary iBUS, SUMD and CRSF data when their driver accepts the selected baud rate. They do not normally provide the inverted electrical signal required by SBUS. Use a hardware inverter or a UART adapter that explicitly supports inversion. Half-duplex protocols may also require an external interface.

## Architecture

```text
rc_reader/
├── app.py                         # lightweight application entry point
├── core/
│   ├── protocol_registry.py       # protocol capabilities and common adapter API
│   ├── generator.py               # periodic TX service and JSON profiles
│   └── ports.py                   # USB-UART discovery and hardware warnings
├── ui/
│   ├── main_window.py             # navigation and bounded event dispatch
│   ├── pages.py                   # Monitor, Generator and Diagnostics pages
│   ├── widgets.py                 # reusable channel widgets
│   └── theme.py                   # dark Qt stylesheet
├── protocols.py                   # streaming protocol parsers
├── encoder.py                     # binary frame encoders
├── serial_worker.py               # RX and auto-detect workers
└── inspector.py                   # decoded packet structure
```

The GUI polls a bounded event queue at 30 ms intervals. Serial readers, auto-detection and periodic transmission run in background threads. Protocol parsers recover synchronization from incomplete frames and noisy input.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The suite covers parsers, CRC/checksum handling, partial and corrupted streams, synchronization recovery, auto-detection confidence, fixed iBUS reference output, all TX encoders, capability declarations, generator profile validation, packet inspection and exports.

Hardware-dependent behavior still needs loopback or receiver testing on the target Windows machine because driver support for unusual baud rates varies by adapter.
