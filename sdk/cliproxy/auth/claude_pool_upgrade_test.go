package auth

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/logging"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	exec "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
)

const (
	upgradeModel   = "claude-opus-5-5"
	upgradeSibling = "claude-fable-5-2"
	upgradePro     = "default_claude_pro"
	upgradeMax     = "default_claude_max_5x"
	upgradeReply   = `{"type":"message","role":"assistant","content":[{"type":"text","text":"client reply"}],"stop_reason":"end_turn"}`
	upgradeStop    = "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
)

type upgradeExecutor struct {
	customStreamMockExecutor
	tier         string
	profileErr   error
	tierForID    map[string]string
	profileCalls atomic.Int32
	onProfile    func()
}

func (e *upgradeExecutor) ClaudeOAuthRateLimitTier(_ context.Context, auth *Auth) (string, error) {
	e.profileCalls.Add(1)
	if e.onProfile != nil {
		e.onProfile()
	}
	if tier, ok := e.tierForID[auth.ID]; ok {
		return tier, e.profileErr
	}
	return e.tier, e.profileErr
}

func upgradeFixture(t *testing.T, baseline string) (*Manager, *upgradeExecutor, string) {
	t.Helper()
	m := NewManager(nil, nil, nil)
	m.SetRetryConfig(0, 0, 0)
	e := &upgradeExecutor{tier: upgradeMax}
	m.RegisterExecutor(e)
	a := &Auth{
		ID: t.Name(), Provider: "claude", Status: StatusActive,
		Attributes: map[string]string{"auth_kind": "oauth"},
		Metadata: map[string]any{
			"type": "claude", "access_token": "test-access",
			"account_uuid": "test-account", "organization_uuid": "test-org",
			"claude_rate_limit_tier": baseline,
		},
	}
	quotaManagerTestRegister(t, m, a, upgradeModel, upgradeSibling)
	return m, e, a.ID
}

func markUpgradeQuota(m *Manager, id, model string, credentialScope bool) {
	ctx := logging.WithFreshResponseHeadersHolder(context.Background())
	logging.SetResponseHeaders(ctx, http.Header{"Anthropic-Ratelimit-Unified-Status": {"rejected"}})
	wait := time.Hour
	m.MarkResult(ctx, Result{AuthID: id, Provider: "claude", Model: model, CredentialScope: credentialScope, Error: &Error{HTTPStatus: 429, Message: "provider quota"}, RetryAfter: &wait})
}

func allowedUpgradeHeaders() http.Header {
	return http.Header{"Anthropic-Ratelimit-Unified-Status": {"allowed"}}
}

