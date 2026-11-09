package auth

import (
	"math"
	"reflect"
	"strconv"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/credentialweight"
)

func quotaBalanceTestNow() time.Time {
	return time.Date(2026, time.November, 6, 12, 0, 0, 0, time.UTC)
}

func quotaBalanceTestAuth(provider string, now time.Time) *Auth {
	signals := map[string]string{
		"Anthropic-Ratelimit-Unified-5h-Utilization": "0.25",
		"Anthropic-Ratelimit-Unified-5h-Reset":       strconv.FormatInt(now.Add(5*time.Hour).Unix(), 10),
	}
	if provider == "codex" {
		signals = map[string]string{
			"X-Codex-Primary-Used-Percent":        "25",
			"X-Codex-Primary-Window-Minutes":      "300",
			"X-Codex-Primary-Reset-After-Seconds": "18000",
		}
	}
	return &Auth{Provider: provider, Quota: QuotaState{ObservedAt: now, Signals: signals}}
}

func TestQuotaBalanceClaudeSharedWindows(t *testing.T) {
	now := quotaBalanceTestNow()
	auth := quotaBalanceTestAuth("claude", now)
	auth.Provider = " CLAUDE "
	auth.Quota.Signals["anthropic-ratelimit-unified-7d-utilization"] = "0.8"
	auth.Quota.Signals["ANTHROPIC-RATELIMIT-UNIFIED-7D-RESET"] = now.Add(7 * 24 * time.Hour).Format(time.RFC3339)
	auth.Quota.NextRecoverAt = now.Add(365 * 24 * time.Hour)
	auth.NextRefreshAfter = now.Add(time.Minute)
	before := auth.Quota.Clone()
	snapshot := quotaBalanceSnapshotFor(auth, "claude-any-model", now, defaultQuotaBalanceMaxAge)
	want := quotaBalanceSnapshot{Known: true, Windows: []quotaBalanceWindow{
		{RemainingBasisPoints: 7500, ResetAt: now.Add(5 * time.Hour), Duration: 5 * time.Hour},
		{RemainingBasisPoints: 2000, ResetAt: now.Add(7 * 24 * time.Hour), Duration: 7 * 24 * time.Hour},
	}}
	if !snapshot.Known || len(snapshot.Windows) != len(want.Windows) {
		t.Fatalf("snapshot = %#v, want %#v", snapshot, want)
	}
	for i, window := range snapshot.Windows {
		if window.RemainingBasisPoints != want.Windows[i].RemainingBasisPoints ||
			window.Duration != want.Windows[i].Duration || !window.ResetAt.Equal(want.Windows[i].ResetAt) {
			t.Fatalf("window = %#v, want %#v", window, want.Windows[i])
		}
	}
	if !reflect.DeepEqual(auth.Quota, before) {
		t.Fatal("parsing mutated credential quota state")
	}
	if got := quotaBalanceEffectiveWeight(1, snapshot, now); got != 200 {
		t.Fatalf("tightest shared window weight = %d, want 200", got)
	}
}

func TestQuotaBalanceCodexDurationsAndObservedRelativeReset(t *testing.T) {
	now := quotaBalanceTestNow()
	auth := quotaBalanceTestAuth("codex", now)
	auth.Provider = "CoDeX"
	auth.Quota.ObservedAt = now.Add(-2 * time.Minute)
	auth.Quota.Signals = map[string]string{
		"x-codex-primary-used-percent":          "98",
		"X-Codex-Primary-Window-Minutes":        "10080",
		"X-Codex-Primary-Reset-After-Seconds":   "604800",
		"X-Codex-Primary-Reset-At":              strconv.FormatInt(auth.Quota.ObservedAt.Add(7*24*time.Hour).Unix(), 10),
		"X-Codex-Secondary-Used-Percent":        "10",
		"X-Codex-Secondary-Window-Minutes":      "300",
		"X-Codex-Secondary-Reset-After-Seconds": "1800",
		"X-Codex-Active-Limit":                  "codex_bengalfox",
	}
	snapshot := quotaBalanceSnapshotFor(auth, "some-request-model", now, defaultQuotaBalanceMaxAge)
	if !snapshot.Known || len(snapshot.Windows) != 2 {
		t.Fatalf("snapshot = %#v, want two known windows", snapshot)
	}
	if snapshot.Windows[0].Duration != 7*24*time.Hour || snapshot.Windows[1].Duration != 5*time.Hour {
		t.Fatalf("primary/secondary durations were assumed instead of parsed: %#v", snapshot.Windows)
	}
	if got := snapshot.Windows[1].ResetAt; !got.Equal(now.Add(28 * time.Minute)) {
		t.Fatalf("relative reset = %s, want ObservedAt + 1800s", got)
	}
	if got := quotaBalanceEffectiveWeight(1, snapshot, now); got >= 21 || got < 20 {
		t.Fatalf("short near-reset window hid exhausted weekly headroom: weight = %d", got)
	}
	delete(auth.Quota.Signals, "X-Codex-Primary-Reset-After-Seconds")
	if snapshot = quotaBalanceSnapshotFor(auth, "some-request-model", now, defaultQuotaBalanceMaxAge); !snapshot.Known {
		t.Fatal("absolute-only Codex reset was rejected")
	}
}

