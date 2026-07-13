#pragma once

#include <stdbool.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

esp_err_t vibe_board_init_power(void);
i2c_master_bus_handle_t vibe_board_i2c_bus(void);
esp_err_t vibe_board_battery_level(int *level_percent);
esp_err_t vibe_board_battery_charging(bool *charging);
esp_err_t vibe_board_usb_powered(bool *usb_powered);
esp_err_t vibe_board_speaker_set_enabled(bool enabled);
esp_err_t vibe_board_shutdown(void);
