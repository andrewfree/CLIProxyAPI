package auth

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strconv"
	"sync"
	"testing"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func quotaAwareTestClock() time.Time {
	return time.Date(2026, time.November, 6, 12, 0, 0, 0, time.UTC)
}

func quotaAwareTestSelector(now time.Time) *QuotaAwareSelector {
	selector := NewQuotaAwareSelector(QuotaAwareSelectorOptions{})
	selector.nowFunc = func() time.Time { return now }
	return selector
}

func quotaAwareTestAuth(id, used string, resetIn time.Duration, now time.Time) *Auth {
	return &Auth{
		ID:       id,
		Provider: "codex",
		Quota: QuotaState{
			ObservedAt: now,
			Signals: map[string]string{
				"X-Codex-Primary-Used-Percent":   used,
				"X-Codex-Primary-Window-Minutes": "300",
				"X-Codex-Primary-Reset-At":       strconv.FormatInt(now.Add(resetIn).Unix(), 10),
			},
		},
	}
}

func quotaAwareTestCounts(t *testing.T, selector *QuotaAwareSelector, auths []*Auth, count int) map[string]int {
	t.Helper()
	counts := make(map[string]int)
	for index := 0; index < count; index++ {
		picked, errPick := selector.Pick(context.Background(), "codex", "route", cliproxyexecutor.Options{}, auths)
		if errPick != nil || picked == nil {
			t.Fatalf("Pick() #%d = %v, %v", index, picked, errPick)
		}
		counts[picked.ID]++
	}
	return counts
}

func TestQuotaAwareUnknownUsesOrdinaryRoundRobin(t *testing.T) {
	t.Parallel()
	selector := &QuotaAwareSelector{}
	auths := []*Auth{
		{ID: "c", Attributes: map[string]string{AttributeWeight: "9"}},
		{ID: "b", Attributes: map[string]string{AttributeWeight: "2"}},
		{ID: "a", Attributes: map[string]string{AttributeWeight: "1"}},
		{ID: "zero", Attributes: map[string]string{AttributeWeight: "0"}},
	}
	for index, want := range []string{"a", "b", "c", "a", "b", "c"} {
		model := "route"
		if index%2 == 1 {
			model += "(high)"
		}
		picked, errPick := selector.Pick(context.Background(), "codex", model, cliproxyexecutor.Options{}, auths)
		if errPick != nil || picked == nil || picked.ID != want {
			t.Fatalf("Pick() #%d = %v, %v; want %s", index, picked, errPick, want)
		}
	}
	if len(selector.states) != 1 {
		t.Fatalf("thinking suffix created %d rotation states, want 1", len(selector.states))
	}
}

func TestQuotaAwareEqualKnownFairness(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	auths := []*Auth{
		quotaAwareTestAuth("c", "25", 3*time.Hour, now),
		quotaAwareTestAuth("a", "25", 3*time.Hour, now),
		quotaAwareTestAuth("b", "25", 3*time.Hour, now),
	}
	counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), auths, 300)
	if !reflect.DeepEqual(counts, map[string]int{"a": 100, "b": 100, "c": 100}) {
		t.Fatalf("equal known counts = %v", counts)
	}
}

func TestQuotaAwareResetUrgencyBiasIsCapped(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	for _, resetIn := range []time.Duration{30 * time.Minute, time.Minute} {
		t.Run(resetIn.String(), func(t *testing.T) {
			auths := []*Auth{
				quotaAwareTestAuth("a", "0", 5*time.Hour, now),
				quotaAwareTestAuth("b", "0", resetIn, now),
			}
			counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), auths, 300)
			if counts["a"] != 100 || counts["b"] != 200 {
				t.Fatalf("urgency counts = %v, want a:b=100:200", counts)
			}
		})
	}
}

