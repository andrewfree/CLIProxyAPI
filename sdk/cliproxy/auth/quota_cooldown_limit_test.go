package auth

import (
	"context"
	"math"
	"net/http"
	"testing"
	"time"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func TestQuotaCooldownLimit_NewFailures(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, model := range []string{"", "quota-model"} {
		for _, tc := range []struct {
			name    string
			seconds int
			hint    time.Duration
			want    time.Duration
		}{
			{"disabled", 0, 5 * 24 * time.Hour, 5 * 24 * time.Hour},
			{"negative", -1, 5 * 24 * time.Hour, 5 * 24 * time.Hour},
			{"bounded", 3600, 5 * 24 * time.Hour, time.Hour},
			{"short hint", 3600, time.Minute, time.Minute},
			{"minimum cap", 1, time.Hour, minQuotaCooldownFloor},
			{"minimum hint", 3600, time.Millisecond, minQuotaCooldownFloor},
			{"overflow", math.MaxInt, time.Hour, time.Hour},
		} {
			t.Run(model+"/"+tc.name, func(t *testing.T) {
				m, auth := newCooldownMonotonicManager(t, "quota-model", "quota-sibling")
				m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: tc.seconds})
				m.mu.Lock()
				ensureModelState(m.auths[auth.ID], "quota-sibling")
				m.mu.Unlock()
				before := time.Now()
				m.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: auth.Provider, Model: model,
					RetryAfter: &tc.hint, CredentialScope: true, Error: &Error{HTTPStatus: 429, Message: "upstream reset remains available"}})
				after := time.Now()
				got, _ := m.GetByID(auth.ID)
				check := func(next time.Time) {
					t.Helper()
					if next.Before(before.Add(tc.want)) || next.After(after.Add(tc.want)) {
						t.Fatalf("deadline %v outside %v..%v", next, before.Add(tc.want), after.Add(tc.want))
					}
				}
				check(got.NextRetryAfter)
				check(got.Quota.NextRecoverAt)
				if got.LastError.Message != "upstream reset remains available" {
					t.Fatal("lost original upstream error")
				}
				if model != "" {
					for _, name := range []string{model, "quota-sibling"} {
						check(got.ModelStates[name].NextRetryAfter)
						check(got.ModelStates[name].Quota.NextRecoverAt)
						if blocked, _, _ := isAuthBlockedForModel(got, name, before.Add(tc.want/2)); !blocked {
							t.Fatal("quota allowed traffic too soon")
						}
						if blocked, _, _ := isAuthBlockedForModel(got, name, after.Add(tc.want+time.Second)); blocked {
							t.Fatal("quota did not expire")
						}
					}
				}
			})
		}
	}
}

func TestQuotaCooldownLimit_BackoffWithoutHint(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, model := range []string{"", "quota-model"} {
		t.Run(model, func(t *testing.T) {
			m, auth := newCooldownMonotonicManager(t, "quota-model")
			m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 60})
			m.mu.Lock()
			quota := QuotaState{Exceeded: true, Reason: "quota", BackoffLevel: 20, NextRecoverAt: time.Now().Add(-time.Minute)}
			if model == "" {
				m.auths[auth.ID].Quota = quota
			} else {
				ensureModelState(m.auths[auth.ID], model).Quota = quota
			}
			m.mu.Unlock()
			m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: model, Error: &Error{HTTPStatus: 429}})
			got, _ := m.GetByID(auth.ID)
			if got.Quota.NextRecoverAt.After(time.Now().Add(time.Minute)) {
				t.Fatal("backoff exceeded cap without an upstream hint")
			}
		})
	}
}

