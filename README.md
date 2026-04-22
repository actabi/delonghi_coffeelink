# De'Longhi Coffee Link for Home Assistant

[![GitHub release (latest by date)](https://img.shields.io/github/v/release/actabi/delonghi_coffeelink?style=for-the-badge)](https://github.com/actabi/delonghi_coffeelink/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![License](https://img.shields.io/github/license/actabi/delonghi_coffeelink?style=for-the-badge)](LICENSE)
[![Validate](https://img.shields.io/github/actions/workflow/status/actabi/delonghi_coffeelink/validate.yml?branch=main&style=for-the-badge)](https://github.com/actabi/delonghi_coffeelink/actions)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=actabi&repository=delonghi_coffeelink&category=integration) -> 
[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=delonghi_coffeelink)

Home Assistant custom integration for DeLonghi PrimaDonna Soul and other Ayla-based DeLonghi coffee machines, controlled through the Coffee Link cloud.

## Supported machines

Any DeLonghi coffee machine exposed by the Coffee Link mobile app through Ayla Networks IoT (tested with PrimaDonna Soul ECAM 612.55.SB). Likely compatible with:

- PrimaDonna Soul ECAM610.xx, ECAM612.xx, ECAM613.xx
- Eletta Explore ECAM450.xx (Wi-Fi models)
- Other Coffee Link Wi-Fi machines

## Features

- 21 beverage buttons (Espresso, Cappuccino, Latte Macchiato, Hot Water, Tea, etc.)
- Counters sensors (total beverages, per-drink counters, descale status)
- Generic Stop button
- Services for raw binary command injection (advanced use)

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

Command format (18 bytes):

```
byte  0-1  : 0x0d 0x0d       prefix + length
byte  2-3  : 0x83 0xf0       command family: beverage
byte  4    : beverage_id     0x01 = espresso, 0x10 = hot water, etc.
byte  5    : action          0x01 = start, 0x02 = stop
byte  6-11 : recipe params   temperature, quantity, aroma
byte 12-13 : CRC16 AUG-CCITT over bytes 0..11
byte 14-17 : Unix timestamp (big-endian)
```

## Credits

- Reverse engineering of Coffee Link auth & protocol: @actabi (2026)
- Based on the Ayla Networks LAN protocol research from [jakecrowley/AylaLocalAPI](https://github.com/jakecrowley/AylaLocalAPI)
- DeLonghi BLE protocol research from [Arbuzov/home_assistant_delonghi_primadonna](https://github.com/Arbuzov/home_assistant_delonghi_primadonna)

## Disclaimer

This is an unofficial integration. De'Longhi and Ayla Networks may change the protocol at any time. Use at your own risk.

## License

MIT
