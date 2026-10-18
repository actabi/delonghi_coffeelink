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
- **Wake** and **Standby** buttons (power the machine on / off remotely)
- Counter sensors: lifetime totals split the way the machine actually splits
  them (black / coffee+milk / milk-only / other), per-drink counters, water and
  filter volumes in litres, descale status
- **Machine Status** (standby, ready, rinsing, dispensing...) and **Connection
  Status** sensors, plus **Last Connected** - when the machine established its
  current cloud connection
- Generic Stop button
- Services for raw binary command injection (advanced use)
- An **event entity** per machine (**Machine Event**): `status_changed`,
  `alarm_raised` / `alarm_cleared` and `beverage_dispensed` (from the per-drink
  counters; a counter that drops is treated as a reset, never as a drink)
- **Maintenance binary sensors** (water tank, grounds container, descale, filter,
  beans) on the PrimaDonna Soul too. They turn unavailable while the machine's
  status is stale instead of showing an old value
- **Diagnostics download** per device - see [Diagnostics download](#diagnostics-download)
- **Re-authentication** when De'Longhi refuses the password: Home Assistant asks
  for it again instead of retrying the same password (which can lock the
  account). A host clock that is more than about two minutes off is reported as
  such
- Dutch translation
- Reads the machine's **own recipe catalogue** from the properties it already
  publishes - every parameter of every drink on every profile, the editable
  min/default/max ranges the machine itself declares, the saved-recipe slots and
  the bean-system drink, plus each profile's ordered short list. Currently used
  to keep the diagnostics and the learn-and-replay path honest; it creates no
  entities yet.

### When the machine is unreachable

The cloud accepts a command for a machine that is offline and answers `200 OK`;
the machine simply never receives it. So when the cloud reports your machine as
offline, commands are **refused with a clear error** instead of being written
into the void - check that the machine is powered at the mains and joined to
Wi-Fi. *Connection Status* tells you how the cloud sees it right now, and *Last
Connected* when it last established that connection. A machine merely in standby
stays online and can still be woken, and `send_raw_command` is never blocked so
you can always poke a machine by hand.

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

**Power-off (Standby)**
The official app has no power-off control, so this frame is always synthesized
(`84 0f`, params `01 01` - validated live on the reference Soul). On Eletta-style
models it needs the per-device signature, which is taken from any frame already
learned (e.g. the wake frame) - so once the Wake teach step above is done, the
**Standby** button works too.

> If you change a drink's settings in the app (e.g. quantity), start it once more from the app so Home Assistant re-learns the new bytes.

> **Why the teach step also unlocks the cloud session.** ECAM machines only obey
> commands sent inside a cloud session registered with *their own* 4-byte device
> signature - the one they append to every frame. That signature is read from the
> first frame you teach, so before any teach step the machine may accept a command
> (HTTP 200, valid CRC, `machine_status: ready`) and simply do nothing. The
> `Cloud Session app_id` diagnostic sensor shows which id is in use through its
> `session_id_source` attribute (`device_signature` once taught, `default_constant`
> before). See [issue #15](https://github.com/actabi/delonghi_coffeelink/issues/15).

A read-only **Dump Recipe Datapoints** diagnostic button is also provided; it logs the recipe definitions the machine stores (it sends nothing to the machine). See [issue #1](https://github.com/actabi/delonghi_coffeelink/issues/1) for the reverse-engineering details.

## IMPORTANT - Coffee Link mobile app must be closed

The machine prioritizes LAN connections over cloud. As long as the Coffee Link mobile app is running on a phone on the same Wi-Fi network, it holds a LAN session (30s keep-alive) and the machine ignores cloud commands.

**Close the Coffee Link app completely** (swipe from recents) before using Home Assistant. If you want to regain control from the app, just reopen it.

A future version of this integration will include a local LAN server to bypass this limitation.

## Field notes: PrimaDonna Soul ECAM 610.75

Tested on an ECAM 610.75 (Wi-Fi module `ADA 1.5.3`) in October 2026:

- **Works from Home Assistant:** Wake and Standby (Machine Status follows within
  about 30 s), Hot Water and Cappuccino. The Cappuccino came out in the
  machine's normal quantity, although on the Soul the integration sends the
  captured hot-water parameter block for every drink.
- **Status refresh:** with the keep-alive running, the machine publishes its
  status about every 30 s.
- **Coffee Link app open:** a cloud Wake still worked with the app open in the
  foreground on a phone next to the machine, and the app did not notice the
  wake. Closing the app is still the safe default.
- **Request Status (test)** got no reply on the response channel. It is a
  diagnostic only.
- **Status gaps of about 5 minutes on weak Wi-Fi.** Now and then the machine
  stops publishing for about 5 minutes: Connection Status goes offline and
  Machine Status turns unknown, then both recover by themselves. A ping log
  showed the machine stayed on the network (with heavy packet loss); it lost
  Ayla's notification channel and fell back to polling the cloud every 5
  minutes. Commands sent in that window can arrive late. A stronger 2.4 GHz
  signal at the machine is the fix, not a setting in this integration.
- **Not verified on this model yet:** other drinks, the Stop button, the
  maintenance binary sensors and alarm bits, and `beverage_dispensed` events.

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

All three services take a **target**. With a single machine set up you can leave
it out and the machine is used. With several, the target is required: a call
without one is refused with an error naming your machines, rather than sent to
all of them.

```yaml
# Start a beverage
action: delonghi_coffeelink.start_beverage
target:
  device_id: 1a2b3c...        # or entity_id / area_id
data:
  beverage: hot_water          # espresso, cappuccino, latte_macchiato, etc.

# Stop a beverage
action: delonghi_coffeelink.stop_beverage
target:
  device_id: 1a2b3c...
data:
  beverage: hot_water

# Send a raw binary command (advanced)
action: delonghi_coffeelink.send_raw_command
target:
  device_id: 1a2b3c...
data:
  value_base64: DQ2D8BABDwD6GwEGgSRp6Myg
```

Naming several devices in one call is still allowed, and sends to each of them -
the difference is that it now has to be asked for.

```yaml
action: delonghi_coffeelink.start_beverage
target:
  device_id: [1a2b3c..., 4d5e6f...]
data:
  beverage: espresso
```

## Technical details

This integration implements the Coffee Link authentication and command protocol:

1. Authenticate to Gigya (SAP Customer Data Cloud) identity service
2. Request a signed JWT (HMAC-SHA1 over the Gigya session)
3. Exchange the JWT for an Ayla Networks SSO token
4. Poll Ayla Networks IoT cloud for the device properties (312 on the reference machine)
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

### Diagnostics download

Each machine's device page has **Download diagnostics** (in the device info
menu). It shows, per machine:

- which datapoints carry commands, responses, the keep-alive and the status
  (`properties`);
- the keep-alive: whether it runs, how many writes, the last success and the
  last error (`keepalive`);
- when the status was last published, its age and the decoded status
  (`monitor`), plus the publish time of every status source the machine has
  (`monitor.candidates`);
- the command and response channels (`channels`).

The e-mail address and password are redacted, and the machine's serial number,
settings PIN, MAC and IP address are left out. It does include the Ayla DSN of
the Wi-Fi module. If Machine Status stops updating, compare
`monitor.published_at` with `keepalive.last_ok`: a keep-alive that succeeds
while the status stays old points at the machine or its Wi-Fi, not at Home
Assistant.

## Adding a new machine model

Your machine isn't a PrimaDonna Soul or an Eletta Explore? **It very likely already works.** Unknown models default to the *learn-and-replay* path: trigger each drink (and power-on) once from the official Coffee Link app, Home Assistant captures the exact bytes and replays them (see [Eletta Explore](#eletta-explore-and-other-non-soul-models) for the procedure). That works on any Coffee Link machine without any code change.

If you'd like **first-class support** for your model (so it's recognised by name, and so we can document/optimise it), open an issue with the data below. Everything needed is already exposed by the integration's diagnostics - **no network sniffing, no rooting, just copy/paste.**

**1. Identify the machine**
- Commercial name + full reference under the machine (e.g. `Maestosa ECAM650.85.MS`).
- Coffee Link account region (EU / US).
- The `oem_model` and `sw_version`: enable debug logging for `custom_components.delonghi_coffeelink`, restart, and copy the `Discovered DeLonghi device:` log line.

**2. What the official app sends** (the ground truth we replay)
For each drink you care about, **start it from the official app** with Home Assistant running, then copy the **Attributes** of the `...Last Captured Command` sensor (Developer Tools → States). Do the same for **power-on**. Each block shows `style`, `beverage_id`, the full `recipe`/`hex`, `crc_valid`, and the device signature - exactly what's needed.

**3. The recipes the machine stores** (for the optional "zero-touch" path)
Press the **Dump Recipe Datapoints** diagnostic button and paste the logged block (it's read-only - it sends nothing to the machine). It selects datapoints by *blob family*, so it also catches your saved-recipe and bean-system recipes, and it deliberately leaves out the machine's serial number, its settings PIN and any name you typed in - the block is safe to paste into a public issue.

**4. Does it work after teaching?**
Tell us whether the Home Assistant buttons brew/power the machine after you've triggered them once from the app (yes/no per drink, and whether the machine was already on or in standby).

With that, adding first-class support is usually just a new `ModelProfile` subclass in [`model_profiles.py`](custom_components/delonghi_coffeelink/model_profiles.py) (detection rule + whether it learns from the app) - nothing else in the integration needs to change. The Eletta Explore reverse-engineering, captured frame-by-frame this way, is tracked in [issue #1](https://github.com/actabi/delonghi_coffeelink/issues/1) as a worked example.

## Credits

- Reverse engineering of Coffee Link auth & protocol: @actabi (2026)
- Based on the Ayla Networks LAN protocol research from [jakecrowley/AylaLocalAPI](https://github.com/jakecrowley/AylaLocalAPI)
- DeLonghi BLE protocol research from [Arbuzov/home_assistant_delonghi_primadonna](https://github.com/Arbuzov/home_assistant_delonghi_primadonna)

## Disclaimer

This is an unofficial integration. De'Longhi and Ayla Networks may change the protocol at any time. Use at your own risk.

## License

MIT