func TestQuotaCooldownLimit_ReloadPreservesOtherFailuresAndPersists(t *testing.T) {
	withQuotaCooldownEnabled(t)
	m, auth := newCooldownMonotonicManager(t, "quota-model", "missing-model")
	store := &recordingCooldownStateStore{}
	m.SetCooldownStateStore(store)
	m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: "missing-model", Error: &Error{HTTPStatus: 404}})
	long := 5 * time.Hour
	m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: "quota-model", CredentialScope: true, RetryAfter: &long, Error: &Error{HTTPStatus: 429}})
	before, _ := m.GetByID(auth.ID)
	cfg := &internalconfig.Config{QuotaCooldownMaxSeconds: 3600, SaveCooldownStatus: true}
	m.SetConfig(cfg)
	got, _ := m.GetByID(auth.ID)
	if !got.ModelStates["missing-model"].NextRetryAfter.Equal(before.ModelStates["missing-model"].NextRetryAfter) {
		t.Fatal("shortened independent 404 deadline")
	}
	if !got.Quota.NextRecoverAt.Equal(before.UpdatedAt.Add(time.Hour)) {
		t.Fatal("reload did not clamp credential cooldown from original timestamp")
	}
	m.SetConfig(cfg)
	again, _ := m.GetByID(auth.ID)
	if !again.Quota.NextRecoverAt.Equal(got.Quota.NextRecoverAt) {
		t.Fatal("reload renewed cooldown")
	}
	if blocked, _, _ := isAuthBlockedForModel(got, "quota-model", before.UpdatedAt.Add(2*time.Hour)); blocked {
		t.Fatal("quota model remained blocked")
	}
	if blocked, _, _ := isAuthBlockedForModel(got, "missing-model", before.UpdatedAt.Add(2*time.Hour)); !blocked {
		t.Fatal("404 model became selectable")
	}
	records := store.savedRecords()
	if len(records) != 3 {
		t.Fatalf("saved %d cooldown records, want 3", len(records))
	}
	for _, record := range records {
		if record.Model != "missing-model" && record.NextRetryAfter.After(before.UpdatedAt.Add(time.Hour)) {
			t.Fatal("persisted old quota deadline")
		}
	}
}

func TestQuotaCooldownLimit_RestoreAndRetry(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, model := range []string{"", "quota-model"} {
		for _, age := range []time.Duration{30 * time.Minute, 2 * time.Hour} {
			t.Run(model+"/"+age.String(), func(t *testing.T) {
				m, auth := newCooldownMonotonicManager(t, "quota-model")
				m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 3600, SaveCooldownStatus: true})
				now := time.Now()
				deadline := now.Add(5 * 24 * time.Hour)
				store := &recordingCooldownStateStore{load: []CooldownStateRecord{{AuthID: auth.ID, Model: model,
					UpdatedAt: now.Add(-age), NextRetryAfter: deadline,
					Quota: QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline}, LastError: &Error{HTTPStatus: 429}}}}
				m.SetCooldownStateStore(store)
				if err := m.RestoreCooldownStates(context.Background()); err != nil {
					t.Fatal(err)
				}
				got, _ := m.GetByID(auth.ID)
				if age < time.Hour {
					if !got.NextRetryAfter.Equal(now.Add(-age).Add(time.Hour)) {
						t.Fatalf("restored deadline = %v", got.NextRetryAfter)
					}
					return
				}
				if len(store.savedRecords()) != 0 {
					t.Fatal("expired saved quota resurrected")
				}
				if _, err := m.scheduler.pickSingle(context.Background(), auth.Provider, "quota-model", cliproxyexecutor.Options{}, nil); err != nil {
					t.Fatalf("scheduler failed to retry expired quota: %v", err)
				}
				m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: model, Success: true})
				got, _ = m.GetByID(auth.ID)
				if blocked, _, _ := isAuthBlockedForModel(got, "quota-model", time.Now()); blocked {
					t.Fatal("successful retry still blocked")
				}
				long := 5 * 24 * time.Hour
				m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: model, RetryAfter: &long, Error: &Error{HTTPStatus: 429}})
				if _, err := m.scheduler.pickSingle(context.Background(), auth.Provider, "quota-model", cliproxyexecutor.Options{}, nil); err == nil {
					t.Fatal("fresh quota failure did not re-block scheduler")
				}
			})
		}
	}
}

