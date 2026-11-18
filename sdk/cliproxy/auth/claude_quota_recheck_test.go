package auth

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/MoeForge/moego/ojson"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/logging"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
)

type claudeQuotaVerifyExecutor struct {
	unauthorizedRefreshExecutor
	headers     http.Header
	during      func()
	lastPayload []byte
	lastOptions cliproxyexecutor.Options
	terminal    []byte
}

func (e *claudeQuotaVerifyExecutor) ExecuteStream(ctx context.Context, _ *Auth, req cliproxyexecutor.Request, opts cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	e.lastPayload = req.Payload
	e.lastOptions = opts
	logging.SetResponseHeaders(ctx, e.headers)
	if e.during != nil {
		e.during()
	}
	chunks := make(chan cliproxyexecutor.StreamChunk, 1)
	terminal := e.terminal
	if len(terminal) == 0 {
		terminal = []byte(`data: {"type":"response.completed","response":{"status":"completed"}}`)
	}
	chunks <- cliproxyexecutor.StreamChunk{Payload: terminal}
	close(chunks)
	return &cliproxyexecutor.StreamResult{Chunks: chunks}, nil
}

func claudeQuotaVerifyFixture(t *testing.T) (*Manager, *claudeQuotaVerifyExecutor, string, time.Time) {
	t.Helper()
	m := NewManager(nil, nil, nil)
	exec := &claudeQuotaVerifyExecutor{unauthorizedRefreshExecutor: unauthorizedRefreshExecutor{id: "claude"},
		headers: http.Header{"Anthropic-Ratelimit-Unified-Status": {"allowed"}}}
	m.RegisterExecutor(exec)
	deadline := time.Now().Add(time.Hour)
	auth := &Auth{ID: t.Name(), Provider: "claude", Status: StatusError, Unavailable: true,
		NextRetryAfter: deadline, Quota: QuotaState{Exceeded: true, Reason: "credential_quota", NextRecoverAt: deadline},
		Metadata: map[string]any{"access_token": "test-token"}, ModelStates: map[string]*ModelState{
			"claude-opus-5-5": {Status: StatusError, Unavailable: true, NextRetryAfter: deadline,
				Quota: QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline}, credentialQuotaPropagated: true},
			"claude-fable-5-2": {Status: StatusError, Unavailable: true, NextRetryAfter: deadline,
				Quota: QuotaState{Exceeded: true, Reason: "credential_quota", NextRecoverAt: deadline}, credentialQuotaPropagated: true,
				credentialQuotaShadow: &ModelState{Status: StatusError, Unavailable: true, NextRetryAfter: deadline.Add(time.Hour),
					Quota: QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline.Add(time.Hour)}}},
			"claude-haiku-5-5": {Status: StatusError, Unavailable: true, NextRetryAfter: deadline,
				Quota: QuotaState{Exceeded: true, Reason: "credential_quota", NextRecoverAt: deadline}, credentialQuotaPropagated: true},
		}}
	reg := registry.GetGlobalRegistry()
	reg.RegisterClient(auth.ID, "claude", []*registry.ModelInfo{{ID: "claude-opus-5-5"}, {ID: "claude-fable-5-2"}, {ID: "claude-haiku-5-5"}})
	t.Cleanup(func() { reg.UnregisterClient(auth.ID) })
	if _, errRegister := m.Register(context.Background(), auth); errRegister != nil {
		t.Fatal(errRegister)
	}
	return m, exec, auth.ID, deadline
}

