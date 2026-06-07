"""Constants for the DeLonghi Coffee Link integration."""
from __future__ import annotations

DOMAIN = "delonghi_coffeelink"
MANUFACTURER = "De'Longhi"

# Extracted from Coffee Link APK v4.9.6
APP_ID = "DLonghiCoffeeIdKit-sQ-id"
APP_SECRET = "DLonghiCoffeeIdKit-HT6b0VNd4y6CSha9ivM5k8navLw"
GIGYA_API_KEY = "3_e5qn7USZK-QtsIso1wCelqUKAK_IVEsYshRIssQ-X-k55haiZXmKWDHDRul2e5Y2"
GIGYA_BASE_URL = "https://accounts.eu1.gigya.com"
AYLA_EU_ADS_URL = "https://ads-eu.aylanetworks.com"
AYLA_EU_USER_URL = "https://user-field-eu.aylanetworks.com"

# Polling
DEFAULT_SCAN_INTERVAL = 30  # seconds

# Persistence of learned Eletta beverage frames (survives HA restarts).
RECIPE_STORE_VERSION = 1
RECIPE_STORE_SAVE_DELAY = 2  # seconds; debounce writes to disk

# Property names vary by model:
# - PrimaDonna Soul (DL-millcore): data_request / data_response / device_connected
# - Eletta Explore (DL-striker-cb): app_data_request / app_data_response / app_device_connected
# Listed in detection priority order.
COMMAND_PROPERTY_CANDIDATES = ["data_request", "app_data_request"]
RESPONSE_PROPERTY_CANDIDATES = ["data_response", "app_data_response"]
CONNECTED_PROPERTY_CANDIDATES = ["device_connected", "app_device_connected"]

# Config
CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# CRC16 AUG-CCITT
CRC_POLY = 0x1021
CRC_INIT = 0x1D0F

# Command structure
CMD_PREFIX = 0x0d       # App -> machine
CMD_RESPONSE_PREFIX = 0xd0  # Machine -> app
CMD_LENGTH = 0x0d       # 13 bytes payload
CMD_FAMILY_BREW = bytes([0x83, 0xf0])  # Brew beverage command family

# Eletta Explore (oem_model=DL-striker-cb) beverage frames carry a variable
# length recipe block terminated by this 2-byte trailer, then the CRC. The Soul
# (DL-millcore) frame has no trailer (fixed 6-byte recipe). See command_builder.
ELETTA_RECIPE_TRAILER = bytes([0x01, 0x0a])
# oem_model prefix of the Eletta Explore family (app_data_request channel).
ELETTA_OEM_PREFIX = "DL-striker"

# Actions
ACTION_START = 0x01
ACTION_STOP = 0x02

# Cloud app session (dlghiot-compatible)
DEFAULT_CLOUD_APP_ID = 0xC0FFEE11
APP_ID_PROPERTY = "app_id"
CONNECT_REFRESH_INTERVAL = 240  # seconds; refresh session before this elapses
CONNECT_POLL_TIMEOUT = 15  # seconds waiting for app_id to update after connect

# Power / Wake command family (0x84 0x0f)
CMD_FAMILY_POWER = bytes([0x84, 0x0f])
POWER_WAKE_PARAMS = bytes([0x02, 0x01])  # wake from standby
POWER_STANDBY_PARAMS = bytes([0x01, 0x01])  # turn off / standby
POWER_REFRESH_PARAMS = bytes([0x03, 0x02])  # refresh app session (NOT wake)

# Machine monitor (d302_monitor_machine)
MONITOR_PROPERTY = "d302_monitor_machine"

MACHINE_STATUS = {
    0: "standby",
    1: "waking_up",
    2: "going_to_sleep",
    4: "descaling",
    5: "preparing_steam",
    6: "recovering",
    7: "ready",
    8: "rinsing",
    10: "preparing_milk",
    11: "dispensing_hot_water",
    12: "cleaning_milk",
    16: "preparing_chocolate",
    17: "preparing_milk_alt",
    29: "unknown",
}

# Default recipe params (from captured hot water command)
# Bytes: temp_flag, reserved, quantity_low, quantity_high?, recipe_type, ???
DEFAULT_RECIPE_PARAMS = bytes([0x0f, 0x00, 0xfa, 0x1b, 0x01, 0x06])