func TestQuotaCooldownLimit_RestorePreservesNonQuota(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, code := range []int{http.StatusNotFound, http.StatusServiceUnavailable} {
		m, auth := newCooldownMonotonicManager(t, "quota-model")
		m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 3600})
		now := time.Now()
		deadline := now.Add(12 * time.Hour)
		store := &recordingCooldownStateStore{load: []CooldownStateRecord{{AuthID: auth.ID, Model: "quota-model",
			UpdatedAt: now.Add(-2 * time.Hour), NextRetryAfter: deadline,
			Quota: QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline}, LastError: &Error{HTTPStatus: code}}}}
		m.SetCooldownStateStore(store)
		if err := m.RestoreCooldownStates(context.Background()); err != nil {
			t.Fatal(err)
		}
		got, _ := m.GetByID(auth.ID)
		if !got.ModelStates["quota-model"].NextRetryAfter.Equal(deadline) {
			t.Fatal("clamped ambiguous legacy non-quota deadline")
		}
	}
}

func TestQuotaCooldownLimit_ReloadExpiredUpdatesScheduler(t *testing.T) {
	withQuotaCooldownEnabled(t)
	m, auth := newCooldownMonotonicManager(t, "quota-model")
	now := time.Now()
	deadline := now.Add(5 * 24 * time.Hour)
	store := &recordingCooldownStateStore{load: []CooldownStateRecord{{AuthID: auth.ID, Model: "quota-model",
		UpdatedAt: now.Add(-2 * time.Hour), NextRetryAfter: deadline,
		Quota: QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline}, LastError: &Error{HTTPStatus: 429}}}}
	m.SetCooldownStateStore(store)
	if err := m.RestoreCooldownStates(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := m.scheduler.pickSingle(context.Background(), auth.Provider, "quota-model", cliproxyexecutor.Options{}, nil); err == nil {
		t.Fatal("precondition: restored quota must block selection")
	}
	m.mu.Lock()
	m.auths[auth.ID].Quota.ObservedAt = now
	m.auths[auth.ID].Quota.Signals = map[string]string{"usage": "50"}
	m.mu.Unlock()
	m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 3600, SaveCooldownStatus: true})
	if _, err := m.scheduler.pickSingle(context.Background(), auth.Provider, "quota-model", cliproxyexecutor.Options{}, nil); err != nil {
		t.Fatalf("reload did not update scheduler: %v", err)
	}
	got, _ := m.GetByID(auth.ID)
	if !got.Quota.ObservedAt.Equal(now) || got.Quota.Signals["usage"] != "50" {
		t.Fatal("reload changed passive quota observation")
	}
	if len(store.savedRecords()) != 0 {
		t.Fatal("expired quota was persisted again")
	}
}

func TestQuotaCooldownLimit_PreservesCloudflareAndHomePolicy(t *testing.T) {
	withQuotaCooldownEnabled(t)
	m, auth := newCooldownMonotonicManager(t, "quota-model")
	now := time.Now()
	deadline := now.Add(time.Hour)
	store := &recordingCooldownStateStore{load: []CooldownStateRecord{{AuthID: auth.ID, Model: "quota-model",
		UpdatedAt: now, NextRetryAfter: deadline,
		Quota: QuotaState{Exceeded: true, Reason: "cloudflare challenge", NextRecoverAt: deadline}, LastError: &Error{HTTPStatus: 429}}}}
	m.SetCooldownStateStore(store)
	m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 60})
	if err := m.RestoreCooldownStates(context.Background()); err != nil {
		t.Fatal(err)
	}
	got, _ := m.GetByID(auth.ID)
	if !got.ModelStates["quota-model"].NextRetryAfter.Equal(deadline) {
		t.Fatal("quota cap shortened Cloudflare challenge")
	}
	cfg := &internalconfig.Config{QuotaCooldownMaxSeconds: 60}
	cfg.Home.Enabled = true
	m.SetConfig(cfg)
	long := time.Hour
	m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: "quota-model", RetryAfter: &long, Error: &Error{HTTPStatus: 429}})
	got, _ = m.GetByID(auth.ID)
	if blocked, _, _ := isAuthBlockedForModel(got, "quota-model", time.Now()); blocked {
		t.Fatal("local quota cap introduced cooling in Home mode")
	}
}