func TestQuotaAwareFakeClockMakesStaleForecastsOrdinaryRotation(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	clock := now
	selector := NewQuotaAwareSelector(QuotaAwareSelectorOptions{})
	selector.nowFunc = func() time.Time { return clock }
	auths := []*Auth{
		quotaAwareTestAuth("a", "0", 5*time.Hour, now),
		quotaAwareTestAuth("b", "0", time.Hour, now),
	}
	countsFresh := quotaAwareTestCounts(t, selector, auths, 300)
	if countsFresh["a"] != 100 || countsFresh["b"] != 200 {
		t.Fatalf("fresh counts = %v, want urgency preference", countsFresh)
	}
	clock = clock.Add(defaultQuotaBalanceMaxAge + time.Second)
	countsStale := quotaAwareTestCounts(t, selector, auths, 300)
	if countsStale["a"] != 150 || countsStale["b"] != 150 {
		t.Fatalf("stale counts = %v, want ordinary rotation", countsStale)
	}
}

func TestQuotaAwareCapacityAndUnknownNeutralShare(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	t.Run("configured capacity", func(t *testing.T) {
		a := quotaAwareTestAuth("a", "50", 5*time.Hour, now)
		a.Attributes = map[string]string{AttributeWeight: "3"}
		b := quotaAwareTestAuth("b", "50", 5*time.Hour, now)
		counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), []*Auth{a, b}, 400)
		if counts["a"] != 300 || counts["b"] != 100 {
			t.Fatalf("capacity counts = %v, want a:b=300:100", counts)
		}
	})
	t.Run("mixed unknown", func(t *testing.T) {
		a := &Auth{ID: "a", Provider: "codex"}
		b := quotaAwareTestAuth("b", "0", 30*time.Minute, now)
		counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), []*Auth{a, b}, 300)
		if counts["a"] != 100 || counts["b"] != 200 {
			t.Fatalf("mixed counts = %v, want neutral a:b=100:200", counts)
		}
	})
}

func TestQuotaAwareChangingObservationsDoNotRestartAtFirstIdentity(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	selector := quotaAwareTestSelector(now)
	a := quotaAwareTestAuth("a", "50", 5*time.Hour, now)
	b := quotaAwareTestAuth("b", "50", 5*time.Hour, now)
	for index := 0; index < 100; index++ {
		used := strconv.Itoa(40 + index%10)
		a.Quota.Signals["X-Codex-Primary-Used-Percent"] = used
		b.Quota.Signals["X-Codex-Primary-Used-Percent"] = used
		picked, errPick := selector.Pick(context.Background(), "codex", "route", cliproxyexecutor.Options{}, []*Auth{a, b})
		want := "a"
		if index%2 == 1 {
			want = "b"
		}
		if errPick != nil || picked == nil || picked.ID != want {
			t.Fatalf("changing observation #%d = %v, %v; want %s", index, picked, errPick, want)
		}
	}
}

func TestQuotaAwareChangingCapacityDoesNotCauseCatchUpBurst(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	selector := quotaAwareTestSelector(now)
	a := quotaAwareTestAuth("a", "50", 5*time.Hour, now)
	a.Attributes = map[string]string{AttributeWeight: "1000000"}
	b := quotaAwareTestAuth("b", "50", 5*time.Hour, now)
	quotaAwareTestCounts(t, selector, []*Auth{a, b}, 2000)
	a.Attributes[AttributeWeight] = "1"
	counts := quotaAwareTestCounts(t, selector, []*Auth{a, b}, 20)
	if counts["a"] < 9 || counts["a"] > 11 || counts["b"] < 9 || counts["b"] > 11 {
		t.Fatalf("counts after capacity change = %v, want balanced within one pick", counts)
	}
}

