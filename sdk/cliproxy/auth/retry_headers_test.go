package auth

import (
	"errors"
	"fmt"
	"net/http"
	"testing"
	"time"
)

func TestSafeResponseHeadersBoundClientWaits(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		wait time.Duration
		want string
	}{
		{"negative", -time.Second, ""},
		{"zero", 0, ""},
		{"fractional", time.Millisecond, "1"},
		{"one minute", time.Minute, "60"},
		{"over one minute", time.Minute + time.Nanosecond, ""},
		{"multi day", 384882 * time.Second, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			now := time.Now()
			cause := &Error{Code: "home_cooldown", HTTPStatus: http.StatusTooManyRequests, Retryable: true}
			for name, err := range map[string]error{
				"auth unavailable": newAuthUnavailableError(now.Add(tc.wait), now),
				"Home dispatch":    &homeDispatchRetryAfterError{cause: cause, retryAfter: tc.wait},
				"Home round":       markHomeRetryRoundExhausted(cause, &tc.wait, false),
				"Home concurrency": NewHomeConcurrencyBusyError("busy", tc.wait),
			} {
				t.Run(name, func(t *testing.T) {
					wrapped := fmt.Errorf("outer: %w", err)
					if got := SafeResponseHeaders(wrapped).Get("Retry-After"); got != tc.want {
						t.Errorf("Retry-After = %q, want %q", got, tc.want)
					}
					if tc.wait > 0 {
						var internal interface{ RetryAfter() *time.Duration }
						if errors.As(wrapped, &internal) {
							if got := internal.RetryAfter(); got == nil || *got != tc.wait {
								t.Errorf("internal retry deadline = %v, want %v", got, tc.wait)
							}
						}
						if unavailable, ok := err.(*authUnavailableError); ok {
							if unavailable.retryAfter != tc.wait || unavailable.StatusCode() != http.StatusServiceUnavailable {
								t.Errorf("auth recovery state changed: %+v", unavailable)
							}
						}
					}
				})
			}
		})
	}
}
