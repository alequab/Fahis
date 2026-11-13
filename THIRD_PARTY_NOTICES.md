# Third-party notices

The Fahis license does not apply to these components. Each remains under its own license, and copyright stays with its owners.

## Included in this repository

| Component | Used for | Owner | License |
|---|---|---|---|
| Kia Picanto profile and catalog of profile names (`profiles/`) | Reading extra car values | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0, full text in [profiles/LICENSE](profiles/LICENSE) |
| Test recordings in `test_obd_tool.py` | Offline tests | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0 |
| Standard value definitions (`data/standard-pids.json`) | Decoding extra standard values | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0, full text in [profiles/LICENSE](profiles/LICENSE) |
| Fault code titles and details (`data/dtc_generic.json`, `data/dtc_details.json.gz`) | Describing fault codes, causes and symptoms | [OBDex](https://github.com/foerbsnavi/OBDex) contributors | CC0 1.0, full text in [data/LICENSE-OBDex](data/LICENSE-OBDex) |
| Manufacturer fault code names (`data/dtc_oem.json`) | Naming codes for the connected car's make | [Wal33D/dtc-database](https://github.com/Wal33D/dtc-database) contributors | MIT, full text in [data/LICENSE-dtc-database](data/LICENSE-dtc-database) |
| Repair plans (`data/repair_plans.json`, `data/repair_plans_ar.json`) | Repair steps with difficulty and estimates | [MechanicDB](https://github.com/MechanicDB/MechanicDB-public) free sample, with an Arabic translation and a few plans added for Fahis | ODbL 1.0, see [data/LICENSE-MechanicDB](data/LICENSE-MechanicDB) |

## Downloaded when you use Fahis

| Component | Used for | Owner | License |
|---|---|---|---|
| Other car profiles, saved to `profiles/downloaded/` | Reading extra car values | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0 |

## Installed during setup

These are installed by `pip` and aren't stored in this repository. The ready-made installer includes them, along with Python itself (PSF-2.0) and the PyInstaller bootloader (GPL-2.0 with its exception for bundled applications).

| Component | Used for | Owner | License |
|---|---|---|---|
| [bleak](https://github.com/hbldh/bleak) | Bluetooth LE connection | Henrik Blidh and contributors | MIT |
| `winrt-runtime` and `winrt-Windows.*` | Windows Bluetooth access, used by bleak | The pywinrt contributors | MIT |
| `typing_extensions` | Python support, used by bleak | The Python Software Foundation and contributors | PSF-2.0 |
| [Tauri](https://github.com/tauri-apps/tauri) and its Rust dependencies, built into `fahis.exe` | Desktop window | The Tauri Programme within The Commons Conservancy and contributors | MIT or Apache-2.0 |
| [Microsoft Edge WebView2](https://developer.microsoft.com/microsoft-edge/webview2/), part of Windows 10 and 11, not included | Draws the window's page | Microsoft | Microsoft software license |
