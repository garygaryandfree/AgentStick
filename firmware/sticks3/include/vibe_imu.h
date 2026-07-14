#pragma once

#include <stdbool.h>

#include "esp_err.h"

esp_err_t vibe_imu_init(void);
bool vibe_imu_is_ready(void);
esp_err_t vibe_imu_read_acceleration(float *x_g, float *y_g, float *z_g);
esp_err_t vibe_imu_shutdown(void);
