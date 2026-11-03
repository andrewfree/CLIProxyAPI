package auth

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"
)

func TestModelCooldownRetryAfter(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name         string
		wait         time.Duration
		retryAfter   string
		resetSeconds int
	}{
		{"negative", -time.Second, "0", 0},
		{"zero", 0, "0", 0},
		{"fractional", time.Millisecond, "1", 1},
		{"one minute", time.Minute, "60", 60},
		{"over one minute", time.Minute + time.Millisecond, "61", 61},
		{"multi day quota", 384882 * time.Second, "384882", 384882},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := newModelCooldownError("gpt-6-astra", "codex", tc.wait)
			if got := err.Headers().Get("Retry-After"); got != tc.retryAfter {
				t.Errorf("Retry-After = %q, want %q", got, tc.retryAfter)
			}
			if got := err.StatusCode(); got != http.StatusTooManyRequests {
				t.Errorf("status = %d, want 429", got)
			}
			var payload struct {
				Error struct {
					Code         string `json:"code"`
					ResetSeconds int    `json:"reset_seconds"`
				} `json:"error"`
			}
			if errDecode := json.Unmarshal([]byte(err.Error()), &payload); errDecode != nil {
				t.Fatal(errDecode)
			}
			if payload.Error.Code != "model_cooldown" || payload.Error.ResetSeconds != tc.resetSeconds {
				t.Errorf("cooldown body lost its reason or deadline: %+v", payload.Error)
			}
		})
	}
}
