#include "vibe_audio.h"

#include <math.h>
#include <stdatomic.h>
#include <string.h>

#include "vibe_board.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define PIN_ES8311_MCLK 18
#define PIN_ES8311_BCLK 17
#define PIN_ES8311_LRCK 15
#define PIN_ES8311_DIN 14
#define PIN_ES8311_DOUT 16

#define AUDIO_FRAME_MS 60
#define AUDIO_FRAME_SAMPLES ((VIBE_STICK_AUDIO_SAMPLE_RATE * AUDIO_FRAME_MS) / 1000)
#define AUDIO_MAX_SECONDS 120
#define AUDIO_MAX_BYTES (VIBE_STICK_AUDIO_SAMPLE_RATE * VIBE_STICK_AUDIO_CHANNELS * \
                         (VIBE_STICK_AUDIO_BITS_PER_SAMPLE / 8) * AUDIO_MAX_SECONDS)
#define TASK_EXIT_WAIT_MS 800
#define VIBE_STICK_SOUND_VOLUME 0.40f
#define VIBE_STICK_SOUND_FRAME_SAMPLES 160
#define VIBE_STICK_SOUND_FADE_MS 8
#define VIBE_STICK_SOUND_OUTPUT_VOLUME 85
#define VIBE_STICK_TWO_PI 6.28318530717958647692f

static const char *TAG = "vibe_audio";

static atomic_bool s_running;
static bool s_initialized;
static SemaphoreHandle_t s_audio_mutex;
static TaskHandle_t s_audio_task;
static i2s_chan_handle_t s_tx_handle;
static i2s_chan_handle_t s_rx_handle;
static bool s_tx_enabled;
static bool s_rx_enabled;
static esp_codec_dev_handle_t s_codec;
static const audio_codec_ctrl_if_t *s_ctrl_if;
static const audio_codec_data_if_t *s_data_if;
static const audio_codec_gpio_if_t *s_gpio_if;
static const audio_codec_if_t *s_codec_if;
static uint8_t *s_audio_buffer;
static atomic_size_t s_audio_len;
static size_t s_audio_capacity;

static esp_err_t init_i2s(bool enable_tx, bool enable_rx)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg,
                                        enable_tx ? &s_tx_handle : NULL,
                                        enable_rx ? &s_rx_handle : NULL),
                        TAG, "create i2s");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(VIBE_STICK_AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = PIN_ES8311_MCLK,
            .bclk = PIN_ES8311_BCLK,
            .ws = PIN_ES8311_LRCK,
            .dout = PIN_ES8311_DIN,
            .din = PIN_ES8311_DOUT,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    if (s_tx_handle) {
        ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_tx_handle, &std_cfg), TAG, "init i2s tx");
        ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx_handle), TAG, "enable i2s tx");
        s_tx_enabled = true;
    }
    if (s_rx_handle) {
        ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx_handle, &std_cfg), TAG, "init i2s rx");
        ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_handle), TAG, "enable i2s rx");
        s_rx_enabled = true;
    }
    return ESP_OK;
}

