# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
- **`Total Beverages` was not the total, and `Total Water` had nothing to do
  with water.** The `d700_tot_bev_b` / `d701_tot_bev_bw` / `d703_tot_bev_w`
  suffixes are **b**lack / **b**lack+**w**hite / **w**hite, not
  "beverages" / "milk drinks" / "water". Verified by arithmetic against a live
  PrimaDonna Soul rather than by reading the names:

  | datapoint | was labelled | actually | proof on the reference machine |
  |---|---|---|---|
  | `d700_tot_bev_b` | Total Beverages | black drinks only | espresso 4 + coffee 4633 + doppio 1 = **4638 exact** |
  | `d703_tot_bev_w` | Total Water | milk-only drinks | hot_milk **24 = 24 exact** |
  | `d701_tot_bev_bw` | Total Milk Drinks | coffee + milk | 248 |

  So `Total Beverages` under-reported that machine's lifetime by ~6 %: the real
  figure is `4638 + 248 + 24 + 7 = 4917`.

  `entity_id`s are deliberately **unchanged** - `sensor.…_total_water` keeps its
  slug while displaying "Total Milk-Only Beverages". Renaming would break every
  existing history, statistic and automation for no functional gain.

### Added
- **`total_other_beverages` (`d702_tot_bev_other`)**, which was not exposed at
  all. Without it the four buckets can never be reconciled against the machine's
  own lifetime total.
- **Counters for beverage ids 24, 25 and 26** - `total_cortado`,
  `total_long_black`, `total_mug_to_go` (`d727_id24_cortado`,
  `d728_id25_long_black`, `d729_id26_travel_mug`). These three drinks had buttons
  in `BEVERAGES` and counters on the machine, but no sensor; every other id from
  1 to 27 had one.

### Changed
- Documented, and pinned with a test, that `COUNTER_SENSORS` matches datapoints
  by **exact full name**. d-numbers are not stable across models: the six
  `mug`/`iced`/`cold brew` entries name numbers that on a PrimaDonna Soul carry
  `d731_pregr_coff_cnt`, `d732_taste_b_bw`, `d735_b_water_qty`,
  `d736_bw_coff_water_qty`, `d737_bw_milk_time_qty` and
  `d738_espressi_water_qty`. Exact matching is the only reason none of them is
  published under a confident, wrong label.

## [0.3.19] - 2026-08-22

Everything here comes from one field diagnosis on the reference PrimaDonna Soul:
the machine had been off the network for ten days, and **the integration showed
no sign of it**. The wake button kept writing a perfectly valid frame that the
cloud accepted with `200 OK` and never delivered.

### Fixed
- **Commands sent to an unreachable machine are now refused, loudly.** Ayla
  accepts a datapoint write for an offline machine and returns `200`/`201`, so
  every wake, standby and beverage command was silently dropped - no toast, no
  error, nothing in the log. Those paths now check the cloud's
  `connection_status` first and raise a translated error naming the machine.
  The guard is deliberately narrow:
  - only an explicit `Offline` blocks - an unknown or unexpected status still
    goes through;
  - a machine merely in **standby stays online**, so waking it is unaffected;
  - a status older than three polling intervals **stops blocking**, because it
    only refreshes on a successful poll and a cloud outage would otherwise
    freeze it and keep blaming the machine long after it came back;
  - `send_raw_command` is **never** refused. It is the field-instrumentation
    escape hatch and has to keep working precisely when the integration's own
    idea of the machine's state is what is wrong. It warns instead.
- **`Last Connected` told the truth about the wrong thing.** It exposed the
  `data_updated_at` of the `device_connected` datapoint - an application-level
  ping that goes stale while the machine keeps talking to the cloud (two months
  out of date on the reference machine, which the cloud knew had connected ten
  days earlier), and which on cloud-session models is written by this
  integration itself. It now reports the cloud's own `connected_at`, i.e. when
  the machine established its current connection - read it next to
  `Connection Status`, not as a last-heard-from. No fallback: an unknown state
  beats a plausible wrong date.
