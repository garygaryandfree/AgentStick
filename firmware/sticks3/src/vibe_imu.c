#include "vibe_imu.h"

#include "bmi270.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "vibe_board.h"

#define BMI270_REG_PWR_CTRL 0x7D
#define BMI270_PWR_CTRL_ACCEL_ONLY BIT(2)

static const char *TAG = "vibe_imu";
static bmi270_handle_t *s_imu;

static esp_err_t create_at_address(uint8_t address)
{
    bmi270_driver_config_t driver_config = {
        .addr = address,
        .interface = BMI270_USE_I2C,
        .i2c_bus = vibe_board_i2c_bus(),
    };
    return bmi270_create(&driver_config, &s_imu);
}

esp_err_t vibe_imu_init(void)
{
    if (s_imu) {
        return ESP_OK;
    }

    ESP_RETURN_ON_FALSE(vibe_board_i2c_bus() != NULL, ESP_ERR_INVALID_STATE,
                        TAG, "board I2C bus is not ready");

    esp_err_t err = create_at_address(BMI270_I2C_ADDRESS_L);
    if (err != ESP_OK) {
        s_imu = NULL;
        err = create_at_address(BMI270_I2C_ADDRESS_H);
    }
    ESP_RETURN_ON_ERROR(err, TAG, "BMI270 not found");

    const bmi270_config_t config = {
        .acce_odr = BMI270_ACC_ODR_25_HZ,
        .acce_range = BMI270_ACC_RANGE_2_G,
        .gyro_odr = BMI270_GYR_ODR_25_HZ,
        .gyro_range = BMI270_GYR_RANGE_250_DPS,
    };
    err = bmi270_start(s_imu, &config);
    if (err != ESP_OK) {
        bmi270_delete(s_imu);
        s_imu = NULL;
        return err;
    }

    // The public driver starts accelerometer, gyroscope and temperature
    // together. Face-down detection only needs acceleration, so turn the
    // other two blocks back off to reduce active battery use.
    const uint8_t power_ctrl[] = {
        BMI270_REG_PWR_CTRL,
        BMI270_PWR_CTRL_ACCEL_ONLY,
    };
    err = i2c_master_transmit(s_imu->i2c_handle, power_ctrl,
                              sizeof(power_ctrl), 100);
    if (err != ESP_OK) {
        bmi270_stop(s_imu);
        bmi270_delete(s_imu);
        s_imu = NULL;
        return err;
    }

    ESP_LOGI(TAG, "BMI270 ready in accelerometer-only mode");
    return ESP_OK;
}

bool vibe_imu_is_ready(void)
{
    return s_imu != NULL;
}

esp_err_t vibe_imu_read_acceleration(float *x_g, float *y_g, float *z_g)
{
    ESP_RETURN_ON_FALSE(s_imu != NULL, ESP_ERR_INVALID_STATE, TAG,
                        "BMI270 is not ready");
    return bmi270_get_acce_data(s_imu, x_g, y_g, z_g);
}

esp_err_t vibe_imu_shutdown(void)
{
    if (!s_imu) {
        return ESP_OK;
    }

    esp_err_t result = bmi270_stop(s_imu);
    esp_err_t delete_result = bmi270_delete(s_imu);
    s_imu = NULL;
    if (result != ESP_OK) {
        return result;
    }
    return delete_result;
}