static esp_err_t init_codec(esp_codec_dev_type_t dev_type, esp_codec_dec_work_mode_t work_mode)
{
    i2c_master_bus_handle_t i2c_bus = vibe_board_i2c_bus();
    ESP_RETURN_ON_FALSE(i2c_bus != NULL, ESP_ERR_INVALID_STATE, TAG, "i2c unavailable");

    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = I2C_NUM_1,
        .addr = ES8311_CODEC_DEFAULT_ADDR,
        .bus_handle = i2c_bus,
    };
    s_ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
    ESP_RETURN_ON_FALSE(s_ctrl_if != NULL, ESP_ERR_NO_MEM, TAG, "codec i2c");

    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_1,
        .rx_handle = s_rx_handle,
        .tx_handle = s_tx_handle,
    };
    s_data_if = audio_codec_new_i2s_data(&i2s_cfg);
    ESP_RETURN_ON_FALSE(s_data_if != NULL, ESP_ERR_NO_MEM, TAG, "codec i2s");

    s_gpio_if = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(s_gpio_if != NULL, ESP_ERR_NO_MEM, TAG, "codec gpio");

    es8311_codec_cfg_t es8311_cfg = {
        .ctrl_if = s_ctrl_if,
        .gpio_if = s_gpio_if,
        .codec_mode = work_mode,
        .pa_pin = -1,
        .pa_reverted = false,
        .master_mode = false,
        .use_mclk = true,
        .digital_mic = false,
        .invert_mclk = false,
        .invert_sclk = false,
        .hw_gain = {
            .pa_voltage = 5.0,
            .codec_dac_voltage = 3.3,
        },
    };
    s_codec_if = es8311_codec_new(&es8311_cfg);
    ESP_RETURN_ON_FALSE(s_codec_if != NULL, ESP_ERR_NO_MEM, TAG, "es8311");

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = dev_type,
        .codec_if = s_codec_if,
        .data_if = s_data_if,
    };
    s_codec = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(s_codec != NULL, ESP_ERR_NO_MEM, TAG, "codec dev");

    esp_codec_dev_sample_info_t sample_cfg = {
        .bits_per_sample = I2S_DATA_BIT_WIDTH_16BIT,
        .channel = VIBE_STICK_AUDIO_CHANNELS,
        .channel_mask = I2S_STD_SLOT_LEFT,
        .sample_rate = VIBE_STICK_AUDIO_SAMPLE_RATE,
        .mclk_multiple = 0,
    };
    ESP_RETURN_ON_FALSE(esp_codec_dev_open(s_codec, &sample_cfg) == ESP_CODEC_DEV_OK,
                        ESP_FAIL, TAG, "open codec");
    if (dev_type & ESP_CODEC_DEV_TYPE_IN) {
        ESP_RETURN_ON_FALSE(esp_codec_dev_set_in_gain(s_codec, 36.0) == ESP_CODEC_DEV_OK,
                            ESP_FAIL, TAG, "mic gain");
    }
    if (dev_type & ESP_CODEC_DEV_TYPE_OUT) {
        ESP_RETURN_ON_FALSE(esp_codec_dev_set_out_vol(s_codec, VIBE_STICK_SOUND_OUTPUT_VOLUME) == ESP_CODEC_DEV_OK,
                            ESP_FAIL, TAG, "speaker volume");
        ESP_RETURN_ON_FALSE(esp_codec_dev_set_out_mute(s_codec, false) == ESP_CODEC_DEV_OK,
                            ESP_FAIL, TAG, "speaker unmute");
    }
    return ESP_OK;
}

static void deinit_codec(void)
{
    if (s_codec) {
        esp_codec_dev_close(s_codec);
        esp_codec_dev_delete(s_codec);
        s_codec = NULL;
        s_tx_enabled = false;
        s_rx_enabled = false;
    }
    if (s_codec_if) {
        audio_codec_delete_codec_if(s_codec_if);
        s_codec_if = NULL;
    }
    if (s_data_if) {
        audio_codec_delete_data_if(s_data_if);
        s_data_if = NULL;
    }
    if (s_gpio_if) {
        audio_codec_delete_gpio_if(s_gpio_if);
        s_gpio_if = NULL;
    }
    if (s_ctrl_if) {
        audio_codec_delete_ctrl_if(s_ctrl_if);
        s_ctrl_if = NULL;
    }
}

static void deinit_i2s(void)
{
    if (s_tx_handle) {
        if (s_tx_enabled) {
            esp_err_t err = i2s_channel_disable(s_tx_handle);
            if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(TAG, "disable i2s tx failed: %s", esp_err_to_name(err));
            }
        }
        i2s_del_channel(s_tx_handle);
        s_tx_handle = NULL;
        s_tx_enabled = false;
    }
    if (s_rx_handle) {
        if (s_rx_enabled) {
            esp_err_t err = i2s_channel_disable(s_rx_handle);
            if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(TAG, "disable i2s rx failed: %s", esp_err_to_name(err));
            }
        }
        i2s_del_channel(s_rx_handle);
        s_rx_handle = NULL;
        s_rx_enabled = false;
    }
}