func TestClaudeQuotaVerifyReleasesCredentialButPreservesIndependentSibling(t *testing.T) {
	m, exec, id, deadline := claudeQuotaVerifyFixture(t)
	updated, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5")
	if errVerify != nil {
		t.Fatalf("completed verification must release the stale credential quota: %v", errVerify)
	}
	if updated.Quota.Reason == "credential_quota" || !updated.InferenceVerified {
		t.Fatalf("verified credential quota remains blocked: %+v", updated.Quota)
	}
	if state := updated.ModelStates["claude-opus-5-5"]; state.Unavailable || state.Quota.Exceeded {
		t.Fatal("verified model remains blocked")
	}
	if state := updated.ModelStates["claude-haiku-5-5"]; state.Unavailable || state.Quota.Exceeded || state.credentialQuotaPropagated {
		t.Fatal("sibling blocked only by credential propagation remains blocked")
	}
	if exec.lastOptions.SourceFormat != sdktranslator.FormatClaude || ojson.GetBytes(exec.lastPayload, "max_tokens").Int() != 32 {
		t.Fatal("Claude verification must use a small native Messages request")
	}
	if !ojson.GetBytes(exec.lastPayload, "stream").Bool() {
		t.Fatal("Claude streaming verification must explicitly request an SSE response")
	}
	sibling := updated.ModelStates["claude-fable-5-2"]
	if !sibling.Unavailable || sibling.Quota.Reason != "quota" || !sibling.NextRetryAfter.Equal(deadline.Add(time.Hour)) {
		t.Fatalf("independent sibling restriction was not preserved: %+v", sibling)
	}
}

func TestClaudeQuotaVerifyDoesNotReleaseWithoutAllowedSignal(t *testing.T) {
	for _, status := range []string{"", "rejected"} {
		t.Run(status, func(t *testing.T) {
			m, exec, id, _ := claudeQuotaVerifyFixture(t)
			exec.headers.Set("Anthropic-Ratelimit-Unified-Status", status)
			_, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5")
			if errVerify == nil {
				t.Fatal("verification without allowed quota signal must fail")
			}
			updated, _ := m.GetByID(id)
			if !updated.Quota.Exceeded || updated.InferenceVerified {
				t.Fatal("unproven cooldown was released")
			}
		})
	}
}

func TestClaudeQuotaVerifyDoesNotEraseRestrictionRecordedDuringProbe(t *testing.T) {
	m, exec, id, _ := claudeQuotaVerifyFixture(t)
	started := make(chan struct{})
	release := make(chan struct{})
	exec.during = func() {
		close(started)
		<-release
	}
	done := make(chan error, 1)
	go func() {
		_, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5")
		done <- errVerify
	}()
	<-started
	m.MarkResult(context.Background(), Result{AuthID: id, Provider: "claude", Model: "claude-fable-5-2",
		Error: &Error{Message: "new failure", HTTPStatus: 503}})
	close(release)
	if errVerify := <-done; errVerify == nil {
		t.Fatal("changed credential generation must prevent recovery")
	}
	updated, _ := m.GetByID(id)
	if !updated.Quota.Exceeded || updated.ModelStates["claude-fable-5-2"].credentialQuotaShadow == nil {
		t.Fatal("new restriction or prior quota was erased")
	}
}

func TestClaudeQuotaVerifyDoesNotRecoverAfterCredentialReplacement(t *testing.T) {
	m, exec, id, _ := claudeQuotaVerifyFixture(t)
	exec.during = func() {
		replacement, _ := m.GetByID(id)
		replacement.Metadata["access_token"] = "replacement-token"
		if _, errUpdate := m.Update(context.Background(), replacement); errUpdate != nil {
			t.Fatal(errUpdate)
		}
	}
	if _, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5"); errVerify == nil {
		t.Fatal("replaced credential must not inherit verification")
	}
	updated, _ := m.GetByID(id)
	if !updated.Quota.Exceeded || updated.InferenceVerified {
		t.Fatal("replacement inherited recovery from an obsolete token")
	}
}

func TestClaudeQuotaOrdinarySuccessCannotReleaseCredentialQuota(t *testing.T) {
	m, _, id, _ := claudeQuotaVerifyFixture(t)
	ctx := logging.WithFreshResponseHeadersHolder(context.Background())
	logging.SetResponseHeaders(ctx, http.Header{"Anthropic-Ratelimit-Unified-Status": {"allowed"}})
	m.MarkResult(ctx, Result{AuthID: id, Provider: "claude", Model: "claude-opus-5-5", Success: true, CompletedResponse: true})
	updated, _ := m.GetByID(id)
	if updated.Quota.Reason != "credential_quota" || !updated.Quota.Exceeded {
		t.Fatal("ordinary request changed explicit quota recovery policy")
	}
}