- **`Machine Status` was permanently `unknown` on several Soul builds** (`#14`,
  thanks `@MarcFu`, `@AKWillows`, `@hoogjoe`). The monitor datapoint was
  hard-wired to `d302_monitor_machine`, but ECAM610.55 and ECAM612.55 publish
  `d302_monitor`. It is now resolved from a candidate list, like every other
  channel here, and the candidates are weighed on **every** poll: a candidate
  only wins if its blob actually decodes, so neither an always-null datapoint
  nor a stale one can lock the poll onto a name that never yields a status, and
  a machine that moves to the other datapoint after a firmware update is
  followed without a restart. The existing parser decodes those blobs unchanged;
  only the name was wrong.
- **A service call no longer stops at the first failing machine.** Services
  address every machine of the config entry; one failure - unreachable, an Ayla
  `5xx`, an expired token - used to abort the loop before the others were tried.
  Every coordinator is now attempted and the first error is re-raised afterwards.

### Changed
- On cloud-session machines (Eletta Explore family) that publish their monitor
  on `d302_monitor`, `Machine Status` starts working - and with it the
  deep-standby nudge that gates on it, which had been silently dead there. Those
  machines now get the usual session-refresh frame before a wake when they
  report standby, exactly as the ones that already resolved a monitor did.
- Minimum Home Assistant version declared to HACS raised from `2024.1.0` to
  `2024.8.0`. The declared floor was already fiction: translated exceptions need
  2024.2+, and the action metadata moved to `strings.json` in 0.3.18 needs
  2024.8+.

### Known limitations
- A refused command still advances the button entity's "last pressed"
  timestamp: Home Assistant stamps it before awaiting the press handler. The
  error toast and the log line are what tell you it did not go through.
- Services are registered per config entry under the same names, so with two
  De'Longhi accounts the last entry registered wins. Per-device targeting is the
  real fix (`#24`).
- On a cloud-session machine with a cold session the write happens after the
  handshake (up to `CONNECT_CONFIRM_TIMEOUT`); reachability is re-checked at
  that point, but an error raised there lands in the log, not in the UI.

## [0.3.18] - 2026-08-20

### Added
- **Complete Czech localization** (`#19`, `#22`, thanks `@kasiom`): config flow,
  actions, entity names and every enum state. `Connection Status` and
  `Machine Status` are now proper `SensorDeviceClass.ENUM` sensors with declared
  options, and action names/descriptions moved from `services.yaml` to
  `strings.json` where modern Home Assistant expects them, with a translated
  beverage selector.
- **German translation** (`#25`, thanks `@MarcFu`): translation-only, complete
  config flow and entity tree.
- **French, Russian and German translations completed** to full parity with
  `strings.json` (they were 78 of 128 keys: no enum states, no action metadata,
  no beverage selector - those would have fallen back to English). The French
  tree is also accented now; it had been ASCII-only.
- **CI runs the test suite** (`#21`, `#23`, thanks `@kasiom`): pytest with
  coverage on Python 3.12 and 3.13, Ruff lint and import order, every GitHub
  Action pinned to a SHA, job timeouts, least-privilege `permissions`, and
  Dependabot for actions and pip.

### Changed
- **BREAKING for statistics** - `Water Total Quantity` and `Water Filter
  Quantity` now report **litres** instead of a unitless count. De'Longhi
  machines publish these counters in millilitres (independently confirmed by
  De'Longhi's own statistics sheet, by `sk7n4k3d/delonghi-ha` which applies
  `scale=0.001` to the same datapoints, and by `PyDeLonghiAPI` which derives
  lifetime litres as `d553 / 1000`), and Home Assistant's water device class
  works in litres. Existing installations have long-term statistics recorded
  without a unit, so Home Assistant will raise a "units changed" repair for
  these two entities; accepting it keeps history consistent with the new unit.
  Values drop by a factor of 1000 in graphs - `387213` now reads `387.213 L`.
  Both keep `TOTAL_INCREASING`: they are lifetime meters, and a filter change
  resetting `d555` is exactly what that state class absorbs.
