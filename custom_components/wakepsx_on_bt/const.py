"""Constants for the Wake PSX on Bluetooth integration."""

DOMAIN: str = "wakepsx_on_bt"

MANUFACTURER_SONY: str = "Sony"

# ESPHome project identifier used to filter valid devices in the registry.
ESPHOME_PROJECT_NAME: str = "esp-wakepsx-on-bt"

ESPHOME_DOMAIN: str = "esphome"
ESPHOME_WAKE_SERVICE: str = "wake_psx"
STATUS_SENSOR_SUFFIX: str = "psx_wake_status"

CONF_CONSOLE_NAME: str = "console_name"
CONF_CONTROLLER_TYPE: str = "controller_type"
CONF_DSX_MAC: str = "dsx_mac"
CONF_ESP_ENTITY: str = "wakepsx_on_bt_entity"
CONF_PSX_MAC: str = "psx_mac"
CONF_WAKE_METHOD: str = "wake_method"
CONF_BT_ADAPTER: str = "bt_adapter"

WAKE_METHOD_ESPHOME: str = "esphome_psx_waker"
WAKE_METHOD_DONGLE: str = "bluetooth_dongle"
DEFAULT_BT_ADAPTER: str = "hci0"

# CSR dongle write mode (used when the adapter strategy is CsrSpoofStrategy).
CONF_CSR_WRITE_MODE: str = "csr_write_mode"

# Cached at setup time — avoid repeated HCI/registry lookups at press time.
CONF_BT_STRATEGY: str = "bt_strategy"  # e.g. "CypressSpoofStrategy"
CONF_ESP_SERVICE: str = "esp_service_name"  # e.g. "psx_wakebt_wake_psx"
CSR_WRITE_MODE_TRANSIENT: str = "transient"
CSR_WRITE_MODE_PERSISTENT: str = "persistent"
DEFAULT_CSR_WRITE_MODE: str = "transient"

CONSOLE_NAME_PS3: str = "PlayStation 3"
CONSOLE_NAME_PS4: str = "PlayStation 4"
CONSOLE_NAME_PS5: str = "PlayStation 5"
DEFAULT_CONSOLE_NAME: str = "PlayStation"
DEFAULT_CONTROLLER_NAME: str = "PlayStation Controller"