func TestClaudePoolUpgradeUsesRealRequest(t *testing.T) {
	for _, stream := range []bool{false, true} {
		name := "messages"
		if stream {
			name = "stream"
		}
		t.Run(name, func(t *testing.T) {
			m, e, id := upgradeFixture(t, upgradePro)
			markUpgradeQuota(m, id, upgradeSibling, false)
			markUpgradeQuota(m, id, upgradeModel, true)
			requestBody := `{"messages":[{"role":"user","content":"original question"}],"max_tokens":917,"stream":` + strconv.FormatBool(stream) + `}`
			req := exec.Request{Model: upgradeModel, Payload: []byte(requestBody)}
			opts := exec.Options{Stream: stream, SourceFormat: translator.FormatClaude, OriginalRequest: req.Payload, Headers: http.Header{"X-Request-Id": {"client-request-id"}}}
			calls := 0
			check := func(ctx context.Context, a *Auth, got exec.Request, gotOpts exec.Options) {
				calls++
				if a.ID != id || got.Model != upgradeModel {
					t.Errorf("selected %q %q, want %q %q", a.ID, got.Model, id, upgradeModel)
				}
				if string(got.Payload) != requestBody || string(gotOpts.OriginalRequest) != requestBody {
					t.Fatal("client payload changed")
				}
				if gotOpts.Stream != stream || gotOpts.SourceFormat != opts.SourceFormat || gotOpts.Headers.Get("X-Request-Id") != "client-request-id" {
					t.Fatal("client request framing changed")
				}
				logging.SetResponseHeaders(ctx, allowedUpgradeHeaders())
			}
			e.executeFn = func(ctx context.Context, a *Auth, got exec.Request, gotOpts exec.Options) (exec.Response, error) {
				check(ctx, a, got, gotOpts)
				return exec.Response{Payload: []byte(upgradeReply)}, nil
			}
			e.streamFn = func(ctx context.Context, a *Auth, got exec.Request, gotOpts exec.Options) (*exec.StreamResult, error) {
				check(ctx, a, got, gotOpts)
				chunks := make(chan exec.StreamChunk, 1)
				chunks <- exec.StreamChunk{Payload: []byte(upgradeStop)}
				close(chunks)
				return &exec.StreamResult{Chunks: chunks, Headers: allowedUpgradeHeaders()}, nil
			}
			if stream {
				res, err := m.ExecuteStream(context.Background(), []string{"claude"}, req, opts)
				if err != nil {
					t.Fatal(err)
				}
				var body string
				for chunk := range res.Chunks {
					if chunk.Err != nil {
						t.Fatal(chunk.Err)
					}
					body += string(chunk.Payload)
				}
				if body != upgradeStop {
					t.Fatalf("stream bytes = %q", body)
				}
			} else {
				res, err := m.Execute(context.Background(), []string{"claude"}, req, opts)
				if err != nil {
					t.Fatal(err)
				}
				if string(res.Payload) != upgradeReply {
					t.Fatalf("response = %s", res.Payload)
				}
			}
			if e.profileCalls.Load() != 1 || calls != 1 {
				t.Fatalf("profile calls = %d, generation calls = %d", e.profileCalls.Load(), calls)
			}
			a, _ := m.GetByID(id)
			if a.Quota.Reason == "credential_quota" || !a.InferenceVerified {
				t.Fatal("completed request with allowed quota did not recover credential")
			}
			if blocked, _, _ := isAuthBlockedForModel(a, upgradeSibling, time.Now()); !blocked || !registry.GetGlobalRegistry().IsModelSuspendedForClient(id, upgradeSibling) {
				t.Fatal("recovery erased the independent sibling cooldown")
			}
			if _, err := m.SelectAuth(context.Background(), "claude", upgradeModel, exec.Options{}); err != nil {
				t.Fatalf("normal routing after recovery: %v", err)
			}
			// A second rejection on Max 5x describes a new quota window at the new tier.
			markUpgradeQuota(m, id, upgradeModel, true)
			if _, err := m.Execute(context.Background(), []string{"claude"}, req, opts); statusCodeFromError(err) != 429 || calls != 1 {
				t.Fatalf("the same upgrade admitted another request: calls %d, error %v", calls, err)
			}
			cooled, _ := m.GetByID(id)
			if tier := cooled.Metadata["claude_rate_limit_tier"]; tier != upgradeMax {
				t.Errorf("tier recorded for subsequent cooldowns = %q, want %q", tier, upgradeMax)
			}
		})
	}
}

func TestClaudePoolUpgradeRequiresConfirmedIncrease(t *testing.T) {
	for _, tt := range []struct {
		name, baseline, newTier string
		err                     error
	}{
		{"same", upgradePro, upgradePro, nil},
		{"downgrade", upgradeMax, upgradePro, nil},
		{"unknown tier", upgradePro, "future-tier", nil},
		{"legacy cooldown", "", upgradeMax, nil},
		{"identity lookup failed", upgradePro, "", errors.New("profile mismatch")},
	} {
		t.Run(tt.name, func(t *testing.T) {
			m, e, id := upgradeFixture(t, tt.baseline)
			e.tier = tt.newTier
			e.profileErr = tt.err
			markUpgradeQuota(m, id, upgradeModel, true)
			e.executeFn = func(context.Context, *Auth, exec.Request, exec.Options) (exec.Response, error) {
				t.Fatal("inference attempted without a confirmed increase")
				return exec.Response{}, nil
			}
			for i := 0; i < 2; i++ {
				_, err := m.Execute(context.Background(), []string{"claude"}, exec.Request{Model: upgradeModel}, exec.Options{})
				if statusCodeFromError(err) != 429 {
					t.Fatalf("pool error = %v, want original 429", err)
				}
			}
			if n := e.profileCalls.Load(); n > 1 || (tt.baseline != "" && n != 1) {
				t.Fatalf("profile calls = %d; lookups must be bounded across requests", n)
			}
			a, _ := m.GetByID(id)
			if a.Quota.Reason != "credential_quota" || !a.Quota.Exceeded {
				t.Fatal("unconfirmed increase erased cooldown")
			}
		})
	}
}