static void release_session_resources(void)
{
    deinit_codec();
    deinit_i2s();
}

typedef struct {
    int freq_hz;
    int duration_ms;
} sound_segment_t;

static float sound_envelope(int sample_index, int total_samples)
{
    const int fade_samples = (VIBE_STICK_AUDIO_SAMPLE_RATE * VIBE_STICK_SOUND_FADE_MS) / 1000;
    if (fade_samples <= 0) {
        return 1.0f;
    }
    if (sample_index < fade_samples) {
        return (float)sample_index / (float)fade_samples;
    }
    int remaining = total_samples - sample_index - 1;
    if (remaining < fade_samples) {
        return (float)remaining / (float)fade_samples;
    }
    return 1.0f;
}

static esp_err_t write_sound_segment(const sound_segment_t *segment)
{
    const int total_samples = (VIBE_STICK_AUDIO_SAMPLE_RATE * segment->duration_ms) / 1000;
    int samples_written = 0;
    int16_t frame[VIBE_STICK_SOUND_FRAME_SAMPLES];

    while (samples_written < total_samples) {
        int frame_samples = total_samples - samples_written;
        if (frame_samples > VIBE_STICK_SOUND_FRAME_SAMPLES) {
            frame_samples = VIBE_STICK_SOUND_FRAME_SAMPLES;
        }

        for (int i = 0; i < frame_samples; ++i) {
            int sample_index = samples_written + i;
            if (segment->freq_hz <= 0) {
                frame[i] = 0;
                continue;
            }
            float phase = VIBE_STICK_TWO_PI * (float)segment->freq_hz *
                          (float)sample_index / (float)VIBE_STICK_AUDIO_SAMPLE_RATE;
            float value = sinf(phase) * sound_envelope(sample_index, total_samples) *
                          VIBE_STICK_SOUND_VOLUME * 32767.0f;
            frame[i] = (int16_t)value;
        }

        int bytes = frame_samples * (int)sizeof(frame[0]);
        ESP_RETURN_ON_FALSE(esp_codec_dev_write(s_codec, frame, bytes) == ESP_CODEC_DEV_OK,
                            ESP_FAIL, TAG, "speaker write");
        samples_written += frame_samples;
    }
    return ESP_OK;
}

static esp_err_t play_sound_segments(const sound_segment_t *segments, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        ESP_RETURN_ON_ERROR(write_sound_segment(&segments[i]), TAG, "sound segment");
    }
    sound_segment_t tail = {.freq_hz = 0, .duration_ms = 20};
    return write_sound_segment(&tail);
}

static const sound_segment_t *sound_segments_for(agent_sound_t sound, size_t *count)
{
    static const sound_segment_t done[] = {
        {.freq_hz = 880, .duration_ms = 80},
        {.freq_hz = 0, .duration_ms = 40},
        {.freq_hz = 1320, .duration_ms = 120},
    };
    static const sound_segment_t error[] = {
        {.freq_hz = 240, .duration_ms = 100},
        {.freq_hz = 0, .duration_ms = 60},
        {.freq_hz = 240, .duration_ms = 100},
        {.freq_hz = 0, .duration_ms = 60},
        {.freq_hz = 240, .duration_ms = 100},
    };
    static const sound_segment_t approval[] = {
        {.freq_hz = 600, .duration_ms = 100},
        {.freq_hz = 0, .duration_ms = 60},
        {.freq_hz = 800, .duration_ms = 100},
    };

    switch (sound) {
    case VIBE_STICK_SOUND_DONE:
        *count = sizeof(done) / sizeof(done[0]);
        return done;
    case VIBE_STICK_SOUND_ERROR:
        *count = sizeof(error) / sizeof(error[0]);
        return error;
    case VIBE_STICK_SOUND_APPROVAL:
        *count = sizeof(approval) / sizeof(approval[0]);
        return approval;
    default:
        *count = 0;
        return NULL;
    }
}

