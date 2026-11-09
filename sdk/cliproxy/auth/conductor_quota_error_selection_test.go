package auth

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

// soonerRetryExecutor rejects every attempt with a quota 429. Only the first
// credential tried recovers soon, so the last-tried error is the latest reset.
type soonerRetryExecutor struct {
	identifier string
	calls      atomic.Int32
}

func (e *soonerRetryExecutor) Identifier() string { return e.identifier }

func (e *soonerRetryExecutor) Execute(ctx context.Context, _ *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, e.nextError(ctx)
}

func (e *soonerRetryExecutor) ExecuteStream(ctx context.Context, _ *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	return nil, e.nextError(ctx)
}

func (*soonerRetryExecutor) Refresh(_ context.Context, auth *Auth) (*Auth, error) {
	return auth, nil
}

func (e *soonerRetryExecutor) CountTokens(ctx context.Context, _ *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, e.nextError(ctx)
}

func (*soonerRetryExecutor) HttpRequest(context.Context, *Auth, *http.Request) (*http.Response, error) {
	return nil, nil
}

func (e *soonerRetryExecutor) nextError(ctx context.Context) error {
	cliproxyexecutor.MarkUpstreamAttempt(ctx)
	wait := 30 * time.Hour
	if e.calls.Add(1) == 1 {
		wait = 2 * time.Hour
	}
	return &retryAfterStatusError{
		status:     http.StatusTooManyRequests,
		message:    fmt.Sprintf("usage limit reached, resets in %s", wait),
		retryAfter: wait,
	}
}

func TestExhaustedPoolReportsSoonestRetryableQuotaError(t *testing.T) {
	paths := []struct {
		name   string
		invoke func(*Manager, string, string) error
	}{
		{
			name: "execute",
			invoke: func(manager *Manager, provider, model string) error {
				_, errExecute := manager.Execute(context.Background(), []string{provider}, cliproxyexecutor.Request{Model: model}, cliproxyexecutor.Options{})
				return errExecute
			},
		},
		{
			name: "count-tokens",
			invoke: func(manager *Manager, provider, model string) error {
				_, errCount := manager.ExecuteCount(context.Background(), []string{provider}, cliproxyexecutor.Request{Model: model}, cliproxyexecutor.Options{})
				return errCount
			},
		},
		{
			name: "stream",
			invoke: func(manager *Manager, provider, model string) error {
				_, errStream := manager.ExecuteStream(context.Background(), []string{provider}, cliproxyexecutor.Request{Model: model}, cliproxyexecutor.Options{Stream: true})
				return errStream
			},
		},
	}

	for _, path := range paths {
		t.Run(path.name, func(t *testing.T) {
			provider := "sooner-retry-" + path.name
			model := provider + "-model"
			manager := NewManager(nil, nil, nil)
			manager.SetRetryConfig(0, 0, 0)
			executor := &soonerRetryExecutor{identifier: provider}
			manager.RegisterExecutor(executor)
			registerRetryRoundLocalAuths(t, manager, provider, model, map[string]int{
				provider + "-a": 0,
				provider + "-b": 0,
				provider + "-c": 0,
			})

			errInvoke := path.invoke(manager, provider, model)
			if got := executor.calls.Load(); got != 3 {
				t.Fatalf("executor calls = %d, want one attempt per credential", got)
			}
			if status := statusCodeFromError(errInvoke); status != http.StatusTooManyRequests {
				t.Fatalf("status = %d (%v), want 429", status, errInvoke)
			}
			var quotaErr *retryAfterStatusError
			if !errors.As(errInvoke, &quotaErr) || quotaErr.retryAfter != 2*time.Hour {
				t.Fatalf("client error = %v, want the credential that resets in 2h", errInvoke)
			}
		})
	}
}