func TestClaudePoolUpgradeRequiresCompletedAllowedGeneration(t *testing.T) {
	for _, tt := range []struct {
		name    string
		headers http.Header
		body    string
		err     error
	}{
		{"no quota headers", nil, upgradeReply, nil},
		{"rejected window", http.Header{"Anthropic-Ratelimit-Unified-Status": {"allowed"}, "Anthropic-Ratelimit-Unified-7d-Status": {"rejected"}}, upgradeReply, nil},
		{"incomplete", allowedUpgradeHeaders(), `{"type":"message","stop_reason":null}`, nil},
		{"provider rejection", http.Header{"Anthropic-Ratelimit-Unified-Status": {"rejected"}}, "", streamQuotaError{customStatusError: customStatusError{code: 429, msg: "provider still rejects quota"}, credentialScoped: true}},
	} {
		t.Run(tt.name, func(t *testing.T) {
			m, e, id := upgradeFixture(t, upgradePro)
			markUpgradeQuota(m, id, upgradeModel, true)
			calls := 0
			e.executeFn = func(ctx context.Context, _ *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
				calls++
				logging.SetResponseHeaders(ctx, tt.headers)
				return exec.Response{Payload: []byte(tt.body)}, tt.err
			}
			_, err := m.Execute(context.Background(), []string{"claude"}, exec.Request{Model: upgradeModel}, exec.Options{})
			if tt.err == nil && err != nil {
				t.Fatal(err)
			}
			if tt.err != nil && statusCodeFromError(err) != 429 {
				t.Fatalf("provider error = %v", err)
			}
			if calls != 1 {
				t.Fatalf("generation calls = %d", calls)
			}
			a, _ := m.GetByID(id)
			if a.Quota.Reason != "credential_quota" || !a.Quota.Exceeded || a.InferenceVerified {
				t.Fatal("unproven response erased cooldown")
			}
			_, err = m.Execute(context.Background(), []string{"claude"}, exec.Request{Model: upgradeModel}, exec.Options{})
			if statusCodeFromError(err) != 429 || calls != 1 || e.profileCalls.Load() != 1 {
				t.Fatalf("repeated same-tier attempt: client error %v, generations %d, profiles %d", err, calls, e.profileCalls.Load())
			}
		})
	}
}