func TestQuotaCooldownLimit_RestartWithDifferentModelDeadlines(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, reloadFirst := range []bool{false, true} {
		for _, age := range []time.Duration{30 * time.Minute, 2 * time.Hour} {
			name := "direct restore/" + age.String()
			if reloadFirst {
				name = "reload then restore/" + age.String()
			}
			t.Run(name, func(t *testing.T) {
				m, auth := newCooldownMonotonicManager(t, "model-a", "model-b")
				store := &recordingCooldownStateStore{}
				m.SetCooldownStateStore(store)
				for i, model := range []string{"model-a", "model-b"} {
					duration := time.Duration(5-i) * 24 * time.Hour
					m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: model, RetryAfter: &duration, Error: &Error{HTTPStatus: 429}})
				}
				observed := time.Now().Add(-age)
				m.mu.Lock()
				current := m.auths[auth.ID]
				current.UpdatedAt = observed
				for _, state := range current.ModelStates {
					state.UpdatedAt = observed
				}
				equalDeadlines := current.NextRetryAfter.Equal(current.Quota.NextRecoverAt)
				m.mu.Unlock()
				if equalDeadlines {
					t.Fatal("precondition: aggregate deadlines must differ")
				}
				m.persistCooldownStates(context.Background())
				cfg := &internalconfig.Config{QuotaCooldownMaxSeconds: 3600, SaveCooldownStatus: true}
				if reloadFirst {
					m.SetConfig(cfg)
				}
				restored, _ := newCooldownMonotonicManager(t, "model-a", "model-b")
				restored.SetConfig(cfg)
				restored.SetCooldownStateStore(&recordingCooldownStateStore{load: store.savedRecords()})
				if err := restored.RestoreCooldownStates(context.Background()); err != nil {
					t.Fatal(err)
				}
				got, _ := restored.GetByID(auth.ID)
				for _, model := range []string{"", "model-a", "model-b"} {
					if blocked, _, _ := isAuthBlockedForModel(got, model, observed.Add(time.Hour+time.Second)); blocked {
						t.Fatalf("%q blocked after capped deadline", model)
					}
				}
				if age > time.Hour {
					for _, model := range []string{"model-a", "model-b"} {
						if _, err := restored.scheduler.pickSingle(context.Background(), auth.Provider, model, cliproxyexecutor.Options{}, nil); err != nil {
							t.Fatalf("restart blocks %s: %v", model, err)
						}
					}
				} else if blocked, _, _ := isAuthBlockedForModel(got, "model-a", time.Now()); !blocked {
					t.Fatal("recent quota expired too early")
				}
			})
		}
	}
}

func TestQuotaCooldownLimit_ReloadPreservesAuthFailureWithModelState(t *testing.T) {
	withQuotaCooldownEnabled(t)
	m, auth := newCooldownMonotonicManager(t, "quota-model")
	long := 5 * 24 * time.Hour
	m.MarkResult(context.Background(), Result{AuthID: auth.ID, Model: "quota-model", RetryAfter: &long, Error: &Error{HTTPStatus: 429}})
	m.MarkResult(context.Background(), Result{AuthID: auth.ID, Error: &Error{HTTPStatus: 503}})
	before, _ := m.GetByID(auth.ID)
	m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 3600})
	after, _ := m.GetByID(auth.ID)
	if !after.NextRetryAfter.Equal(before.NextRetryAfter) || !after.Unavailable || after.LastError.HTTPStatus != 503 {
		t.Fatal("recomputing model aggregate discarded the separate auth failure")
	}
}