// Live Codex responses for plans without a second window report it as all
// zeros. That placeholder must not discard the real primary window.
func TestQuotaBalanceCodexIgnoresAllZeroSecondaryWindow(t *testing.T) {
	now := quotaBalanceTestNow()
	liveShape := func() *Auth {
		auth := quotaBalanceTestAuth("codex", now)
		auth.Quota.Signals = map[string]string{
			"X-Codex-Active-Limit":                         "premium",
			"X-Codex-Credits-Balance":                      "0",
			"X-Codex-Credits-Has-Credits":                  "False",
			"X-Codex-Credits-Unlimited":                    "False",
			"X-Codex-Plan-Type":                            "pro",
			"X-Codex-Primary-Over-Secondary-Limit-Percent": "0",
			"X-Codex-Primary-Reset-After-Seconds":          "136159",
			"X-Codex-Primary-Reset-At":                     strconv.FormatInt(now.Add(136159*time.Second).Unix(), 10),
			"X-Codex-Primary-Used-Percent":                 "100",
			"X-Codex-Primary-Window-Minutes":               "10080",
			"X-Codex-Secondary-Reset-After-Seconds":        "0",
			"X-Codex-Secondary-Used-Percent":               "0",
			"X-Codex-Secondary-Window-Minutes":             "0",
		}
		return auth
	}

	snapshot := quotaBalanceSnapshotFor(liveShape(), "gpt-6.1-sol", now, defaultQuotaBalanceMaxAge)
	if !snapshot.Known || len(snapshot.Windows) != 1 {
		t.Fatalf("snapshot = %#v, want the weekly primary window only", snapshot)
	}
	withoutResetAfter := liveShape()
	delete(withoutResetAfter.Quota.Signals, "X-Codex-Secondary-Reset-After-Seconds")
	if got := quotaBalanceSnapshotFor(withoutResetAfter, "gpt-6.1-sol", now, defaultQuotaBalanceMaxAge); !got.Known || len(got.Windows) != 1 {
		t.Fatalf("placeholder without reset-after = %#v, want the weekly primary window only", got)
	}
	if window := snapshot.Windows[0]; window.Duration != 7*24*time.Hour || window.RemainingBasisPoints != 0 ||
		!window.ResetAt.Equal(now.Add(136159*time.Second)) {
		t.Fatalf("primary window = %#v, want exhausted weekly window", window)
	}

	for name, modify := range map[string]func(map[string]string){
		"nonzero usage":       func(s map[string]string) { s["X-Codex-Secondary-Used-Percent"] = "10" },
		"nonzero reset-after": func(s map[string]string) { s["X-Codex-Secondary-Reset-After-Seconds"] = "1800" },
		"absolute reset": func(s map[string]string) {
			s["X-Codex-Secondary-Reset-At"] = strconv.FormatInt(now.Add(time.Hour).Unix(), 10)
		},
	} {
		t.Run("zero length with "+name, func(t *testing.T) {
			auth := liveShape()
			modify(auth.Quota.Signals)
			if snapshot := quotaBalanceSnapshotFor(auth, "gpt-6.1-sol", now, defaultQuotaBalanceMaxAge); snapshot.Known {
				t.Fatalf("zero-length window with data was accepted: %#v", snapshot)
			}
		})
	}
}

