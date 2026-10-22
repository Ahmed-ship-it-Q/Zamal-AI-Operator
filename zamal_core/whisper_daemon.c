/*
 * whisper_daemon.c — loads the ggml model once, transcribes on demand via
 * stdin/stdout JSON lines.  Protocol:
 *   request:  {"wav":"/abs/path.wav","offset_ms":0,"duration_ms":0}\n
 *   response: {"text":"..."}\n   or   {"error":"..."}\n
 *
 * Stderr emits "ready\n" once the model is loaded and the daemon is
 * accepting requests.  Compile with build_whisper_daemon.sh.
 */
#include "whisper.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── WAV reader ─────────────────────────────────────────────────────────── */

static float *read_wav_pcm(const char *path, int *out_n) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;

    char hdr[12];
    if (fread(hdr, 1, 12, f) != 12 ||
        memcmp(hdr, "RIFF", 4) || memcmp(hdr + 8, "WAVE", 4)) {
        fclose(f); return NULL;
    }

    long data_offset = -1;
    uint32_t data_size = 0;
    char id[4]; uint32_t sz;
    while (fread(id, 1, 4, f) == 4 && fread(&sz, 1, 4, f) == 4) {
        if (!memcmp(id, "data", 4)) { data_offset = ftell(f); data_size = sz; break; }
        fseek(f, (long)sz, SEEK_CUR);
    }
    if (data_offset < 0) { fclose(f); return NULL; }

    fseek(f, data_offset, SEEK_SET);
    int n = (int)(data_size / 2);
    int16_t *pcm16 = (int16_t *)malloc(data_size);
    if (!pcm16) { fclose(f); return NULL; }
    if ((int)fread(pcm16, 2, n, f) != n) { free(pcm16); fclose(f); return NULL; }
    fclose(f);

    float *pcm32 = (float *)malloc(n * sizeof(float));
    if (!pcm32) { free(pcm16); return NULL; }
    for (int i = 0; i < n; i++) pcm32[i] = pcm16[i] / 32768.0f;
    free(pcm16);
    *out_n = n;
    return pcm32;
}

/* ── Minimal JSON helpers (controlled internal protocol only) ───────────── */

static int json_str(const char *json, const char *key, char *out, int outlen) {
    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\":", key);
    const char *p = strstr(json, needle);
    if (!p) return 0;
    p += strlen(needle);
    while (*p == ' ') p++;
    if (*p != '"') return 0;
    p++;
    int i = 0;
    while (*p && *p != '"' && i < outlen - 1) out[i++] = *p++;
    out[i] = '\0';
    return 1;
}

static int json_int(const char *json, const char *key, int def) {
    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\":", key);
    const char *p = strstr(json, needle);
    if (!p) return def;
    p += strlen(needle);
    while (*p == ' ') p++;
    return atoi(p);
}

static void json_escape(const char *s, FILE *out) {
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"')       fputs("\\\"", out);
        else if (c == '\\') fputs("\\\\", out);
        else if (c == '\n') fputs("\\n",  out);
        else if (c == '\r') fputs("\\r",  out);
        else if (c < 0x20)  fprintf(out, "\\u%04x", c);
        else                fputc(c, out);
    }
}

/* ── Main ───────────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: whisper_daemon <model.bin>\n");
        return 1;
    }

    struct whisper_context_params cparams = whisper_context_default_params();
    cparams.use_gpu = false;

    struct whisper_context *ctx =
        whisper_init_from_file_with_params(argv[1], cparams);
    if (!ctx) {
        fprintf(stderr, "failed to load model: %s\n", argv[1]);
        return 1;
    }

    fprintf(stderr, "ready\n");
    fflush(stderr);

    char line[4096];
    while (fgets(line, sizeof(line), stdin)) {
        char wav[2048] = {0};
        if (!json_str(line, "wav", wav, sizeof(wav))) {
            printf("{\"error\":\"bad_request\"}\n");
            fflush(stdout);
            continue;
        }
        int offset_ms   = json_int(line, "offset_ms",   0);
        int duration_ms = json_int(line, "duration_ms",  0);

        int n_samples;
        float *pcm = read_wav_pcm(wav, &n_samples);
        if (!pcm) {
            printf("{\"error\":\"read_failed\"}\n");
            fflush(stdout);
            continue;
        }

        struct whisper_full_params wparams =
            whisper_full_default_params(WHISPER_SAMPLING_BEAM_SEARCH);
        wparams.language              = "auto";
        wparams.offset_ms             = offset_ms;
        wparams.duration_ms           = duration_ms;
        wparams.print_realtime        = false;
        wparams.print_progress        = false;
        wparams.print_timestamps      = false;
        wparams.single_segment        = false;
        wparams.no_context            = true;  /* each request is independent */
        wparams.n_threads             = 4;
        wparams.beam_search.beam_size = 5;
        wparams.greedy.best_of        = 5;

        if (whisper_full(ctx, wparams, pcm, n_samples) != 0) {
            free(pcm);
            printf("{\"error\":\"transcription_failed\"}\n");
            fflush(stdout);
            continue;
        }
        free(pcm);

        printf("{\"text\":\"");
        int n_seg = whisper_full_n_segments(ctx);
        for (int i = 0; i < n_seg; i++)
            json_escape(whisper_full_get_segment_text(ctx, i), stdout);
        printf("\"}\n");
        fflush(stdout);
    }

    whisper_free(ctx);
    return 0;
}