- The conversion is declared in `COUNTER_MEASUREMENTS` (const.py) next to
  `COUNTER_SENSORS`, not branched on entity keys inside the sensor platform, so
  a machine that publishes the same measurement under another datapoint is
  supported by extending that row - no entity code involved.
- The translation parity test now covers **every** language file found in
  `translations/`, discovered dynamically, and derives the expected machine
  statuses and beverage options from `const.py`. A new language, machine status
  or beverage now fails the suite until it is translated; previously only `en`
  and `cs` were checked, which is how `fr`/`ru`/`de` had drifted 50 keys behind.

## [0.3.17] - 2026-08-19

### Fixed
- **Eletta Explore ignored every command** (`#15`, reported and verified live by
  `DouglasPavanPy` on a 450.65.S). The cloud session was registered with a fixed
  made-up id (`INTEGRATION_CLOUD_APP_ID = 0xC0FFEE11`, identical for every user
  and every machine). Ayla accepted it, `app_id` confirmed it, commands returned
  HTTP 200/201 with a valid CRC - and the machine did nothing, silently. An ECAM
  only executes commands from a session registered with **its own 4-byte device
  signature**, the value it appends to every frame it exchanges with the
  official app. That signature is now read from any learned frame and used as
  the session id (and as the wake/standby session tail); the constant remains
  only as a fallback until a frame has been learned. Because learned frames are
  persisted, a clean restart with the official app closed uses the right id
  immediately. This also explains the old "it works right after using the
  official app, then stops" behaviour: the app's session was mislabelled as
  foreign, adopted transiently, then reverted to the broken constant.
- **Beverages whose recipe does not end with `01 0a` could never be learned**
  (`#15`). That 2-byte trailer was treated as an Eletta dialect marker, but it
  is recipe data and varies per drink (Coffee `0x02` ends with `01 06`), so its
  frame was decoded as a Soul frame and dropped by the learning gate - the
  button then logged "trigger this drink once from the official app" forever,
  even though the frame had been captured and was valid. The dialect is now
  decided by the frame shape, and a captured frame is learned whenever it is a
  beverage with a valid CRC and a known beverage id. Frames matching what this
  integration would emit itself are still ignored, so a best-effort fallback
  echoed back on the wire is never mistaken for the app's bytes.

### Changed
- The `Cloud Session app_id` diagnostic compares against the coordinator's own
  session id and exposes `session_id_source` (`device_signature` /
  `default_constant`).
- Cloud-session confirmation now waits for the id actually POSTed, so learning a
  frame mid-connect cannot leave a command waiting for an id never registered.

### Notes
- The PrimaDonna Soul (`DL-millcore`) is untouched: it holds no cloud session and
  learns no frame, so its session id stays the constant and its command bytes are
  unchanged. Covered by regression tests.

## [0.3.16] - 2026-06-22