func TestQuotaCooldownLimit_RestoreExpiredWrapperPreservesIndependentFailure(t *testing.T) {
	withQuotaCooldownEnabled(t)
	m, auth := newCooldownMonotonicManager(t, "missing-model")
	m.SetConfig(&internalconfig.Config{QuotaCooldownMaxSeconds: 3600, SaveCooldownStatus: true})
	now := time.Now()
	independentDeadline := now.Add(10 * time.Hour)
	quotaDeadline := now.Add(5 * 24 * time.Hour)
	store := &recordingCooldownStateStore{load: []CooldownStateRecord{{
		AuthID: auth.ID, Model: "missing-model", UpdatedAt: now.Add(-2 * time.Hour),
		NextRetryAfter: quotaDeadline, LastError: &Error{HTTPStatus: 429},
		Quota:                     QuotaState{Exceeded: true, Reason: "credential_quota", NextRecoverAt: quotaDeadline},
		CredentialQuotaPropagated: true,
		IndependentState: &ModelState{Unavailable: true, Status: StatusError,
			NextRetryAfter: independentDeadline, LastError: &Error{HTTPStatus: 404},
			UpdatedAt: now.Add(-3 * time.Hour)},
	}}}
	m.SetCooldownStateStore(store)
	if err := m.RestoreCooldownStates(context.Background()); err != nil {
		t.Fatal(err)
	}
	got, _ := m.GetByID(auth.ID)
	if blocked, _, _ := isAuthBlockedForModel(got, "missing-model", now); !blocked {
		t.Fatal("expired credential quota discarded a live independent 404")
	}
	state := got.ModelStates["missing-model"]
	if state.LastError == nil || state.LastError.HTTPStatus != 404 || !state.NextRetryAfter.Equal(independentDeadline) {
		t.Fatalf("independent failure changed: %+v", state)
	}
}

func TestQuotaCooldownLimit_BoundsIndependentQuotaAcrossRestoreAndReload(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, reload := range []bool{false, true} {
		t.Run(map[bool]string{false: "restore", true: "reload"}[reload], func(t *testing.T) {
			m, auth := newCooldownMonotonicManager(t, "quota-model")
			cfg := &internalconfig.Config{QuotaCooldownMaxSeconds: 3600, SaveCooldownStatus: true}
			if !reload {
				m.SetConfig(cfg)
			}
			now := time.Now()
			shadowTime := now.Add(-30 * time.Minute)
			deadline := now.Add(5 * 24 * time.Hour)
			store := &recordingCooldownStateStore{load: []CooldownStateRecord{{
				AuthID: auth.ID, Model: "quota-model", UpdatedAt: now,
				NextRetryAfter: deadline, LastError: &Error{HTTPStatus: 429},
				Quota:                     QuotaState{Exceeded: true, Reason: "credential_quota", NextRecoverAt: deadline},
				CredentialQuotaPropagated: true,
				IndependentState: &ModelState{Unavailable: true, Status: StatusError,
					NextRetryAfter: deadline, LastError: &Error{HTTPStatus: 429}, UpdatedAt: shadowTime,
					Quota: QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline}},
			}}}
			m.SetCooldownStateStore(store)
			if err := m.RestoreCooldownStates(context.Background()); err != nil {
				t.Fatal(err)
			}
			if reload {
				m.SetConfig(cfg)
			}
			got, _ := m.GetByID(auth.ID)
			shadow := got.ModelStates["quota-model"].credentialQuotaShadow
			if shadow == nil || !shadow.NextRetryAfter.Equal(shadowTime.Add(time.Hour)) {
				t.Fatalf("independent quota did not keep its original bounded deadline: %+v", shadow)
			}
			if blocked, _, _ := isAuthBlockedForModel(got, "quota-model", now.Add(2*time.Hour)); blocked {
				t.Fatal("expired independent quota still blocked the model")
			}
		})
	}
}