static void audio_task(void *arg)
{
    (void)arg;
    int16_t frame[AUDIO_FRAME_SAMPLES];
    size_t dropped = 0;

    while (atomic_load(&s_running)) {
        esp_err_t err = esp_codec_dev_read(s_codec, frame, sizeof(frame));
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "codec read failed: %s", esp_err_to_name(err));
            continue;
        }
        size_t audio_len = atomic_load_explicit(&s_audio_len, memory_order_acquire);
        if (audio_len + sizeof(frame) <= s_audio_capacity) {
            memcpy(s_audio_buffer + audio_len, frame, sizeof(frame));
            atomic_store_explicit(&s_audio_len, audio_len + sizeof(frame), memory_order_release);
        } else {
            dropped += sizeof(frame);
        }
    }

    ESP_LOGI(TAG, "recorded %u bytes dropped=%u",
             (unsigned)atomic_load(&s_audio_len), (unsigned)dropped);
    release_session_resources();
    s_audio_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t vibe_audio_init(void)
{
    if (!s_audio_mutex) {
        s_audio_mutex = xSemaphoreCreateMutex();
        ESP_RETURN_ON_FALSE(s_audio_mutex != NULL, ESP_ERR_NO_MEM, TAG, "audio mutex");
    }
    s_initialized = true;
    return ESP_OK;
}

esp_err_t vibe_audio_start(void)
{
    ESP_RETURN_ON_FALSE(s_initialized, ESP_ERR_INVALID_STATE, TAG, "not initialized");
    if (atomic_load(&s_running)) {
        return ESP_OK;
    }
    ESP_RETURN_ON_FALSE(s_audio_mutex != NULL, ESP_ERR_INVALID_STATE, TAG, "audio mutex missing");
    ESP_RETURN_ON_FALSE(xSemaphoreTake(s_audio_mutex, pdMS_TO_TICKS(250)) == pdTRUE,
                        ESP_ERR_TIMEOUT, TAG, "audio busy");
    if (atomic_load(&s_running) || s_audio_task != NULL || s_codec != NULL || s_tx_handle != NULL || s_rx_handle != NULL) {
        xSemaphoreGive(s_audio_mutex);
        return ESP_ERR_INVALID_STATE;
    }

    vibe_audio_clear();
    s_audio_capacity = AUDIO_MAX_BYTES;
    s_audio_buffer = heap_caps_malloc(s_audio_capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_audio_buffer) {
        s_audio_buffer = heap_caps_malloc(s_audio_capacity, MALLOC_CAP_8BIT);
    }
    if (!s_audio_buffer) {
        xSemaphoreGive(s_audio_mutex);
        ESP_RETURN_ON_FALSE(false, ESP_ERR_NO_MEM, TAG, "audio buffer");
    }
    atomic_store(&s_audio_len, 0);

    esp_err_t err = init_i2s(false, true);
    if (err != ESP_OK) {
        release_session_resources();
        xSemaphoreGive(s_audio_mutex);
        return err;
    }
    err = init_codec(ESP_CODEC_DEV_TYPE_IN, ESP_CODEC_DEV_WORK_MODE_ADC);
    if (err != ESP_OK) {
        release_session_resources();
        vibe_audio_clear();
        xSemaphoreGive(s_audio_mutex);
        return err;
    }

    atomic_store(&s_running, true);
    BaseType_t ok = xTaskCreatePinnedToCore(audio_task, "vibe_audio", 32768, NULL, 5, &s_audio_task, 1);
    if (ok != pdPASS) {
        atomic_store(&s_running, false);
        release_session_resources();
        vibe_audio_clear();
        xSemaphoreGive(s_audio_mutex);
        return ESP_ERR_NO_MEM;
    }
    xSemaphoreGive(s_audio_mutex);
    ESP_LOGI(TAG, "recording started");
    return ESP_OK;
}