func TestClaudeQuotaVerifyPreservesDisabledTargetShadow(t *testing.T) {
	m, _, id, _ := claudeQuotaVerifyFixture(t)
	m.mu.Lock()
	m.auths[id].ModelStates["claude-opus-5-5"].credentialQuotaShadow = &ModelState{Status: StatusDisabled, Unavailable: true}
	m.mu.Unlock()
	if _, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5"); errVerify == nil {
		t.Fatal("verification must not release a disabled model hidden beneath quota propagation")
	}
	updated, _ := m.GetByID(id)
	if !updated.Quota.Exceeded || updated.ModelStates["claude-opus-5-5"].credentialQuotaShadow.Status != StatusDisabled {
		t.Fatal("disabled model state was erased")
	}
}

func TestClaudeQuotaVerifyPreservesDisabledSiblingShadow(t *testing.T) {
	m, _, id, _ := claudeQuotaVerifyFixture(t)
	m.mu.Lock()
	m.auths[id].ModelStates["claude-fable-5-2"].credentialQuotaShadow = &ModelState{Status: StatusDisabled, Unavailable: true}
	m.mu.Unlock()
	updated, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5")
	if errVerify != nil {
		t.Fatal(errVerify)
	}
	if updated.ModelStates["claude-fable-5-2"].Status != StatusDisabled {
		t.Fatal("recovery enabled an independently disabled sibling")
	}
}

func TestClaudeQuotaVerifyOverallAllowedDoesNotOverrideRejectedWindow(t *testing.T) {
	for _, window := range []string{"5h", "7d", "7d_oi"} {
		t.Run(window, func(t *testing.T) {
			m, exec, id, _ := claudeQuotaVerifyFixture(t)
			exec.headers.Set("Anthropic-Ratelimit-Unified-"+window+"-Status", "rejected")
			if _, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5"); errVerify == nil {
				t.Fatal("rejected window must prevent early quota recovery")
			}
			updated, _ := m.GetByID(id)
			if updated.Quota.Reason != "credential_quota" || !updated.Quota.Exceeded {
				t.Fatal("overall allowed signal erased a rejected window")
			}
		})
	}
}

func TestClaudeQuotaVerifyRefreshesSchedulerAndRegistry(t *testing.T) {
	m, _, id, _ := claudeQuotaVerifyFixture(t)
	ctx := context.Background()
	reg := registry.GetGlobalRegistry()
	reg.SetModelQuotaExceeded(id, "claude-opus-5-5")
	reg.SuspendClientModel(id, "claude-opus-5-5", "quota")
	if _, errPick := m.scheduler.pickSingle(ctx, "claude", "claude-opus-5-5", cliproxyexecutor.Options{}, nil); errPick == nil {
		t.Fatal("scheduler must reject account before quota recovery")
	}
	if _, errVerify := m.VerifyInference(ctx, id, "claude-opus-5-5"); errVerify != nil {
		t.Fatal(errVerify)
	}
	for _, model := range []string{"claude-opus-5-5", "claude-haiku-5-5"} {
		picked, errPick := m.scheduler.pickSingle(ctx, "claude", model, cliproxyexecutor.Options{}, nil)
		if errPick != nil || picked == nil || picked.ID != id {
			t.Fatalf("scheduler did not release %s after recovery: %v", model, errPick)
		}
		if reg.IsModelSuspendedForClient(id, model) || reg.IsModelQuotaExceededForClient(id, model) {
			t.Fatalf("registry still blocks recovered model %s", model)
		}
	}
	if _, errPick := m.scheduler.pickSingle(ctx, "claude", "claude-fable-5-2", cliproxyexecutor.Options{}, nil); errPick == nil {
		t.Fatal("scheduler released independently blocked sibling")
	}
	if !reg.IsModelSuspendedForClient(id, "claude-fable-5-2") {
		t.Fatal("registry released independently blocked sibling")
	}
}

func TestClaudeQuotaVerifyAcceptsNativeMessageStop(t *testing.T) {
	m, exec, id, _ := claudeQuotaVerifyFixture(t)
	exec.terminal = []byte("event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
	if _, errVerify := m.VerifyInference(context.Background(), id, "claude-opus-5-5"); errVerify != nil {
		t.Fatalf("native Claude completion did not recover quota: %v", errVerify)
	}
}
