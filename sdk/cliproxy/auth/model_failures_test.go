package auth

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
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
	if failure.HTTPStatus != 529 || failure.Message != overloadedBody {
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

func TestModelFailureTruncatesLongMessages(t *testing.T) {
	model := "claude-fable-5-2"
	manager, auth := registerModelFailureAuth(t, model)

	manager.MarkResult(context.Background(), Result{AuthID: auth.ID, Provider: "claude", Model: model,
		Error: &Error{HTTPStatus: http.StatusInternalServerError, Message: strings.Repeat("é", 400)}})

	updated, _ := manager.GetByID(auth.ID)
	message := updated.ModelFailuresSnapshot()[model].Message
	if len([]rune(message)) != modelFailureMessageLimit {
		t.Fatalf("expected %d runes, got %d", modelFailureMessageLimit, len([]rune(message)))
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
