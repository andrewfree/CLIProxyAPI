package handlers

import (
	"net/http"
	"reflect"
	"testing"
	"time"
)

func TestFilterClientRetryAfterHeaders(t *testing.T) {
	now := time.Date(2026, 11, 2, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name   string
		values []string
		want   string
	}{
		{"fraction rounded internally", []string{"1"}, "1"},
		{"minute", []string{"60"}, "60"},
		{"over minute", []string{"61"}, ""},
		{"multi day", []string{"384882"}, ""},
		{"zero", []string{"0"}, ""},
		{"negative", []string{"-1"}, ""},
		{"overflow", []string{"18446744073709551616"}, ""},
		{"invalid", []string{"later"}, ""},
		{"multiple", []string{"1", "256"}, ""},
		{"comma list", []string{"1, 256"}, ""},
		{"short date", []string{now.Add(time.Minute).Format(http.TimeFormat)}, now.Add(time.Minute).Format(http.TimeFormat)},
		{"long date", []string{now.Add(61 * time.Second).Format(http.TimeFormat)}, ""},
		{"past date", []string{now.Add(-time.Second).Format(http.TimeFormat)}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := http.Header{"retry-after": tc.values, "X-Request-Id": {"req-policy"}}
			FilterClientRetryAfterHeaders(h, now)
			if got := h.Get("Retry-After"); got != tc.want {
				t.Errorf("Retry-After = %q, want %q", got, tc.want)
			}
			if h.Get("X-Request-Id") != "req-policy" {
				t.Fatal("unrelated header changed")
			}
			before := h.Clone()
			FilterClientRetryAfterHeaders(h, now)
			if !reflect.DeepEqual(before, h) {
				t.Fatal("filter is not idempotent")
			}
		})
	}
	h := http.Header{"Retry-After": {"30"}, "retry-after": {"256"}}
	FilterClientRetryAfterHeaders(h, now)
	if len(h) != 0 {
		t.Fatalf("ambiguous casing survived: %v", h)
	}
	FilterClientRetryAfterHeaders(nil, now)
}