func TestQuotaAwareTemporaryExclusionsRetainBoundedCredits(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	selector := quotaAwareTestSelector(now)
	a := quotaAwareTestAuth("a", "20", 5*time.Hour, now)
	b := quotaAwareTestAuth("b", "20", 5*time.Hour, now)
	c := quotaAwareTestAuth("c", "20", 5*time.Hour, now)
	quotaAwareTestCounts(t, selector, []*Auth{a, b, c}, 1)
	countsSubset := quotaAwareTestCounts(t, selector, []*Auth{a, c}, 1000)
	if countsSubset["a"] < 499 || countsSubset["a"] > 501 {
		t.Fatalf("temporary subset lost fairness: %v", countsSubset)
	}
	countsRejoined := quotaAwareTestCounts(t, selector, []*Auth{a, b, c}, 300)
	for _, id := range []string{"a", "b", "c"} {
		if countsRejoined[id] < 99 || countsRejoined[id] > 101 {
			t.Fatalf("rejoined counts = %v, want no catch-up burst", countsRejoined)
		}
	}
	for _, state := range selector.states {
		for id, credit := range state.current {
			if credit < -1 || credit > 1 {
				t.Fatalf("unbounded credit %s = %v", id, credit)
			}
		}
	}
}

func TestQuotaAwareForecastNeverOverridesPinOrSoleCandidate(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	zero := quotaAwareTestAuth("zero", "100", time.Hour, now)
	zero.Attributes = map[string]string{AttributeWeight: "0"}
	other := quotaAwareTestAuth("other", "0", time.Hour, now)
	selector := quotaAwareTestSelector(now)
	for _, pin := range []any{" zero ", []byte("zero")} {
		opts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: pin}}
		picked, errPick := selector.Pick(context.Background(), "codex", "route", opts, []*Auth{other, zero})
		if errPick != nil || picked != zero {
			t.Fatalf("pinned zero = %v, %v; want same credential", picked, errPick)
		}
	}
	for _, candidates := range [][]*Auth{{zero}, {zero, {ID: "disabled", Disabled: true}}} {
		picked, errPick := selector.Pick(context.Background(), "codex", "route", cliproxyexecutor.Options{}, candidates)
		if errPick != nil || picked != zero {
			t.Fatalf("sole eligible zero = %v, %v; want same credential", picked, errPick)
		}
	}
	zero.Disabled = true
	opts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: "zero"}}
	if picked, errPick := selector.Pick(context.Background(), "codex", "route", opts, []*Auth{zero, other}); errPick == nil || picked != nil {
		t.Fatalf("disabled pin switched or ignored eligibility: %v, %v", picked, errPick)
	}
	opts.Metadata[cliproxyexecutor.PinnedAuthMetadataKey] = "missing"
	if picked, errPick := selector.Pick(context.Background(), "codex", "route", opts, []*Auth{other}); errPick == nil || picked != nil {
		t.Fatalf("missing pin switched accounts: %v, %v", picked, errPick)
	}
}

func TestQuotaAwareConfiguredZeroWeightIsExcludedFromPool(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	zero := &Auth{ID: "zero", Attributes: map[string]string{AttributeWeight: "0", "priority": "100"}}
	a := &Auth{ID: "a"}
	counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), []*Auth{zero, a}, 10)
	if counts["zero"] != 0 || counts["a"] != 10 {
		t.Fatalf("zero-weight high priority suppressed positive lower tier: %v", counts)
	}
	b := &Auth{ID: "b", Attributes: map[string]string{AttributeWeight: "0"}}
	if picked, errPick := quotaAwareTestSelector(now).Pick(context.Background(), "codex", "route", cliproxyexecutor.Options{}, []*Auth{zero, b}); errPick == nil || picked != nil {
		t.Fatalf("all configured zero pool = %v, %v; want unavailable", picked, errPick)
	}
}

func TestQuotaAwareAllExhaustedHintsUseSafeRoundRobinFallback(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	auths := []*Auth{
		quotaAwareTestAuth("a", "100", time.Hour, now),
		quotaAwareTestAuth("b", "100", time.Hour, now),
	}
	counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), auths, 100)
	if counts["a"] != 50 || counts["b"] != 50 {
		t.Fatalf("forecast-only exhaustion blocked or skewed eligible accounts: %v", counts)
	}
}