func TestClaudePoolUpgradeConcurrentRequests(t *testing.T) {
	m, e, id := upgradeFixture(t, upgradePro)
	markUpgradeQuota(m, id, upgradeModel, true)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	profileStarted, releaseProfile := make(chan struct{}, 1), make(chan struct{})
	generationStarted, releaseGeneration := make(chan struct{}, 1), make(chan struct{})
	e.onProfile = func() {
		select {
		case profileStarted <- struct{}{}:
		default:
		}
		select {
		case <-releaseProfile:
		case <-ctx.Done():
		}
	}
	var generations atomic.Int32
	e.executeFn = func(attemptCtx context.Context, _ *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
		generations.Add(1)
		select {
		case generationStarted <- struct{}{}:
		default:
		}
		select {
		case <-releaseGeneration:
		case <-ctx.Done():
			return exec.Response{}, ctx.Err()
		}
		logging.SetResponseHeaders(attemptCtx, allowedUpgradeHeaders())
		return exec.Response{Payload: []byte(upgradeReply)}, nil
	}
	run := func() <-chan error {
		done := make(chan error, 1)
		go func() {
			_, err := m.Execute(ctx, []string{"claude"}, exec.Request{Model: upgradeModel}, exec.Options{})
			done <- err
		}()
		return done
	}
	timeout := time.After(5 * time.Second)
	first := run()
	awaitPhase := func(phase chan struct{}) {
		t.Helper()
		select {
		case <-phase:
		case err := <-first:
			t.Fatalf("original request exited before phase: %v", err)
		case <-timeout:
			t.Fatal("timed out waiting for original request")
		}
	}
	assertOtherCooled := func() {
		t.Helper()
		select {
		case err := <-run():
			if statusCodeFromError(err) != 429 {
				t.Fatalf("concurrent client error = %v, want 429", err)
			}
		case <-timeout:
			t.Fatal("concurrent request stalled")
		}
	}
	awaitPhase(profileStarted)
	assertOtherCooled()
	close(releaseProfile)
	awaitPhase(generationStarted)
	assertOtherCooled()
	close(releaseGeneration)
	select {
	case err := <-first:
		if err != nil {
			t.Fatal(err)
		}
	case <-timeout:
		t.Fatal("original request stalled")
	}
	if n := e.profileCalls.Load(); n != 1 || generations.Load() != 1 {
		t.Fatalf("profiles=%d, client executions=%d, want one of each", n, generations.Load())
	}
}

func TestClaudePoolUpgradeDoesNotTransferProofAcrossChanges(t *testing.T) {
	for _, phase := range []string{"profile", "generation"} {
		t.Run(phase, func(t *testing.T) {
			m, e, id := upgradeFixture(t, upgradePro)
			markUpgradeQuota(m, id, upgradeModel, true)
			// Record a fresh provider restriction between taking the snapshot and using it.
			change := func() {
				m.MarkResult(context.Background(), Result{AuthID: id, Provider: "claude", Model: upgradeSibling, Error: &Error{HTTPStatus: 503, Message: "new independent failure"}})
			}
			executions := 0
			e.executeFn = func(ctx context.Context, _ *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
				executions++
				if phase == "generation" {
					change()
				}
				logging.SetResponseHeaders(ctx, allowedUpgradeHeaders())
				return exec.Response{Payload: []byte(upgradeReply)}, nil
			}
			if phase == "profile" {
				e.onProfile = change
			}
			_, err := m.Execute(context.Background(), []string{"claude"}, exec.Request{Model: upgradeModel}, exec.Options{})
			if phase == "profile" {
				if statusCodeFromError(err) != 429 || executions != 0 {
					t.Fatalf("stale profile dispatched generation %d, error %v", executions, err)
				}
			} else if err != nil || executions != 1 {
				t.Fatalf("client execution count %d, error %v", executions, err)
			}
			a, _ := m.GetByID(id)
			if !a.Quota.Exceeded || a.Quota.Reason != "credential_quota" || a.ModelStates[upgradeSibling] == nil {
				t.Fatal("a stale result cleared a new restriction")
			}
		})
	}
}

