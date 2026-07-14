#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define VIBE_STICK_AUDIO_SAMPLE_RATE 16000
#define VIBE_STICK_AUDIO_CHANNELS 1
#define VIBE_STICK_AUDIO_BITS_PER_SAMPLE 16

typedef enum {
    VIBE_STICK_SOUND_DONE,
    VIBE_STICK_SOUND_ERROR,
    VIBE_STICK_SOUND_APPROVAL,
} agent_sound_t;

esp_err_t vibe_audio_init(void);
esp_err_t vibe_audio_start(void);
esp_err_t vibe_audio_stop(void);
esp_err_t vibe_audio_shutdown(void);
esp_err_t vibe_audio_play_sound(agent_sound_t sound);
bool vibe_audio_is_recording(void);
const uint8_t *vibe_audio_data(size_t *len);
esp_err_t vibe_audio_chunk(size_t offset, size_t max_len,
                           const uint8_t **data, size_t *len);
void vibe_audio_clear(void);
