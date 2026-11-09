package auth

import (
	"context"
	"errors"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

// scriptedFailureExecutor fails the Nth attempt with the Nth scripted error,
// whichever credential the manager picked for it.
type scriptedFailureExecutor struct {
	identifier string
	script     []error
	calls      atomic.Int32
}

func (e *scriptedFailureExecutor) Identifier() string { return e.identifier }

func (e *scriptedFailureExecutor) Execute(ctx context.Context, _ *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, e.nextError(ctx)
}

func (e *scriptedFailureExecutor) ExecuteStream(ctx context.Context, _ *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	return nil, e.nextError(ctx)
}

func (*scriptedFailureExecutor) Refresh(_ context.Context, auth *Auth) (*Auth, error) {
	return auth, nil
}

func (e *scriptedFailureExecutor) CountTokens(ctx context.Context, _ *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, e.nextError(ctx)
}

func (*scriptedFailureExecutor) HttpRequest(context.Context, *Auth, *http.Request) (*http.Response, error) {
	return nil, nil
}

func (e *scriptedFailureExecutor) nextError(ctx context.Context) error {
	cliproxyexecutor.MarkUpstreamAttempt(ctx)
	call := int(e.calls.Add(1)) - 1
	if call >= len(e.script) {
		return &Error{HTTPStatus: http.StatusInternalServerError, Message: "unexpected extra attempt"}
	}
	return e.script[call]
}

func quotaRejection(message string, wait time.Duration) error {
	return &retryAfterStatusError{status: http.StatusTooManyRequests, message: message, retryAfter: wait}
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
	serverFailure := &Error{HTTPStatus: http.StatusInternalServerError, Message: "upstream server failure"}
	scenarios := []struct {
		name    string
		script  []error
		wantMsg string
	}{
		{
			name:    "soonest tried first",
			script:  []error{quotaRejection("resets in 2h", 2*time.Hour), quotaRejection("resets in 30h", 30*time.Hour), quotaRejection("resets in 31h", 31*time.Hour)},
			wantMsg: "resets in 2h",
		},
		{
			name:    "other failure between quota rejections",
			script:  []error{quotaRejection("resets in 2h", 2*time.Hour), serverFailure, quotaRejection("resets in 30h", 30*time.Hour)},
			wantMsg: "resets in 2h",
		},
		{
			name:    "newest failure is not a quota rejection",
			script:  []error{quotaRejection("resets in 2h", 2*time.Hour), quotaRejection("resets in 30h", 30*time.Hour), serverFailure},
			wantMsg: serverFailure.Message,
		},
	}

	for _, path := range paths {
		for _, scenario := range scenarios {
			t.Run(path.name+"/"+scenario.name, func(t *testing.T) {
				provider := "sooner-retry-" + path.name
				model := provider + "-model"
				manager := NewManager(nil, nil, nil)
				manager.SetRetryConfig(0, 0, 0)
				executor := &scriptedFailureExecutor{identifier: provider, script: scenario.script}
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
				if errInvoke == nil || errInvoke.Error() != scenario.wantMsg {
					t.Fatalf("client error = %v, want %q", errInvoke, scenario.wantMsg)
				}
				var quotaErr *retryAfterStatusError
				if errors.As(errInvoke, &quotaErr) {
					if hint := retryAfterFromError(errInvoke); hint == nil || *hint > quotaErr.retryAfter {
						t.Fatalf("retry hint = %v, want at most %s from the reported credential", hint, quotaErr.retryAfter)
					}
				}
			})
		}
	}
}