func TestQuotaAwareRealEligibilityAndPrioritiesRemainAuthoritative(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	auths := []*Auth{
		{ID: "invalid", Unavailable: true, Status: StatusError, LastError: &Error{HTTPStatus: 401}},
		{ID: "disabled", Disabled: true},
		{ID: "cooldown", Quota: QuotaState{Exceeded: true, Reason: "credential_quota", NextRecoverAt: now.Add(time.Hour)}},
		{ID: "model-cooldown", ModelStates: map[string]*ModelState{"route": {Unavailable: true, NextRetryAfter: now.Add(time.Hour)}}},
		{ID: "low", Attributes: map[string]string{"priority": "-1"}},
		{ID: "high", Attributes: map[string]string{"priority": "1"}},
	}
	counts := quotaAwareTestCounts(t, quotaAwareTestSelector(now), auths, 30)
	if len(counts) != 1 || counts["high"] != 30 {
		t.Fatalf("eligibility/priority counts = %v, want high only", counts)
	}
	for _, blocked := range auths[:4] {
		opts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: blocked.ID}}
		if picked, errPick := quotaAwareTestSelector(now).Pick(context.Background(), "codex", "route", opts, auths); errPick == nil || picked != nil {
			t.Fatalf("blocked explicit pin %s = %v, %v", blocked.ID, picked, errPick)
		}
	}
}

func TestQuotaAwareWebsocketPreferenceRemainsAuthoritative(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	highQuota := quotaAwareTestAuth("a", "0", time.Hour, now)
	websocket := quotaAwareTestAuth("b", "99", time.Hour, now)
	websocket.Attributes = map[string]string{"websockets": "true"}
	ctx := cliproxyexecutor.WithDownstreamWebsocket(context.Background())
	selector := quotaAwareTestSelector(now)
	for index := 0; index < 20; index++ {
		picked, errPick := selector.Pick(ctx, "codex", "route", cliproxyexecutor.Options{}, []*Auth{highQuota, websocket})
		if errPick != nil || picked != websocket {
			t.Fatalf("websocket preference = %v, %v; want websocket credential", picked, errPick)
		}
	}
}

func TestQuotaAwareShadowReturnsExactOrdinaryRoundRobin(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	shadow := NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true})
	shadow.nowFunc = func() time.Time { return now }
	baseline := &RoundRobinSelector{}
	a := quotaAwareTestAuth("a", "99", time.Hour, now)
	b := quotaAwareTestAuth("b", "0", time.Hour, now)
	c := &Auth{ID: "c", Attributes: map[string]string{AttributeWeight: "0"}}
	d := &Auth{ID: "d", Attributes: map[string]string{AttributeWeight: "0"}}
	for index := 0; index < 200; index++ {
		auths := []*Auth{c, b, a}
		if index%7 == 0 {
			auths = []*Auth{c, a}
		}
		if index%11 == 0 {
			auths = []*Auth{b}
		}
		if index%13 == 0 {
			auths = []*Auth{d, c}
		}
		a.Quota.Signals["X-Codex-Primary-Used-Percent"] = strconv.Itoa(index % 100)
		model := "route"
		if index%2 == 1 {
			model += "(high)"
		}
		actual, errActual := shadow.Pick(context.Background(), "codex", model, cliproxyexecutor.Options{}, auths)
		want, errWant := baseline.Pick(context.Background(), "codex", model, cliproxyexecutor.Options{}, auths)
		if errActual != nil || errWant != nil || actual != want {
			t.Fatalf("shadow #%d = %v, %v; baseline %v, %v", index, actual, errActual, want, errWant)
		}
	}
}

