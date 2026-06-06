# De'Longhi Coffee Link for Home Assistant

[![GitHub release (latest by date)](https://img.shields.io/github/v/release/actabi/delonghi_coffeelink?style=for-the-badge)](https://github.com/actabi/delonghi_coffeelink/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![License](https://img.shields.io/github/license/actabi/delonghi_coffeelink?style=for-the-badge)](LICENSE)
[![Validate](https://img.shields.io/github/actions/workflow/status/actabi/delonghi_coffeelink/validate.yml?branch=main&style=for-the-badge)](https://github.com/actabi/delonghi_coffeelink/actions)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=actabi&repository=delonghi_coffeelink&category=integration) 
[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=delonghi_coffeelink)

Home Assistant custom integration for DeLonghi PrimaDonna Soul and other Ayla-based DeLonghi coffee machines, controlled through the Coffee Link cloud.

## Supported machines

Any DeLonghi coffee machine exposed by the Coffee Link mobile app through Ayla Networks IoT. Confirmed working:

- **PrimaDonna Soul** ECAM610.xx, ECAM612.xx, ECAM613.xx (`oem_model = DL-millcore`) - works out of the box.
- **Eletta Explore** ECAM45x.xx (`oem_model = DL-striker-cb`) - beverages and power-on confirmed working; needs a one-time "teach from the app" step per drink (see [Eletta Explore](#eletta-explore-and-other-non-soul-models) below).
- Other Coffee Link Wi-Fi machines may work; the same teach-from-app mechanism applies.

## Features

- 21 beverage buttons (Espresso, Cappuccino, Latte Macchiato, Hot Water, Tea, etc.)
- Counters sensors (total beverages, per-drink counters, descale status)
- Generic Stop button
- Services for raw binary command injection (advanced use)

## Eletta Explore (and other non-Soul models)

The PrimaDonna Soul this integration was built on uses a fixed beverage command. The **Eletta Explore** (`oem_model = DL-striker-cb`) - and likely other non-Soul models - uses a **different, variable-length command** (the recipe, quantity, intensity and milk are all encoded in the bytes, and the frame carries a per-device signature). Rather than guess those bytes, the integration **learns the exact command your machine's official app sends and replays it** - which is reliable by construction.

So on an Eletta Explore there is a **one-time teach step**, after which everything works from Home Assistant (and is remembered across restarts):

**Beverages**
1. Make sure Home Assistant is running and the machine is online.
2. **Start each drink once from the official Coffee Link app.** Home Assistant captures the exact bytes (you can confirm on the *Last Captured Command* diagnostic sensor: `style: eletta`, `crc_valid: true`).
3. From then on, the matching Home Assistant button (or `start_beverage` service) brews that drink.

**Power-on (Wake)**
1. With Home Assistant running, **power the machine on once from the official app.**
2. From then on the **Wake** button powers it from standby.

> If you change a drink's settings in the app (e.g. quantity), start it once more from the app so Home Assistant re-learns the new bytes.

A read-only **Dump Recipe Datapoints** diagnostic button is also provided; it logs the recipe definitions the machine stores (it sends nothing to the machine). See [issue #1](https://github.com/actabi/delonghi_coffeelink/issues/1) for the reverse-engineering details.

## IMPORTANT - Coffee Link mobile app must be closed

The machine prioritizes LAN connections over cloud. As long as the Coffee Link mobile app is running on a phone on the same Wi-Fi network, it holds a LAN session (30s keep-alive) and the machine ignores cloud commands.

**Close the Coffee Link app completely** (swipe from recents) before using Home Assistant. If you want to regain control from the app, just reopen it.

A future version of this integration will include a local LAN server to bypass this limitation.

## Installation

### Via HACS (recommended)

**One-click install** - click the badge below (requires HACS already installed in your HA) :

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=actabi&repository=delonghi_coffeelink&category=integration)

Or manually :

1. In HACS, click the 3-dots menu > Custom repositories
2. Add `https://github.com/actabi/delonghi_coffeelink` as category **Integration**
3. Install "De'Longhi Coffee Link"
4. Restart Home Assistant
5. Click this badge to add the integration :

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=delonghi_coffeelink)

6. Enter your Coffee Link email and password (same as the mobile app)

### Manual

1. Copy `custom_components/delonghi_coffeelink/` to your HA `config/custom_components/`
2. Restart Home Assistant
3. Add integration via Settings > Devices & Services

## Services

```yaml
# Start a beverage
service: delonghi_coffeelink.start_beverage
data:
  beverage: hot_water  # espresso, cappuccino, latte_macchiato, etc.

# Stop a beverage
service: delonghi_coffeelink.stop_beverage
data:
  beverage: hot_water

# Send a raw binary command (advanced)
service: delonghi_coffeelink.send_raw_command
data:
  value_base64: DQ2D8BABDwD6GwEGgSRp6Myg
```

## Technical details

This integration implements the Coffee Link authentication and command protocol:

1. Authenticate to Gigya (SAP Customer Data Cloud) identity service
2. Request a signed JWT (HMAC-SHA1 over the Gigya session)
3. Exchange the JWT for an Ayla Networks SSO token
4. Poll Ayla Networks IoT cloud for 312 device properties
5. Send binary commands via the `data_request` property (base64-encoded)

Beverage command format - **PrimaDonna Soul** (fixed 18 bytes):

```
byte  0-1  : 0x0d 0x0d       prefix + length
byte  2-3  : 0x83 0xf0       command family: beverage
byte  4    : beverage_id     0x01 = espresso, 0x10 = hot water, etc.
byte  5    : action          0x01 = start, 0x02 = stop
byte  6-11 : recipe params   temperature, quantity, aroma
byte 12-13 : CRC16 AUG-CCITT over bytes 0..11
byte 14-17 : Unix timestamp (big-endian)
```

**Eletta Explore** (`DL-striker-cb`) uses a **variable-length** frame: the same `0x83 0xf0 <bev> <action>` header, then a variable recipe block (quantity in ml, intensity and milk encoded inline), the same CRC16 AUG-CCITT (over the whole frame before the CRC), the timestamp, and a 4-byte per-device signature. Because the recipe layout varies, the integration does not synthesize this frame; it **replays the exact frame captured from the official app** (see [Eletta Explore](#eletta-explore-and-other-non-soul-models)). The power-on frame uses family `0x84 0x0f` and is handled the same way.

## Diagnostics - capturing what the official app sends

If commands are accepted by the cloud (no error) but your machine does nothing
- common on models other than the PrimaDonna Soul this was built on - the
integration can capture the **exact bytes the official Coffee Link app sends**,
so they can be compared to what it generates.

Each device has a diagnostic sensor **Last Captured Command**
(`sensor.<machine>_last_captured_command`). To use it:

1. Make sure the integration is running and the device is online.
2. Open the **official Coffee Link app** and start a beverage (e.g. an espresso).
3. Within one polling cycle (~30 s), open **Developer Tools -> States** and look
   at `sensor.<machine>_last_captured_command`.
   - **State** = the app's raw base64 command.
   - **Attributes** decode it: `origin` (`app` vs `integration`), `beverage_name`,
     `params`, `crc_valid`, and **`matches_integration`**.
4. `matches_integration: true` means the integration generates the same bytes as
   the app (so any "machine ignores it" issue is environmental - e.g. the app
   holding the local session, see the note above). `matches_integration: false`
   means the app uses different bytes - **paste this sensor's attributes into a
   GitHub issue** and the command builder can be corrected for your model.

App-originated captures are also logged (enable debug logging for
`custom_components.delonghi_coffeelink`) as `CAPTURED app->machine command ...`.

## Credits

- Reverse engineering of Coffee Link auth & protocol: @actabi (2026)
- Based on the Ayla Networks LAN protocol research from [jakecrowley/AylaLocalAPI](https://github.com/jakecrowley/AylaLocalAPI)
- DeLonghi BLE protocol research from [Arbuzov/home_assistant_delonghi_primadonna](https://github.com/Arbuzov/home_assistant_delonghi_primadonna)

## Disclaimer

This is an unofficial integration. De'Longhi and Ayla Networks may change the protocol at any time. Use at your own risk.

## License

MIT
