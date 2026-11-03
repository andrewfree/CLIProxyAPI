package handlers

import (
	"net/http"
	"strconv"
	"strings"
	"time"
)

// FilterClientRetryAfterHeaders limits automatic client waits without changing
// the trusted recovery deadlines used by auth selection and internal retries.
func FilterClientRetryAfterHeaders(headers http.Header, now time.Time) {
	var values []string
	for key, entries := range headers {
		if strings.EqualFold(key, "Retry-After") {
			values = append(values, entries...)
			delete(headers, key)
		}
	}
	if len(values) != 1 {
		return
	}
	value := strings.TrimSpace(values[0])
	if seconds, err := strconv.ParseUint(value, 10, 64); err == nil {
		if seconds > 0 && seconds <= 60 {
			headers.Set("Retry-After", strconv.FormatUint(seconds, 10))
		}
		return
	}
	if deadline, err := http.ParseTime(value); err == nil {
		if wait := deadline.Sub(now); wait > 0 && wait <= time.Minute {
			headers.Set("Retry-After", deadline.UTC().Format(http.TimeFormat))
		}
	}
}