### Added
- Russian translations (`translations/ru.json`). Credit: `TischenkoArseny` (#12).

## [0.3.15] - 2026-06-18

### Added
- **Eletta maintenance binary sensors**: water tank empty, waste container full,
  decalcification needed, filter change needed (`device_class: problem`), plus
  MonitorV2 `switches`/`alarms` parsing surfaced as attributes on Machine Status.
  Gated on the ECAM cloud-session profile, so the Soul is unaffected.
  Credit: `TischenkoArseny` (#9).

## [0.3.14] - 2026-06-17

### Added
- **Eletta Explore (450.65.G) counter sensors** mapped from the real Ayla
  datapoint dump contributed in #7 (`kasiom`): iced beverages, cold brew,
  hot/cold mug drinks, espresso/coffee/long/doppio/americano per-drink totals,
  total descales (`d552`), total water quantity (`d553`), filters used
  (`d554`), filtered-water quantity (`d555`), and more. Each sensor is only
  created when its datapoint is present on the device, so the PrimaDonna Soul
  is unaffected (absent datapoints are skipped, no orphan "unknown" entities).
- **Czech localisation** (`translations/cs.json`) and full **French** entity
  names (`translations/fr.json`), contributed/expanded from #7.

### Fixed
- **JSON-aggregated counters** (`#7`): newer models (Eletta Explore) publish
  some counters (e.g. `d735_iced_bev`, `d738_cold_brew_bev`) as a JSON blob of
  per-recipe sub-counts rather than a plain integer, which left the sensor
  `unknown`. The counter sensor now detects a `{...}` value, sums the integer
  sub-values for the sensor state, and exposes the full breakdown in
  `extra_state_attributes`. Plain-integer counters (Soul) are unchanged.
- **`Last Connected` showing raw base64** (`#7`): on models where
  `app_device_connected`/`device_connected` carries a session blob, the sensor
  no longer renders the unparseable value. It now uses the property's
  `data_updated_at` timestamp with `device_class: timestamp`.

### Changed
- Entity names migrated to Home Assistant translation keys
  (`_attr_translation_key`) for sensors and buttons, with matching
  `strings.json` + `en.json`/`fr.json`/`cs.json` entries (HA best practice).
  Entity ids (unique_ids) are unchanged, so existing dashboards/automations
  keep working. Credit: `kasiom` (#7).

### Notes
- Issue #10 (counter sensors frozen on the PrimaDonna Soul) is a separate,
  cloud-sync limitation - not addressed by the JSON parsing above. The Ayla
  property `value` only refreshes when the machine pushes a datapoint (during a
  session sync); the Soul deliberately does not hold a cloud session while
  idle, so an idle-polled counter can stay frozen. Under investigation.

## [0.3.12] - 2026-06-07

### Added
- **Cloud session management for ECAM models** (PR #6 by @TischenkoArseny, following the DlghIoT `connect()` logic). Before commands on Eletta-style models, the integration registers a cloud app session by writing `timestamp + app_id` to `app_device_connected`. This targets the deep-standby problem in #1 (machine stops reacting to cloud commands until the official app "nudges" it). **Eletta-only** (`uses_cloud_session` profile flag): the PrimaDonna Soul path is byte-for-byte unchanged. Command frames are NOT modified - learned replay stays verbatim; the session id (`0xC0FFEE11`, DlghIoT convention) is used only for the session property write. Cold connect runs in a background task (POST + 4 s settle) so button presses return immediately; a warm session (4 min cache) sends directly.

### Changed (maintainer hardening on top of PR #6)
- Commands pressed while a cold connect is in progress are now **queued** behind the connect lock instead of dropped.
- Adopting the official app's session id is now **transient**: the integration never refreshes a foreign session in the background, and reverts to its own id as soon as the machine reports the session released.
- Tests for the session helpers (`normalize_signed_app_id`, `integration_app_id_to_bytes`, profile gating).

## [0.3.11] - 2026-06-07

### Added
- **"Machine Status" sensor** - the machine's operational state (standby, waking_up, ready, rinsing, dispensing_hot_water, ...) decoded from the `d302_monitor_machine` monitor blob it already publishes, with progress/action/accessory as attributes. Contributed by @TischenkoArseny (cherry-picked from PR #5), derived from the DlghIoT client by Matthieu Guerquin-Kern (framagit.org/mattgk/dlghiot). Parsing is defensive: a blob that doesn't decode on a given model yields an unknown state with the parse error as attribute, and can never break the data update.

### Changed
- `start/stop` service handlers and buttons now use the `ACTION_START`/`ACTION_STOP` constants; the raw-command service goes through a proper `coordinator.async_send_raw` (also from PR #5).

## [0.3.10] - 2026-06-07

### Added
- **Standby button - power the machine off remotely.** The power family (`0x84 0x0f`) has a standby payload (params `01 01`, CRC `0x0041`) first reported on an Eletta Explore by @TischenkoArseny (#1) and **validated live on the reference PrimaDonna Soul** (the machine powers off exactly as with the physical button). The official app exposes no power-off control, so the frame is always synthesized. On learn-and-replay models (Eletta) the per-device signature is appended from any already-learned frame (e.g. the wake frame); until one is learned, a best-effort unsigned frame is sent with a clear log message.

## [0.3.9] - 2026-06-07

### Fixed
- **Wake learning can no longer be overwritten by session-refresh packets.** The official app emits `0x84 0x0f` frames that are not a power-on (e.g. params `03 02`, seen in issue #1 captures); the sniffer used to learn *any* power-family frame as the wake frame, so such a packet could silently replace the learned power-on frame and break the Wake button. Only frames with the real wake params (`02 01`) are now learned (`is_wake_power_frame` guard), and a non-wake frame persisted by an earlier version is discarded at load with a clear log message asking to power the machine on once from the app to re-learn. Thanks @TischenkoArseny for spotting the overwrite path.

## [0.3.8] - 2026-06-07

### Changed
- **Per-model behaviour extracted into model profiles** (`model_profiles.py`). All model-specific differences (synthesize vs learn-and-replay, command property, beverage/wake command building) now live in one small class per machine family (`SoulProfile`, `ElettaProfile`) instead of `if is_eletta` branches scattered across the coordinator. Adding first-class support for a new model is now a single new class. No behaviour change. Unknown models default to the universal learn-and-replay path. See the README "Adding a new machine model" section.

## [0.3.7] - 2026-06-06

### Added
- **Diagnostic button "Dump Recipe Datapoints"** (read-only). Logs the recipe definitions the machine already reports (`d059_rec_1_*` …) plus the active profile, decoded to hex, between clear `BEGIN`/`END` markers. Sends nothing to the machine. This is the data needed to confirm whether a stored recipe maps to the beverage command's variable recipe block - the path to drop the one-time "trigger the drink from the app" learning step (zero-touch).

## [0.3.6] - 2026-06-06

### Fixed
- **Wake / power-on now works on Eletta Explore.** The synthesized wake frame was being ignored: the official app appends a 4-byte **device signature** after the timestamp (e.g. `00 d3 2f 8c`) that the built frame lacked - which is also why verbatim beverage replay already worked but a synthesized wake did not. The integration now **learns and replays the app's power-on frame** verbatim (fresh timestamp only), exactly like beverages. Power the machine on once from the official app so Home Assistant captures it; the learned wake frame is persisted across restarts. The Soul (`DL-millcore`) wake is unchanged.

## [0.3.5] - 2026-06-06

### Added
- **Learned Eletta frames now persist across Home Assistant restarts.** The per-beverage app frames captured for `oem_model=DL-striker-cb` are saved to disk (HA `Store`, debounced) and restored at setup, so you no longer have to re-trigger every drink from the official app after each restart - the integration teaches itself once and remembers.

## [0.3.4] - 2026-06-06

### Added
- **Eletta Explore (`oem_model=DL-striker-cb`) beverage support via recipe replay.** Captured app frames (issue #1) proved the Eletta beverage frame is *not* the Soul's fixed 13-byte frame: it carries a **variable-length recipe block** (quantity in ml, intensity, milk all encoded inline) terminated by a `01 0a` trailer before the CRC. The CRC itself is unchanged (CRC16/AUG-CCITT) - it validates once the frame is parsed at the right length. The integration learns the exact recipe bytes the official Coffee Link app sends for each beverage (from the existing command sniffer) and **replays** them, so quantity/intensity/milk are reproduced faithfully. New `build_eletta_beverage_command`, gated by model (`is_eletta`); the PrimaDonna Soul path is untouched.

### Fixed
- `decode_command` now parses the beverage frame using its self-describing length byte, so it correctly handles **both** the fixed Soul frame and the variable-length Eletta frame (previously it read Soul-fixed offsets, which made captured Eletta frames show `crc_valid: false` and a truncated `params`). The diagnostic sensor now reports `style` (soul/eletta) and the full `recipe` block, and Eletta frames show `crc_valid: true`.

### Notes
- Until a beverage has been brewed once from the official app (so its bytes can be captured), pressing that beverage in Home Assistant logs a warning and sends a best-effort Soul frame. Reading the machine's stored recipe datapoints to remove this one-time step is the next step.

## [0.3.3] - 2026-06-05

### Fixed
- Command sniffer: Ayla returns string datapoints wrapped in whitespace (a real captured app wake came back as `...\n`). The trailing newline made `base64.b64decode(validate=True)` reject the frame, so the `Last Captured Command` sensor showed only `origin`/`captured_at` with no decoded fields, and could mis-attribute the integration's own echoed command as `app`. Values are now normalised (whitespace stripped) before attribution and decoding.

## [0.3.2] - 2026-06-05

### Added
- **Command sniffer (diagnostic).** The coordinator now watches the binary command channel (`data_request` / `app_data_request`) and the response channel each poll. When a command is written by the **official Coffee Link app** (i.e. one this integration did not send), its exact bytes are captured, decoded, and logged (`CAPTURED app->machine command ...`).
- New diagnostic sensor **Last Captured Command**: its state is the captured base64 frame; attributes decode it (family, beverage, action, recipe params, CRC validity, timestamp) and include **`matches_integration`** - whether the app's structural bytes (payload + CRC, timestamp ignored) equal what this integration would generate. This is the ground-truth needed to debug models where commands return HTTP 200 but the machine stays silent (e.g. Eletta Explore).
- `decode_command` / `summarize_decoded` helpers in `command_builder` (pure, fully unit-tested).

### Notes
- Passive feature: no extra API calls (properties are already polled), and no change to command encoding - safe for the reference PrimaDonna Soul.

## [0.3.1] - 2026-06-03

### Fixed
- Sensors stuck on `unknown` for Eletta Explore (`oem_model=DL-striker-cb`): counter property names now resolve from a per-model candidate list (e.g. `d700_tot_bev_b` on Soul vs `d701_tot_bev_b` on Eletta), same approach as the v0.3.0 command-property detection.
- `Last Connected` now resolves `device_connected` / `app_device_connected` via the candidate list (the previous one-off fallback is removed).

### Changed
- Counter/info sensors whose property is absent on the device are no longer created, instead of appearing permanently `unknown` (e.g. Total Milk Drinks / Total Water / Descale Status on Eletta).
- Counter parsing is more robust (handles int and numeric strings); when a counter value is present but not a plain integer, the raw value and Ayla `base_type` are logged once so unknown encodings can be reported and supported.

## [0.3.0] - 2026-05-21

### Added
- Auto-detection of the binary command property at first refresh (`data_request` on PrimaDonna Soul / `app_data_request` on Eletta Explore), fixing `HTTP 404` on `set_property` for non-Soul models.

## [0.2.0] - 2026-04-22

### Added
- `Wake` button to bring the machine out of standby (cmd family `0x84 0x0f`).

## [0.1.0] - 2026-04-22

Initial release.

### Added
- Cloud authentication chain: Gigya (SAP Customer Data Cloud) login + HMAC-SHA1 signed JWT + Ayla Networks SSO.
- 22 beverage buttons (Espresso, Cappuccino, Latte Macchiato, Hot Water, Tea, etc.) + generic Stop.
- 16 sensors for lifetime counters, descale status, water hardness, connection status, software version.
- Services: `start_beverage`, `stop_beverage`, `send_raw_command` (advanced).
- English + French translations for the config flow.

### Technical
- Reverse-engineered command format: `0x0d <len> <family> <action> <params> <crc16> <unix_ts>`.
- CRC16 AUG-CCITT (poly `0x1021`, init `0x1D0F`) over pre-CRC bytes, big-endian.
- Beverage family: `0x83 0xf0`. Power/wake family: `0x84 0x0f`.
- Tested on PrimaDonna Soul ECAM 612.55.SB.

### Known limitations
- Coffee Link mobile app must be closed for the machine to accept cloud-routed commands (LAN mode takes priority with a 30s keep-alive).
- Default recipe parameters are the captured Hot Water values; some beverages may need per-drink tuned params.
- No power-off command captured yet.
