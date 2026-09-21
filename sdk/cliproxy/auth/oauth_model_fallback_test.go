package auth

import (
	"context"
	"net/http"
	"sync"
	"testing"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
)

type oauthModelFallbackExecutor struct {
	mu     sync.Mutex
	models []string
}

func (e *oauthModelFallbackExecutor) Identifier() string { return "kimi" }

func (e *oauthModelFallbackExecutor) Execute(_ context.Context, _ *Auth, req cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	e.record(req.Model)
	if req.Model == "kimi-k3" {
		return cliproxyexecutor.Response{}, &Error{HTTPStatus: http.StatusTooManyRequests, Message: "K3 quota"}
	}
	return cliproxyexecutor.Response{Payload: []byte(req.Model)}, nil
}

func (e *oauthModelFallbackExecutor) ExecuteStream(_ context.Context, _ *Auth, req cliproxyexecutor.Request, _ cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	e.record(req.Model)
	if req.Model == "kimi-k3" {
		return nil, &Error{HTTPStatus: http.StatusServiceUnavailable, Message: "K3 unavailable"}
	}
	chunks := make(chan cliproxyexecutor.StreamChunk, 1)
	chunks <- cliproxyexecutor.StreamChunk{Payload: []byte(req.Model)}
	close(chunks)
	return &cliproxyexecutor.StreamResult{Chunks: chunks}, nil
}

func (e *oauthModelFallbackExecutor) Refresh(_ context.Context, auth *Auth) (*Auth, error) {
	return auth, nil
}

func (e *oauthModelFallbackExecutor) CountTokens(_ context.Context, _ *Auth, req cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	e.record(req.Model)
	if req.Model == "kimi-k3" {
		return cliproxyexecutor.Response{}, &Error{HTTPStatus: http.StatusTooManyRequests, Message: "K3 quota"}
	}
	return cliproxyexecutor.Response{Payload: []byte(req.Model)}, nil
}

func (*oauthModelFallbackExecutor) HttpRequest(context.Context, *Auth, *http.Request) (*http.Response, error) {
	return nil, nil
}

func (e *oauthModelFallbackExecutor) record(model string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.models = append(e.models, model)
}

func (e *oauthModelFallbackExecutor) Models() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]string(nil), e.models...)
}

func newOAuthFallbackManager(t *testing.T, executor *oauthModelFallbackExecutor) *Manager {
	t.Helper()
	manager := NewManager(nil, nil, nil)
	manager.RegisterExecutor(executor)
	manager.SetRetryConfig(0, 0, 1)
	manager.SetOAuthModelAlias(map[string][]internalconfig.OAuthModelAlias{
		"kimi": {{
			Name:     "kimi-k3",
			Alias:    "kimi-default",
			Fallback: []string{"kimi-k2.8"},
		}},
	})
	if _, errRegister := manager.Register(context.Background(), &Auth{ID: "kimi-fallback-auth", Provider: "kimi", Status: StatusActive}); errRegister != nil {
		t.Fatalf("register auth: %v", errRegister)
	}
	registryRef := registry.GetGlobalRegistry()
	registryRef.RegisterClient("kimi-fallback-auth", "kimi", []*registry.ModelInfo{{ID: "kimi-default"}, {ID: "kimi-k3"}, {ID: "kimi-k2.8"}})
	t.Cleanup(func() { registryRef.UnregisterClient("kimi-fallback-auth") })
	manager.RefreshSchedulerEntry("kimi-fallback-auth")
	return manager
}

func TestManagerExecute_OAuthModelFallbackOnTransientError(t *testing.T) {
	executor := &oauthModelFallbackExecutor{}
	manager := newOAuthFallbackManager(t, executor)

	resp, errExecute := manager.Execute(context.Background(), []string{"kimi"}, cliproxyexecutor.Request{Model: "kimi-default"}, cliproxyexecutor.Options{})
	if errExecute != nil {
		t.Fatalf("execute error = %v", errExecute)
	}
	if string(resp.Payload) != "kimi-k2.8" {
		t.Fatalf("response model = %q, want kimi-k2.8", string(resp.Payload))
	}
	got := executor.Models()
	if len(got) != 2 || got[0] != "kimi-k3" || got[1] != "kimi-k2.8" {
		t.Fatalf("models = %#v, want [kimi-k3 kimi-k2.8]", got)
	}
}

func TestManagerExecuteStream_OAuthModelFallbackOnTransientError(t *testing.T) {
	executor := &oauthModelFallbackExecutor{}
	manager := newOAuthFallbackManager(t, executor)

	result, errExecute := manager.ExecuteStream(context.Background(), []string{"kimi"}, cliproxyexecutor.Request{Model: "kimi-default"}, cliproxyexecutor.Options{Stream: true})
	if errExecute != nil {
		t.Fatalf("execute stream error = %v", errExecute)
	}
	var payloads []string
	for chunk := range result.Chunks {
		if chunk.Err != nil {
			t.Fatalf("stream chunk error = %v", chunk.Err)
		}
		payloads = append(payloads, string(chunk.Payload))
	}
	if len(payloads) != 1 || payloads[0] != "kimi-k2.8" {
		t.Fatalf("stream payloads = %#v, want [kimi-k2.8]", payloads)
	}
	got := executor.Models()
	if len(got) != 2 || got[0] != "kimi-k3" || got[1] != "kimi-k2.8" {
		t.Fatalf("stream models = %#v, want [kimi-k3 kimi-k2.8]", got)
	}
}

func TestShouldAttemptOAuthModelFallback_RejectsAuthAndRequestErrors(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want bool
	}{
		{name: "unauthorized", err: &Error{HTTPStatus: http.StatusUnauthorized, Message: "expired"}},
		{name: "invalid request", err: &Error{HTTPStatus: http.StatusBadRequest, Message: "invalid_request_error"}},
		{name: "quota", err: &Error{HTTPStatus: http.StatusTooManyRequests, Message: "quota"}, want: true},
		{name: "unsupported model", err: &Error{HTTPStatus: http.StatusBadRequest, Message: "model_not_supported"}, want: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := shouldAttemptOAuthModelFallback(tt.err); got != tt.want {
				t.Fatalf("shouldAttemptOAuthModelFallback() = %t, want %t", got, tt.want)
			}
		})
	}
}
