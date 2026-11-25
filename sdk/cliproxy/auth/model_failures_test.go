package auth

import (
	"context"
	"fmt"
	"net/http"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

const overloadedBody = `{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}`

func registerModelFailureAuth(t *testing.T, model string) (*Manager, *Auth) {
	t.Helper()
	manager := NewManager(nil, nil, nil)
	auth := &Auth{ID: "auth-claude-model-failures", Provider: "claude", Metadata: map[string]any{"email": "user@example.com"}}
	reg := registry.GetGlobalRegistry()
	reg.RegisterClient(auth.ID, "claude", []*registry.ModelInfo{{ID: model}})
	t.Cleanup(func() { reg.UnregisterClient(auth.ID) })
	if _, errRegister := manager.Register(WithSkipPersist(context.Background()), auth); errRegister != nil {
		t.Fatalf("Register returned error: %v", errRegister)
	}
	return manager, auth
}

func TestModelFailureRecordsOverloadAndSurvivesSuccess(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	before := time.Now()
	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{HTTPStatus: 529, Message: overloadedBody}})
	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model, Success: true})

	updated, _ := manager.GetByID(auth.ID)
	failure, ok := updated.ModelFailuresSnapshot()[model]
	if !ok {
		t.Fatalf("expected a recorded failure for %s, got %v", model, updated.ModelFailuresSnapshot())
	}
	if failure.HTTPStatus != 529 || failure.Type != "overloaded_error" {
		t.Fatalf("unexpected failure record: %+v", failure)
	}
	if failure.At.Before(before) || failure.At.After(time.Now()) {
		t.Fatalf("failure time %v outside the test window", failure.At)
	}
}

func TestModelFailureRecordsStreamedOverloadAsBadGateway(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{HTTPStatus: http.StatusBadGateway, Message: overloadedBody}})

	updated, _ := manager.GetByID(auth.ID)
	if got := updated.ModelFailuresSnapshot()[model].HTTPStatus; got != http.StatusBadGateway {
		t.Fatalf("expected the 502 failure to be recorded, got status %d", got)
	}
}

func TestModelFailureIgnoresFailuresWithoutUpstreamStatus(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{Message: "context canceled"}})

	updated, _ := manager.GetByID(auth.ID)
	if failures := updated.ModelFailuresSnapshot(); len(failures) != 0 {
		t.Fatalf("a failure with no upstream status must not be recorded, got %v", failures)
	}
}

func TestModelFailureKeepsOnlyStructuredFields(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	secretBody := `{"type":"error","error":{"type":"invalid request: Bearer sk-secret","message":"token sk-secret"}}`
	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{HTTPStatus: http.StatusBadRequest, Message: secretBody}})

	updated, _ := manager.GetByID(auth.ID)
	failure := updated.ModelFailuresSnapshot()[model]
	if failure.HTTPStatus != http.StatusBadRequest {
		t.Fatalf("expected the 400 to be recorded, got %+v", failure)
	}
	if failure.Type != "" {
		t.Fatalf("a free-form error type must not be kept, got %q", failure.Type)
	}
}

// fastDirectError mirrors the Claude Fast direct response error: a generic
// message, Anthropic's body on the wrapped terminated response, request scoped.
type fastDirectError struct {
	response *cliproxyexecutor.RequestTerminatedError
}

func (e *fastDirectError) Error() string {
	return fmt.Sprintf("claude Fast upstream request failed with status %d", e.response.HTTPStatus)
}
func (e *fastDirectError) Unwrap() error         { return e.response }
func (e *fastDirectError) IsRequestScoped() bool { return true }

type fastFailureExecutor struct{}

func (fastFailureExecutor) Identifier() string { return "claude" }
func (fastFailureExecutor) Execute(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, &fastDirectError{response: &cliproxyexecutor.RequestTerminatedError{
		HTTPStatus: 529, Body: []byte(overloadedBody)}}
}
func (fastFailureExecutor) ExecuteStream(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	return nil, fmt.Errorf("not used")
}
func (fastFailureExecutor) Refresh(_ context.Context, auth *Auth) (*Auth, error) { return auth, nil }
func (fastFailureExecutor) CountTokens(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, fmt.Errorf("not used")
}
func (fastFailureExecutor) HttpRequest(context.Context, *Auth, *http.Request) (*http.Response, error) {
	return nil, fmt.Errorf("not used")
}

func TestModelFailureReadsTypeFromFastDirectResponseBody(t *testing.T) {
	model := "claude-opus-5-5"
	manager, auth := registerModelFailureAuth(t, model)
	manager.RegisterExecutor(fastFailureExecutor{})

	if _, errExecute := manager.Execute(context.Background(), []string{"claude"},
		cliproxyexecutor.Request{Model: model}, cliproxyexecutor.Options{}); errExecute == nil {
		t.Fatal("expected the Fast failure to reach the caller")
	}

	updated, _ := manager.GetByID(auth.ID)
	failure := updated.ModelFailuresSnapshot()[model]
	if failure.HTTPStatus != 529 || failure.Type != "overloaded_error" {
		t.Fatalf("expected a 529 overloaded_error from the Fast body, got %+v", failure)
	}
}

func TestUpstreamErrorTypeReadsWrappedBodies(t *testing.T) {
	cases := map[string]string{
		overloadedBody:                          "overloaded_error",
		"upstream said: " + overloadedBody:      "overloaded_error",
		`{"error":{"type":"rate_limit_error"}}`: "rate_limit_error",
		"not json":                              "",
		`{"error":{"type":"Has Spaces"}}`:       "",
		`{"error":"flat string"}`:               "",
	}
	for body, want := range cases {
		if got := upstreamErrorType([]byte(body)); got != want {
			t.Errorf("upstreamErrorType(%q) = %q, want %q", body, got, want)
		}
	}
}

func TestModelFailureSurvivesCredentialUpdate(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{HTTPStatus: 529, Message: overloadedBody}})
	refreshed, _ := manager.GetByID(auth.ID)
	refreshed.Metadata["access_token"] = "rotated"
	if _, errUpdate := manager.Update(WithSkipPersist(context.Background()), refreshed); errUpdate != nil {
		t.Fatalf("Update returned error: %v", errUpdate)
	}

	updated, _ := manager.GetByID(auth.ID)
	if _, ok := updated.ModelFailuresSnapshot()[model]; !ok {
		t.Fatal("a credential update must keep the recorded failure")
	}
}

func TestModelFailureSnapshotIsACopy(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{HTTPStatus: 529, Message: overloadedBody}})
	first, _ := manager.GetByID(auth.ID)
	snapshot := first.ModelFailuresSnapshot()
	delete(snapshot, model)

	second, _ := manager.GetByID(auth.ID)
	if _, ok := second.ModelFailuresSnapshot()[model]; !ok {
		t.Fatal("changing a snapshot must not change the manager's record")
	}
}
