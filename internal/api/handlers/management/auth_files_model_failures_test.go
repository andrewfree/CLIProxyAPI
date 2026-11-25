package management

import (
	"context"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
)

func TestListAuthFilesReportsModelFailuresOnV8Only(t *testing.T) {
	t.Setenv("MANAGEMENT_PASSWORD", "")
	const model = "claude-fable-5-2"
	const body = `{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}`

	manager := coreauth.NewManager(nil, nil, nil)
	record := &coreauth.Auth{
		ID:         "claude-model-failures",
		Provider:   "claude",
		Attributes: map[string]string{"runtime_only": "true"},
		Metadata:   map[string]any{"type": "claude"},
	}
	quiet := &coreauth.Auth{
		ID:         "claude-no-failures",
		Provider:   "claude",
		Attributes: map[string]string{"runtime_only": "true"},
		Metadata:   map[string]any{"type": "claude"},
	}
	for _, auth := range []*coreauth.Auth{record, quiet} {
		if _, errRegister := manager.Register(coreauth.WithSkipPersist(context.Background()), auth); errRegister != nil {
			t.Fatalf("Register returned error: %v", errRegister)
		}
	}
	manager.MarkResult(context.Background(), coreauth.Result{AuthID: record.ID, Provider: "claude", Model: model,
		Error: &coreauth.Error{HTTPStatus: 529, Message: body}})
	manager.MarkResult(context.Background(), coreauth.Result{AuthID: record.ID, Provider: "claude", Model: model, Success: true})

	h := NewHandlerWithoutConfigFilePath(&config.Config{AuthDir: t.TempDir()}, manager)
	h.tokenStore = &memoryAuthStore{}
	router := oauthProviderTestRouter(h)

	v8Files := listOAuthProviderTestFiles(t, router, "/v8/management/credentials")
	if got := v8Files[quiet.ID].Get("model_failures"); !got.IsObject() || len(got.Map()) != 0 {
		t.Fatalf("a credential with no failures must report an empty object, got %s", v8Files[quiet.ID].Raw)
	}
	v8 := v8Files[record.ID]
	failure := v8.Get("model_failures." + model)
	if !failure.Exists() {
		t.Fatalf("v8 entry has no failure for %s: %s", model, v8.Raw)
	}
	if got := failure.Get("http_status").Int(); got != 529 {
		t.Fatalf("http_status = %d, want 529", got)
	}
	if got := failure.Get("type").String(); got != "overloaded_error" {
		t.Fatalf("type = %q, want overloaded_error", got)
	}
	if failure.Get("message").Exists() {
		t.Fatalf("the upstream message must not be exposed: %s", failure.Raw)
	}
	if failure.Get("at").String() == "" {
		t.Fatal("failure has no time")
	}

	v0 := listOAuthProviderTestFiles(t, router, "/v0/management/auth-files")[record.ID]
	if v0.Get("model_failures").Exists() {
		t.Fatalf("v0 must not report model failures: %s", v0.Raw)
	}
}