func TestQuotaBalanceRejectsUntrustworthySnapshots(t *testing.T) {
	now := quotaBalanceTestNow()
	tests := []struct {
		name     string
		provider string
		modify   func(*Auth)
	}{
		{"missing observation", "claude", func(a *Auth) { a.Quota.ObservedAt = time.Time{} }},
		{"stale", "claude", func(a *Auth) { a.Quota.ObservedAt = now.Add(-5*time.Minute - time.Nanosecond) }},
		{"future beyond skew", "claude", func(a *Auth) { a.Quota.ObservedAt = now.Add(30*time.Second + time.Nanosecond) }},
		{"pre-epoch observation", "claude", func(a *Auth) { a.Quota.ObservedAt = time.Unix(-1, 0) }},
		{"unsupported provider", "kimi", func(a *Auth) {}},
		{"empty", "claude", func(a *Auth) { a.Quota.Signals = nil }},
		{"missing utilization", "claude", func(a *Auth) { delete(a.Quota.Signals, "Anthropic-Ratelimit-Unified-5h-Utilization") }},
		{"missing reset", "claude", func(a *Auth) { delete(a.Quota.Signals, "Anthropic-Ratelimit-Unified-5h-Reset") }},
		{"partial sibling", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-7d-Utilization"] = "1" }},
		{"NaN", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Utilization"] = "NaN" }},
		{"infinite", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Utilization"] = "+Inf" }},
		{"negative", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Utilization"] = "-0.1" }},
		{"fraction out of range", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Utilization"] = "1.01" }},
		{"already reset", "claude", func(a *Auth) {
			a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Reset"] = strconv.FormatInt(now.Unix(), 10)
		}},
		{"past reset", "claude", func(a *Auth) {
			a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Reset"] = strconv.FormatInt(now.Add(-time.Second).Unix(), 10)
		}},
		{"implausible horizon", "claude", func(a *Auth) {
			a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Reset"] = strconv.FormatInt(now.Add(6*time.Hour+time.Second).Unix(), 10)
		}},
		{"invalid reset text", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Reset"] = "tomorrow" }},
		{"zero reset", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Reset"] = "0" }},
		{"overflow reset", "claude", func(a *Auth) { a.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Reset"] = "9223372036854775807" }},
		{"case variants contradict", "claude", func(a *Auth) { a.Quota.Signals["anthropic-ratelimit-unified-5h-utilization"] = "0.9" }},
		{"Codex missing duration", "codex", func(a *Auth) { delete(a.Quota.Signals, "X-Codex-Primary-Window-Minutes") }},
		{"Codex missing reset", "codex", func(a *Auth) { delete(a.Quota.Signals, "X-Codex-Primary-Reset-After-Seconds") }},
		{"Codex percent out of range", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Used-Percent"] = "100.1" }},
		{"Codex infinite percent", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Used-Percent"] = "-Inf" }},
		{"Codex zero duration", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Window-Minutes"] = "0" }},
		{"Codex negative duration", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Window-Minutes"] = "-1" }},
		{"Codex overflow duration", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Window-Minutes"] = "9223372036854775807" }},
		{"Codex fractional duration", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Window-Minutes"] = "3.5" }},
		{"Codex negative reset-after", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Reset-After-Seconds"] = "-1" }},
		{"Codex already reset-after", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Reset-After-Seconds"] = "0" }},
		{"Codex reset-after horizon", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Reset-After-Seconds"] = "21601" }},
		{"Codex contradictory reset", "codex", func(a *Auth) {
			a.Quota.Signals["X-Codex-Primary-Reset-At"] = strconv.FormatInt(now.Add(time.Hour).Unix(), 10)
		}},
		{"Codex invalid supplied absolute reset", "codex", func(a *Auth) { a.Quota.Signals["X-Codex-Primary-Reset-At"] = "garbage" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			auth := quotaBalanceTestAuth(tt.provider, now)
			tt.modify(auth)
			if snapshot := quotaBalanceSnapshotFor(auth, "request-model", now, defaultQuotaBalanceMaxAge); snapshot.Known {
				t.Fatalf("untrustworthy snapshot accepted: %#v", snapshot)
			}
		})
	}
	if quotaBalanceSnapshotFor(nil, "request-model", now, defaultQuotaBalanceMaxAge).Known {
		t.Fatal("nil auth was known")
	}
	if quotaBalanceSnapshotFor(quotaBalanceTestAuth("claude", now), "request-model", time.Time{}, defaultQuotaBalanceMaxAge).Known {
		t.Fatal("zero clock was known")
	}
}

func TestQuotaBalanceFreshnessBoundaries(t *testing.T) {
	now := quotaBalanceTestNow()
	for _, offset := range []time.Duration{-defaultQuotaBalanceMaxAge, 30 * time.Second} {
		auth := quotaBalanceTestAuth("claude", now)
		auth.Quota.ObservedAt = now.Add(offset)
		if snapshot := quotaBalanceSnapshotFor(auth, "request-model", now, 0); !snapshot.Known {
			t.Fatalf("boundary observation offset %s rejected", offset)
		}
	}
	auth := quotaBalanceTestAuth("codex", now)
	auth.Quota.ObservedAt = now.Add(-time.Minute)
	if quotaBalanceSnapshotFor(auth, "request-model", now, 30*time.Second).Known {
		t.Fatal("custom max age ignored")
	}
	auth.Quota.ObservedAt = now.Add(750 * time.Millisecond)
	auth.Quota.Signals["X-Codex-Primary-Reset-At"] = strconv.FormatInt(now.Add(5*time.Hour).Unix(), 10)
	if !quotaBalanceSnapshotFor(auth, "request-model", now, defaultQuotaBalanceMaxAge).Known {
		t.Fatal("second precision reset-at disagreed with fractional ObservedAt")
	}
}

func TestQuotaBalanceFeatureAndUnmappedLimits(t *testing.T) {
	now := quotaBalanceTestNow()
	claude := quotaBalanceTestAuth("claude", now)
	claude.Quota.Signals["Anthropic-Ratelimit-Unified-Overage-Utilization"] = "1"
	claude.Quota.Signals["Anthropic-Ratelimit-Unified-Overage-Reset"] = strconv.FormatInt(now.Add(30*24*time.Hour).Unix(), 10)
	claude.Quota.Signals["Anthropic-Ratelimit-Unified-Overage-Status"] = "rejected"
	claude.Quota.Signals["Anthropic-Ratelimit-Unified-Reset"] = "not-a-shared-window"
	claude.Quota.Signals["Retry-After"] = "999999"
	codex := quotaBalanceTestAuth("codex", now)
	codex.Quota.Signals["X-Codex-Code-Review-Primary-Used-Percent"] = "100"
	codex.Quota.Signals["X-Codex-Code-Review-Primary-Reset-At"] = "invalid"
	codex.Quota.Signals["X-Codex-Credits-Balance"] = "0"
	codex.Quota.Signals["X-Codex-Credits-Has-Credits"] = "false"
	for _, auth := range []*Auth{claude, codex} {
		snapshot := quotaBalanceSnapshotFor(auth, "ordinary-request", now, defaultQuotaBalanceMaxAge)
		if !snapshot.Known || quotaBalanceEffectiveWeight(1, snapshot, now) != 750 {
			t.Fatalf("unrelated feature or billing data affected shared headroom: %#v", snapshot)
		}
	}
	for _, name := range []string{
		"X-Codex-Additional-Gpt-5.3-Codex-Spark-Primary-Used-Percent",
		"X-Codex-Bengalfox-Secondary-Reset-At",
		"X-Codex-Bengalfox-Limit-Name",
	} {
		auth := quotaBalanceTestAuth("codex", now)
		auth.Quota.Signals[name] = "100"
		if quotaBalanceSnapshotFor(auth, "gpt-5.3-codex-spark", now, defaultQuotaBalanceMaxAge).Known {
			t.Fatalf("unmapped model quota %s was silently omitted", name)
		}
	}
	claude.Quota.Signals["Anthropic-Ratelimit-Unified-7d_oi-Utilization"] = "1"
	if quotaBalanceSnapshotFor(claude, "claude-any-model", now, defaultQuotaBalanceMaxAge).Known {
		t.Fatal("unmapped Claude model-specific window was silently omitted")
	}
}

func TestQuotaBalanceDoesNotCombineObservationsOrBorrowRecovery(t *testing.T) {
	now := quotaBalanceTestNow()
	auth := quotaBalanceTestAuth("claude", now)
	delete(auth.Quota.Signals, "Anthropic-Ratelimit-Unified-5h-Reset")
	auth.Quota.NextRecoverAt = now.Add(time.Hour)
	auth.Quota.Signals["Retry-After"] = "3600"
	auth.ModelStates = map[string]*ModelState{
		"request-model": {Quota: QuotaState{ObservedAt: now.Add(-time.Second), Signals: map[string]string{
			"Anthropic-Ratelimit-Unified-5h-Reset": strconv.FormatInt(now.Add(time.Hour).Unix(), 10),
		}}},
	}
	if quotaBalanceSnapshotFor(auth, "request-model", now, defaultQuotaBalanceMaxAge).Known {
		t.Fatal("missing reset was borrowed from a different observation or cooldown")
	}
}

func TestQuotaBalanceEffectiveWeight(t *testing.T) {
	now := quotaBalanceTestNow()
	snapshot := func(remaining int64, resetAfter, duration time.Duration) quotaBalanceSnapshot {
		return quotaBalanceSnapshot{Known: true, Windows: []quotaBalanceWindow{{remaining, now.Add(resetAfter), duration}}}
	}
	tests := []struct {
		name     string
		base     int64
		snapshot quotaBalanceSnapshot
		want     int64
	}{
		{"unknown", 1, quotaBalanceSnapshot{}, 1000},
		{"empty known", 1, quotaBalanceSnapshot{Known: true}, 1000},
		{"full equals unknown", 1, snapshot(10000, 5*time.Hour, 5*time.Hour), 1000},
		{"half headroom", 1, snapshot(5000, 5*time.Hour, 5*time.Hour), 500},
		{"near reset preferred", 1, snapshot(5000, time.Hour, 5*time.Hour), 1000},
		{"reset preference bounded", 1, snapshot(10000, time.Nanosecond, 5*time.Hour), 2000},
		{"exhausted", 1, snapshot(0, time.Nanosecond, 5*time.Hour), 0},
		{"positive floor", 1, snapshot(1, 6*time.Hour, 5*time.Hour), 1},
		{"configured ratio", 7, snapshot(5000, 5*time.Hour, 5*time.Hour), 3500},
		{"configured maximum", credentialweight.Max, snapshot(10000, time.Hour, 5*time.Hour), credentialweight.Max * 2000},
		{"disabled", 0, snapshot(10000, time.Hour, 5*time.Hour), 0},
		{"negative", -1, quotaBalanceSnapshot{}, 0},
		{"unknown saturation", math.MaxInt64, quotaBalanceSnapshot{}, math.MaxInt64},
		{"known saturation", math.MaxInt64, snapshot(10000, time.Hour, 5*time.Hour), math.MaxInt64},
		{"expired snapshot fallback", 3, snapshot(0, -time.Second, 5*time.Hour), 3000},
		{"invalid remaining fallback", 3, snapshot(10001, time.Hour, 5*time.Hour), 3000},
		{"negative remaining fallback", 3, snapshot(-1, time.Hour, 5*time.Hour), 3000},
		{"invalid duration fallback", 3, snapshot(10000, time.Hour, 0), 3000},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := quotaBalanceEffectiveWeight(tt.base, tt.snapshot, now); got != tt.want {
				t.Fatalf("weight = %d, want %d", got, tt.want)
			}
		})
	}
	a := snapshot(5000, 5*time.Hour, 5*time.Hour)
	b := snapshot(5000, 5*time.Hour, 5*time.Hour)
	if quotaBalanceEffectiveWeight(17, a, now) != quotaBalanceEffectiveWeight(17, b, now) {
		t.Fatal("equal snapshots produced different weights")
	}
	a.Windows = append(a.Windows, quotaBalanceWindow{0, now.Add(7 * 24 * time.Hour), 7 * 24 * time.Hour})
	if got := quotaBalanceEffectiveWeight(17, a, now); got != 0 {
		t.Fatalf("weekly exhaustion must dominate short reset preference, got %d", got)
	}
}

func TestQuotaBalanceTinyPositiveHeadroom(t *testing.T) {
	now := quotaBalanceTestNow()
	for _, provider := range []string{"claude", "codex"} {
		auth := quotaBalanceTestAuth(provider, now)
		if provider == "claude" {
			auth.Quota.Signals["Anthropic-Ratelimit-Unified-5h-Utilization"] = "0.99999999"
		} else {
			auth.Quota.Signals["X-Codex-Primary-Used-Percent"] = "99.999999"
		}
		snapshot := quotaBalanceSnapshotFor(auth, "request-model", now, defaultQuotaBalanceMaxAge)
		if !snapshot.Known || snapshot.Windows[0].RemainingBasisPoints != 1 || quotaBalanceEffectiveWeight(1, snapshot, now) != 1 {
			t.Fatalf("positive %s headroom was rounded to exhaustion: %#v", provider, snapshot)
		}
	}
}
