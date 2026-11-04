# Third-party notices

The Fahis license does not apply to these components. Each remains under its own license, and copyright stays with its owners.

## Included in this repository

| Component | Used for | Owner | License |
|---|---|---|---|
| Kia Picanto profile and catalog of profile names (`profiles/`) | Reading extra car values | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0, full text in [profiles/LICENSE](profiles/LICENSE) |
| Test recordings in `test_obd_tool.py` | Offline tests | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0 |
| Fault code descriptions (`data/dtc_generic.json`) | Describing fault codes | [OBDex](https://github.com/foerbsnavi/OBDex) contributors | CC0 1.0, full text in [data/LICENSE-OBDex](data/LICENSE-OBDex) |

## Downloaded when you use Fahis

| Component | Used for | Owner | License |
|---|---|---|---|
| Other car profiles, saved to `profiles/downloaded/` | Reading extra car values | [OBDb](https://github.com/OBDb) editors and contributors | CC BY-SA 4.0 |

## Installed during setup

These are installed by `pip` and aren't stored in this repository.

| Component | Used for | Owner | License |
|---|---|---|---|
| [bleak](https://github.com/hbldh/bleak) | Bluetooth LE connection | Henrik Blidh and contributors | MIT |
| `winrt-runtime` and `winrt-Windows.*` | Windows Bluetooth access, used by bleak | The pywinrt contributors | MIT |
| `typing_extensions` | Python support, used by bleak | The Python Software Foundation and contributors | PSF-2.0 |
| [Electron](https://github.com/electron/electron), installed by `npm` | Desktop window | OpenJS Foundation and Electron contributors | MIT |