# Beverage definitions: (bev_id, key, display_name, icon)
BEVERAGES = [
    (0x01, "espresso",        "Espresso",         "mdi:coffee"),
    (0x02, "coffee",          "Coffee",           "mdi:coffee"),
    (0x03, "long_coffee",     "Long Coffee",      "mdi:coffee-outline"),
    (0x04, "double_espresso", "Double Espresso",  "mdi:coffee"),
    (0x05, "doppio",          "Doppio+",          "mdi:coffee"),
    (0x06, "americano",       "Americano",        "mdi:coffee"),
    (0x07, "cappuccino",      "Cappuccino",       "mdi:coffee"),
    (0x08, "latte_macchiato", "Latte Macchiato",  "mdi:coffee"),
    (0x09, "caffelatte",      "Caffe Latte",      "mdi:coffee"),
    (0x0a, "flat_white",      "Flat White",       "mdi:coffee"),
    (0x0b, "espresso_macchiato", "Espresso Macchiato", "mdi:coffee"),
    (0x0c, "hot_milk",        "Hot Milk",         "mdi:cup"),
    (0x0d, "cappuccino_doppio", "Cappuccino Doppio+", "mdi:coffee"),
    (0x0f, "cappuccino_reverse", "Cappuccino Reverse", "mdi:coffee"),
    (0x10, "hot_water",       "Hot Water",        "mdi:water"),
    (0x16, "tea",             "Tea",              "mdi:tea"),
    (0x17, "coffee_pot",      "Coffee Pot",       "mdi:coffee-maker"),
    (0x18, "cortado",         "Cortado",          "mdi:coffee"),
    (0x19, "long_black",      "Long Black",       "mdi:coffee"),
    (0x1a, "mug_to_go",       "Mug to Go",        "mdi:coffee-to-go"),
    (0x1b, "brew_over_ice",   "Brew Over Ice",    "mdi:coffee"),
]

# Counter properties to expose as sensors:
#   (candidate_property_names, entity_key, display_name, icon)
# Property names differ between models; the first candidate present on the device
# wins (same approach as COMMAND_PROPERTY_CANDIDATES). A sensor whose property is
# absent on the device is not created (avoids permanently-"unknown" entities).
#   - PrimaDonna Soul (DL-millcore): d700_tot_bev_b, d701_tot_bev_bw, d703_tot_bev_w, d825_descale_status
#   - Eletta Explore (DL-striker-cb): d701_tot_bev_b (no milk/water/descale equivalents exposed)
COUNTER_SENSORS = [
    (["d700_tot_bev_b", "d701_tot_bev_b"], "total_beverages",       "Total Beverages",       "mdi:counter"),
    (["d704_tot_bev_espressi"],            "total_espresso",        "Total Espresso",        "mdi:coffee"),
    (["d701_tot_bev_bw"],                  "total_milk_drinks",     "Total Milk Drinks",     "mdi:cup"),
    (["d703_tot_bev_w"],                   "total_water",           "Total Water",           "mdi:water"),
    (["d710_tot_id7_capp"],                "total_cappuccino",      "Total Cappuccino",      "mdi:coffee"),
    (["d711_id8_lattmacc"],                "total_latte_macchiato", "Total Latte Macchiato", "mdi:coffee"),
    (["d712_id9_cafflatt"],                "total_caffelatte",      "Total Caffe Latte",     "mdi:coffee"),
    (["d715_id12_hotmilk"],                "total_hot_milk",        "Total Hot Milk",        "mdi:cup"),
    (["d718_id16_hotwater"],               "total_hot_water",       "Total Hot Water",       "mdi:water"),
    (["d719_id22_tea"],                    "total_tea",             "Total Tea",             "mdi:tea"),
    (["d720_tot_id23_coffee_pot"],         "total_coffee_pot",      "Total Coffee Pot",      "mdi:coffee-maker"),
    (["d551_cnt_coffee_fondi"],            "grounds_counter",       "Grounds Counter",       "mdi:dots-grid"),
    (["d825_descale_status"],              "descale_status",        "Descale Status",        "mdi:water-pump"),
    (["d556_water_hardness"],              "water_hardness",        "Water Hardness",        "mdi:water-percent"),
]

# Info sensors (not counters, general state):
#   (candidate_property_names, entity_key, display_name, icon)
INFO_SENSORS = [
    (["software_version"],                         "software_version", "Software Version", "mdi:chip"),
    (["device_connected", "app_device_connected"], "last_connected",   "Last Connected",   "mdi:clock-outline"),
]

PLATFORMS = ["sensor", "button"]

# Service names
SERVICE_SEND_RAW_COMMAND = "send_raw_command"
SERVICE_START_BEVERAGE = "start_beverage"
SERVICE_STOP_BEVERAGE = "stop_beverage"