func TestQuotaAwareConcurrentFairDistribution(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	for _, known := range []bool{false, true} {
		t.Run(strconv.FormatBool(known), func(t *testing.T) {
			auths := []*Auth{{ID: "a"}, {ID: "b"}, {ID: "c"}}
			if known {
				for index, candidate := range auths {
					auths[index] = quotaAwareTestAuth(candidate.ID, "50", time.Hour, now)
				}
				auths[0].Attributes = map[string]string{AttributeWeight: "2"}
			}
			selector := quotaAwareTestSelector(now)
			results := make(chan string, 900)
			var workers sync.WaitGroup
			for worker := 0; worker < 30; worker++ {
				workers.Add(1)
				go func() {
					defer workers.Done()
					for pick := 0; pick < 30; pick++ {
						chosen, errPick := selector.Pick(context.Background(), "codex", "route", cliproxyexecutor.Options{}, auths)
						if errPick != nil || chosen == nil {
							results <- "error"
							continue
						}
						results <- chosen.ID
					}
				}()
			}
			workers.Wait()
			close(results)
			counts := make(map[string]int)
			for id := range results {
				counts[id]++
			}
			want := map[string]int{"a": 300, "b": 300, "c": 300}
			if known {
				want = map[string]int{"a": 450, "b": 225, "c": 225}
			}
			if !reflect.DeepEqual(counts, want) {
				t.Fatalf("concurrent counts = %v", counts)
			}
		})
	}
}

func TestQuotaAwareDoesNotMutateAuthsOrOptions(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	auths := []*Auth{
		quotaAwareTestAuth("b", "20", time.Hour, now),
		quotaAwareTestAuth("a", "60", 3*time.Hour, now),
	}
	opts := cliproxyexecutor.Options{Metadata: map[string]any{"caller": "unchanged"}}
	before, errBefore := json.Marshal(auths)
	if errBefore != nil {
		t.Fatal(errBefore)
	}
	selector := quotaAwareTestSelector(now)
	for index := 0; index < 20; index++ {
		if _, errPick := selector.Pick(context.Background(), "codex", "route", opts, auths); errPick != nil {
			t.Fatal(errPick)
		}
	}
	after, errAfter := json.Marshal(auths)
	if errAfter != nil {
		t.Fatal(errAfter)
	}
	if string(before) != string(after) || !reflect.DeepEqual(opts.Metadata, map[string]any{"caller": "unchanged"}) {
		t.Fatal("selection mutated candidate snapshots, order, or request metadata")
	}
}

func TestQuotaAwareStateCardinalityIsBounded(t *testing.T) {
	t.Parallel()
	now := quotaAwareTestClock()
	selector := quotaAwareTestSelector(now)
	selector.maxKeys = 4
	a := quotaAwareTestAuth("a", "50", time.Hour, now)
	b := quotaAwareTestAuth("b", "50", time.Hour, now)
	for index := 0; index < 30; index++ {
		if _, errPick := selector.Pick(context.Background(), "codex", fmt.Sprintf("route-%d", index), cliproxyexecutor.Options{}, []*Auth{a, b}); errPick != nil {
			t.Fatal(errPick)
		}
		if len(selector.states) > selector.maxKeys {
			t.Fatalf("route state count = %d, limit %d", len(selector.states), selector.maxKeys)
		}
	}
	for index := 0; index < maxSmoothWeightedStateEntries+10; index++ {
		candidate := quotaAwareTestAuth(fmt.Sprintf("transient-%d", index), "50", time.Hour, now)
		quotaAwareTestCounts(t, selector, []*Auth{a, candidate}, 1)
		if entries := len(selector.states["codex:route"].current); entries > maxSmoothWeightedStateEntries {
			t.Fatalf("retired candidate entries = %d, limit %d", entries, maxSmoothWeightedStateEntries)
		}
	}
	shadow := NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true})
	shadow.maxKeys = 4
	shadow.nowFunc = func() time.Time { return now }
	for index := 0; index < 30; index++ {
		if _, errPick := shadow.Pick(context.Background(), "codex", fmt.Sprintf("route-%d", index), cliproxyexecutor.Options{}, []*Auth{a, b}); errPick != nil {
			t.Fatal(errPick)
		}
		if len(shadow.states) > 4 || len(shadow.roundRobin.lastPicked) > 4 {
			t.Fatalf("shadow state count = %d forecast, %d actual; limit 4", len(shadow.states), len(shadow.roundRobin.lastPicked))
		}
	}
}