func TestClaudePoolUpgradeOnlyOnSharedGeneration(t *testing.T) {
	for _, which := range []string{"healthy route", "pinned", "count", "selection", "generation disabled", "prefixed", "independently blocked target"} {
		t.Run(which, func(t *testing.T) {
			m, e, id := upgradeFixture(t, upgradePro)
			model := upgradeModel
			if which == "independently blocked target" {
				markUpgradeQuota(m, id, model, false)
			}
			markUpgradeQuota(m, id, upgradeModel, true)
			if which == "healthy route" {
				healthy := &Auth{ID: id + "-healthy", Provider: "claude", Status: StatusActive, Attributes: map[string]string{"auth_kind": "oauth"}}
				quotaManagerTestRegister(t, m, healthy, model)
			}
			opts := exec.Options{}
			if which == "pinned" {
				opts.Metadata = map[string]any{exec.PinnedAuthMetadataKey: id}
			}
			if which == "generation disabled" {
				opts.Metadata = map[string]any{exec.GenerateMetadataKey: false}
			}
			if which == "prefixed" {
				model = "account/" + model
			}
			calls := 0
			e.executeFn = func(_ context.Context, a *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
				calls++
				if a.ID == id {
					t.Error("dispatched a cooled credential on ineligible route")
				}
				return exec.Response{Payload: []byte(upgradeReply)}, nil
			}
			ctx := context.Background()
			var err error
			switch which {
			case "count":
				_, err = m.ExecuteCount(ctx, []string{"claude"}, exec.Request{Model: model}, opts)
			case "selection":
				_, err = m.SelectAuth(ctx, "claude", model, opts)
			default:
				_, err = m.Execute(ctx, []string{"claude"}, exec.Request{Model: model}, opts)
			}
			if which == "healthy route" {
				if err != nil || calls != 1 {
					t.Fatalf("healthy route: calls=%d, err=%v", calls, err)
				}
			} else if err == nil || calls != 0 {
				t.Fatalf("blocked route: calls=%d, err=%v", calls, err)
			}
			if e.profileCalls.Load() != 0 {
				t.Fatalf("unexpected profile checks: %d", e.profileCalls.Load())
			}
		})
	}
}

func TestClaudePoolUpgradeTruncatedStreamPreservesQuota(t *testing.T) {
	m, e, id := upgradeFixture(t, upgradePro)
	markUpgradeQuota(m, id, upgradeModel, true)
	partial := `data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"message_stop"}}` + "\n\n"
	e.streamFn = func(ctx context.Context, _ *Auth, _ exec.Request, _ exec.Options) (*exec.StreamResult, error) {
		logging.SetResponseHeaders(ctx, allowedUpgradeHeaders())
		chunks := make(chan exec.StreamChunk, 1)
		chunks <- exec.StreamChunk{Payload: []byte(partial)}
		close(chunks)
		return &exec.StreamResult{Chunks: chunks, Headers: allowedUpgradeHeaders()}, nil
	}
	ctx := context.Background()
	res, err := m.ExecuteStream(ctx, []string{"claude"}, exec.Request{Model: upgradeModel}, exec.Options{Stream: true, SourceFormat: translator.FormatClaude})
	if err != nil {
		t.Fatal(err)
	}
	var body string
	for chunk := range res.Chunks {
		body += string(chunk.Payload)
	}
	if body != partial {
		t.Fatalf("client stream = %q", body)
	}
	a, _ := m.GetByID(id)
	if !a.Quota.Exceeded || a.Quota.Reason != "credential_quota" || a.InferenceVerified {
		t.Fatal("stream without terminal event cleared credential quota")
	}
}

