package auth

import (
	"math"
	"strconv"
	"strings"
	"time"
)

const defaultQuotaBalanceMaxAge = 5 * time.Minute

type quotaBalanceWindow struct {
	RemainingBasisPoints int64
	ResetAt              time.Time
	Duration             time.Duration
}

type quotaBalanceSnapshot struct {
	Known   bool
	Windows []quotaBalanceWindow
}

// quotaBalanceSnapshotFor reads one observation without combining model states
// or borrowing cooldown, OAuth, credit, or Retry-After timestamps. The manager
// remains responsible for model eligibility and credential availability.
func quotaBalanceSnapshotFor(auth *Auth, routeModel string, now time.Time, maxAge time.Duration) quotaBalanceSnapshot {
	if auth == nil || now.IsZero() || auth.Quota.ObservedAt.IsZero() {
		return quotaBalanceSnapshot{}
	}
	if maxAge <= 0 {
		maxAge = defaultQuotaBalanceMaxAge
	}
	observedAt := auth.Quota.ObservedAt
	if observedAt.Unix() <= 0 || observedAt.After(now.Add(30*time.Second)) || now.Sub(observedAt) > maxAge {
		return quotaBalanceSnapshot{}
	}
	signals := make(map[string]string, len(auth.Quota.Signals))
	for key, value := range auth.Quota.Signals {
		key = strings.ToLower(strings.TrimSpace(key))
		value = strings.TrimSpace(value)
		if previous, exists := signals[key]; exists && previous != value {
			// Conflicting case variants are not a trustworthy scalar value.
			value = ""
		}
		signals[key] = value
	}
	provider := strings.ToLower(strings.TrimSpace(auth.Provider))
	// The observation does not record the request model, and provider limit
	// names are not a model-eligibility mapping. Forecast only shared windows;
	// model-specific windows require a future explicit applicability contract.
	_ = routeModel
	if quotaBalanceHasUnmappedWindows(provider, signals) {
		return quotaBalanceSnapshot{}
	}
	var windows []quotaBalanceWindow
	switch provider {
	case "claude":
		for _, definition := range []struct {
			name     string
			duration time.Duration
		}{{"5h", 5 * time.Hour}, {"7d", 7 * 24 * time.Hour}} {
			prefix := "anthropic-ratelimit-unified-" + definition.name + "-"
			used, hasUsed := signals[prefix+"utilization"]
			reset, hasReset := signals[prefix+"reset"]
			if !hasUsed && !hasReset {
				continue
			}
			remaining, okUsed := quotaBalanceRemaining(used, 1)
			resetAt, okReset := quotaBalanceReset(reset)
			window := quotaBalanceWindow{remaining, resetAt, definition.duration}
			if !okUsed || !okReset || !quotaBalanceValidWindow(window, observedAt, now) {
				return quotaBalanceSnapshot{}
			}
			windows = append(windows, window)
		}
	case "codex":
		// These names map to the main rate_limits object in the existing
		// websocket collector. Active-Limit is its label, not a model mapping;
		// additional and code-review objects have separate namespaces.
		for _, name := range []string{"primary", "secondary"} {
			prefix := "x-codex-" + name + "-"
			used, hasUsed := signals[prefix+"used-percent"]
			minutes, hasMinutes := signals[prefix+"window-minutes"]
			reset, hasReset := signals[prefix+"reset-at"]
			after, hasAfter := signals[prefix+"reset-after-seconds"]
			if !hasUsed && !hasMinutes && !hasReset && !hasAfter {
				continue
			}
			remaining, okUsed := quotaBalanceRemaining(used, 100)
			durationMinutes, errMinutes := strconv.ParseInt(minutes, 10, 64)
			if !okUsed || errMinutes != nil || durationMinutes <= 0 || durationMinutes > (math.MaxInt64-int64(time.Hour))/int64(time.Minute) {
				return quotaBalanceSnapshot{}
			}
			duration := time.Duration(durationMinutes) * time.Minute
			var resetAt time.Time
			if hasReset {
				var okReset bool
				resetAt, okReset = quotaBalanceReset(reset)
				if !okReset {
					return quotaBalanceSnapshot{}
				}
			}
			if hasAfter {
				seconds, errSeconds := strconv.ParseInt(after, 10, 64)
				if errSeconds != nil || seconds <= 0 || seconds > int64((duration+time.Hour)/time.Second) {
					return quotaBalanceSnapshot{}
				}
				relativeReset := observedAt.Add(time.Duration(seconds) * time.Second)
				if hasReset {
					// Unix reset-at has second precision; ObservedAt may not.
					difference := resetAt.Sub(relativeReset)
					if difference < -time.Second || difference > time.Second {
						return quotaBalanceSnapshot{}
					}
				}
				if relativeReset.After(resetAt) {
					resetAt = relativeReset
				}
			}
			window := quotaBalanceWindow{remaining, resetAt, duration}
			if !quotaBalanceValidWindow(window, observedAt, now) {
				return quotaBalanceSnapshot{}
			}
			windows = append(windows, window)
		}
	}
	return quotaBalanceSnapshot{Known: len(windows) > 0, Windows: windows}
}