esp_err_t vibe_audio_stop(void)
{
    if (!atomic_load(&s_running)) {
        return ESP_OK;
    }
    atomic_store(&s_running, false);
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(TASK_EXIT_WAIT_MS);
    while (s_audio_task != NULL) {
        if (xTaskGetTickCount() >= deadline) {
            ESP_LOGW(TAG, "audio task stop timeout");
            return ESP_ERR_TIMEOUT;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return ESP_OK;
}

esp_err_t vibe_audio_shutdown(void)
{
    esp_err_t stop_err = vibe_audio_stop();
    if (s_audio_mutex && xSemaphoreTake(s_audio_mutex, pdMS_TO_TICKS(250)) == pdTRUE) {
        release_session_resources();
        vibe_audio_clear();
        s_initialized = false;
        xSemaphoreGive(s_audio_mutex);
    }
    ESP_ERROR_CHECK_WITHOUT_ABORT(vibe_board_speaker_set_enabled(false));
    return stop_err;
}

bool vibe_audio_is_recording(void)
{
    return atomic_load(&s_running) || s_audio_task != NULL;
}

esp_err_t vibe_audio_play_sound(agent_sound_t sound)
{
    ESP_RETURN_ON_FALSE(s_initialized, ESP_ERR_INVALID_STATE, TAG, "not initialized");
    ESP_RETURN_ON_FALSE(s_audio_mutex != NULL, ESP_ERR_INVALID_STATE, TAG, "audio mutex missing");
    if (vibe_audio_is_recording()) {
        return ESP_ERR_INVALID_STATE;
    }

    if (xSemaphoreTake(s_audio_mutex, 0) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    if (vibe_audio_is_recording() || s_codec != NULL || s_tx_handle != NULL || s_rx_handle != NULL) {
        xSemaphoreGive(s_audio_mutex);
        return ESP_ERR_INVALID_STATE;
    }

    size_t segment_count = 0;
    const sound_segment_t *segments = sound_segments_for(sound, &segment_count);
    if (!segments || segment_count == 0) {
        xSemaphoreGive(s_audio_mutex);
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t err = vibe_board_speaker_set_enabled(true);
    if (err == ESP_OK) {
        err = init_i2s(true, false);
    }
    if (err == ESP_OK) {
        err = init_codec(ESP_CODEC_DEV_TYPE_OUT, ESP_CODEC_DEV_WORK_MODE_DAC);
    }
    if (err == ESP_OK) {
        err = play_sound_segments(segments, segment_count);
    }

    release_session_resources();
    ESP_ERROR_CHECK_WITHOUT_ABORT(vibe_board_speaker_set_enabled(false));
    xSemaphoreGive(s_audio_mutex);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "sound playback failed: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "sound played id=%d", (int)sound);
    }
    return err;
}

const uint8_t *vibe_audio_data(size_t *len)
{
    if (len) {
        *len = atomic_load_explicit(&s_audio_len, memory_order_acquire);
    }
    return s_audio_buffer;
}

esp_err_t vibe_audio_chunk(size_t offset, size_t max_len,
                           const uint8_t **data, size_t *len)
{
    ESP_RETURN_ON_FALSE(data != NULL && len != NULL, ESP_ERR_INVALID_ARG, TAG,
                        "null chunk output");
    size_t total = atomic_load_explicit(&s_audio_len, memory_order_acquire);
    if (!s_audio_buffer || offset >= total || max_len == 0) {
        *data = NULL;
        *len = 0;
        return ESP_OK;
    }
    size_t available = total - offset;
    *len = available < max_len ? available : max_len;
    *data = s_audio_buffer + offset;
    return ESP_OK;
}

void vibe_audio_clear(void)
{
    if (s_audio_buffer) {
        heap_caps_free(s_audio_buffer);
        s_audio_buffer = NULL;
    }
    atomic_store(&s_audio_len, 0);
    s_audio_capacity = 0;
}