func TestClaudePoolUpgradeRestoredCooldown(t *testing.T) {
	for _, tt := range []struct {
		name, baseline                                  string
		replaceIdentity, providerRejected, wantRecovery bool
	}{
		{name: "matching identity with refreshed metadata", baseline: upgradePro, wantRecovery: true},
		{name: "replacement organization", baseline: upgradePro, replaceIdentity: true},
		{name: "legacy record with no baseline"},
		{name: "provider still rejected at new tier", baseline: upgradePro, providerRejected: true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			ctx := context.Background()
			store := NewFileCooldownStateStore(t.TempDir())
			old, oldExec, id := upgradeFixture(t, tt.baseline)
			old.SetCooldownStateStore(store)
			markUpgradeQuota(old, id, upgradeModel, true)
			req := exec.Request{Model: upgradeModel}
			if tt.providerRejected {
				oldExec.executeFn = func(ctx context.Context, _ *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
					logging.SetResponseHeaders(ctx, http.Header{"Anthropic-Ratelimit-Unified-Status": {"rejected"}})
					return exec.Response{}, streamQuotaError{customStatusError: customStatusError{code: 429, msg: "still capped"}, credentialScoped: true}
				}
				if _, err := old.Execute(ctx, []string{"claude"}, req, exec.Options{}); statusCodeFromError(err) != 429 || oldExec.profileCalls.Load() != 1 {
					t.Fatalf("upgraded credential rejection = %v, profile calls %d", err, oldExec.profileCalls.Load())
				}
			}
			// A normal OAuth refresh can have learned the new tier before restart. The cooldown's snapshot is the source of its old tier.
			current, e, _ := upgradeFixture(t, upgradeMax)
			if tt.replaceIdentity {
				a, _ := current.GetByID(id)
				a.Metadata["organization_uuid"] = "different-organization"
				if _, err := current.Update(ctx, a); err != nil {
					t.Fatal(err)
				}
			}
			current.SetCooldownStateStore(store)
			if err := current.RestoreCooldownStates(ctx); err != nil {
				t.Fatal(err)
			}
			cooled, _ := current.GetByID(id)
			if !cooled.Quota.Exceeded || cooled.Quota.Reason != "credential_quota" {
				t.Fatal("missing restored credential cooldown")
			}
			generations := 0
			e.executeFn = func(ctx context.Context, _ *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
				generations++
				logging.SetResponseHeaders(ctx, allowedUpgradeHeaders())
				return exec.Response{Payload: []byte(upgradeReply)}, nil
			}
			_, err := current.Execute(ctx, []string{"claude"}, req, exec.Options{})
			if tt.wantRecovery {
				if err != nil || generations != 1 {
					t.Fatalf("expected recovery from old persisted tier; executions %d, error %v", generations, err)
				}
			} else if statusCodeFromError(err) != 429 || generations != 0 {
				t.Fatalf("inadmissible restored proof dispatched %d generations, error %v", generations, err)
			}
		})
	}
}

func TestClaudePoolUpgradeWithQuotaAwareAffinity(t *testing.T) {
	m, e, alpha := upgradeFixture(t, upgradePro)
	selector := NewSessionAffinitySelector(NewQuotaAwareSelector(QuotaAwareSelectorOptions{}))
	t.Cleanup(selector.Stop)
	m.SetSelector(selector)
	m.SetPluginScheduler(&inactivePluginScheduler{})
	beta := alpha + "-beta"
	betaAuth, _ := m.GetByID(alpha)
	betaAuth.ID = beta
	betaAuth.Metadata["account_uuid"] = "upgraded-account"
	betaAuth.Metadata["organization_uuid"] = "upgraded-org"
	quotaManagerTestRegister(t, m, betaAuth, upgradeModel)
	markUpgradeQuota(m, alpha, upgradeModel, true)
	markUpgradeQuota(m, beta, upgradeModel, true)
	e.tierForID = map[string]string{alpha: upgradePro, beta: upgradeMax}
	var calls []string
	e.executeFn = func(ctx context.Context, a *Auth, _ exec.Request, _ exec.Options) (exec.Response, error) {
		calls = append(calls, a.ID)
		logging.SetResponseHeaders(ctx, allowedUpgradeHeaders())
		return exec.Response{Payload: []byte(upgradeReply)}, nil
	}
	opts := exec.Options{Headers: http.Header{"X-Session-Id": {"shared-session"}}}
	_, err := m.Execute(context.Background(), []string{"claude"}, exec.Request{Model: upgradeModel}, opts)
	if err != nil || len(calls) != 1 || calls[0] != beta {
		t.Fatalf("with one upgrade in the pool, selected %v, error %v", calls, err)
	}
	if checks := e.profileCalls.Load(); checks < 1 || checks > 2 {
		t.Fatalf("checked %d profiles in two-account pool", checks)
	}
	unchanged, _ := m.GetByID(alpha)
	if unchanged.Quota.Reason != "credential_quota" || !unchanged.Quota.Exceeded {
		t.Fatal("recovery changed unchanged account")
	}
}