func quotaBalanceHasUnmappedWindows(provider string, signals map[string]string) bool {
	for name := range signals {
		switch provider {
		case "claude":
			const prefix = "anthropic-ratelimit-unified-"
			if !strings.HasPrefix(name, prefix) || strings.HasPrefix(name, prefix+"overage-") {
				continue
			}
			if name == prefix+"reset" || name == prefix+"5h-reset" || name == prefix+"7d-reset" ||
				name == prefix+"5h-utilization" || name == prefix+"7d-utilization" {
				continue
			}
			if strings.HasSuffix(name, "-reset") || strings.HasSuffix(name, "-utilization") {
				return true
			}
		case "codex":
			if !strings.HasPrefix(name, "x-codex-") || strings.HasPrefix(name, "x-codex-primary-") ||
				strings.HasPrefix(name, "x-codex-secondary-") || strings.HasPrefix(name, "x-codex-code-review-") {
				continue
			}
			for _, suffix := range []string{"-used-percent", "-window-minutes", "-reset-at", "-reset-after-seconds", "-limit-name"} {
				if strings.HasSuffix(name, suffix) {
					return true
				}
			}
		}
	}
	return false
}

func quotaBalanceRemaining(raw string, maximum float64) (int64, bool) {
	used, errParse := strconv.ParseFloat(raw, 64)
	if errParse != nil || math.IsNaN(used) || math.IsInf(used, 0) || used < 0 || used > maximum {
		return 0, false
	}
	remaining := int64(math.Round((maximum - used) * 10000 / maximum))
	if remaining == 0 && used < maximum {
		remaining = 1
	}
	return remaining, true
}

func quotaBalanceReset(raw string) (time.Time, bool) {
	if seconds, errParse := strconv.ParseInt(raw, 10, 64); errParse == nil && seconds > 0 {
		resetAt := time.Unix(seconds, 0)
		return resetAt, resetAt.Year() <= 9999
	}
	resetAt, errParse := time.Parse(time.RFC3339Nano, raw)
	return resetAt, errParse == nil && resetAt.Unix() > 0
}

func quotaBalanceValidWindow(window quotaBalanceWindow, observedAt, now time.Time) bool {
	return window.RemainingBasisPoints >= 0 && window.RemainingBasisPoints <= 10000 &&
		window.Duration > 0 && window.Duration <= time.Duration(math.MaxInt64)-time.Hour &&
		window.ResetAt.After(now) && window.ResetAt.After(observedAt) &&
		!window.ResetAt.After(observedAt.Add(window.Duration+time.Hour))
}

// quotaBalanceEffectiveWeight treats percentages as headroom, never absolute
// capacity. The tightest valid window wins, and reset preference is capped at 2x.
func quotaBalanceEffectiveWeight(baseWeight int64, snapshot quotaBalanceSnapshot, now time.Time) int64 {
	if baseWeight <= 0 {
		return 0
	}
	baseline := func() int64 {
		if baseWeight > math.MaxInt64/1000 {
			return math.MaxInt64
		}
		return baseWeight * 1000
	}
	if !snapshot.Known || len(snapshot.Windows) == 0 {
		return baseline()
	}
	score := 2.0
	for _, window := range snapshot.Windows {
		if !quotaBalanceValidWindow(window, now, now) {
			return baseline()
		}
		resetPreference := math.Min(2, float64(window.Duration)/float64(window.ResetAt.Sub(now)))
		score = math.Min(score, float64(window.RemainingBasisPoints)/10000*resetPreference)
	}
	if score == 0 {
		return 0
	}
	weight := float64(baseWeight) * 1000 * score
	if weight >= float64(math.MaxInt64) {
		return math.MaxInt64
	}
	if weight < 1 {
		return 1
	}
	return int64(weight)
}
