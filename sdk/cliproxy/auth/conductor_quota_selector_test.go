package auth

import (
	"context"
	"errors"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	internallogging "github.com/router-for-me/CLIProxyAPI/v8/internal/logging"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func quotaManagerTestHeaders(usedPercent string) http.Header {
	return http.Header{
		"X-Codex-Primary-Used-Percent":   []string{usedPercent},
		"X-Codex-Primary-Window-Minutes": []string{"300"},
		"X-Codex-Primary-Reset-At":       []string{strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10)},
	}
}

func quotaManagerTestObservation(usedPercent string) QuotaState {
	var quota QuotaState
	quota.ObserveResponseHeadersForProvider("codex", quotaManagerTestHeaders(usedPercent), time.Now())
	return quota
}

func quotaManagerTestAuth(t *testing.T, label string) *Auth {
	t.Helper()
	return &Auth{ID: label + "-" + t.Name(), Provider: "codex", Status: StatusActive}
}

func quotaManagerTestRegister(t *testing.T, manager *Manager, candidate *Auth, models ...string) {
	t.Helper()
	infos := make([]*registry.ModelInfo, len(models))
	for index, model := range models {
		infos[index] = &registry.ModelInfo{ID: model}
	}
	registry.GetGlobalRegistry().RegisterClient(candidate.ID, candidate.Provider, infos)
	t.Cleanup(func() { registry.GetGlobalRegistry().UnregisterClient(candidate.ID) })
	if _, errRegister := manager.Register(context.Background(), candidate); errRegister != nil {
		t.Fatalf("Register(%s): %v", candidate.ID, errRegister)
	}
}

func quotaManagerTestExecutor(execute func(context.Context, *Auth, cliproxyexecutor.Request, cliproxyexecutor.Options) (cliproxyexecutor.Response, error)) ProviderExecutor {
	return &customStreamMockExecutor{
		identifier: "codex",
		mockCustomErrorExecutor: mockCustomErrorExecutor{
			executeFn: execute,
			countFn:   execute,
		},
		streamFn: func(ctx context.Context, selected *Auth, req cliproxyexecutor.Request, opts cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
			response, errExecute := execute(ctx, selected, req, opts)
			if errExecute != nil {
				return nil, errExecute
			}
			chunks := make(chan cliproxyexecutor.StreamChunk, 1)
			chunks <- cliproxyexecutor.StreamChunk{Payload: response.Payload}
			close(chunks)
			return &cliproxyexecutor.StreamResult{Headers: response.Headers, Chunks: chunks}, nil
		},
	}
}

func quotaManagerTestRun(manager *Manager, mode, model string, opts cliproxyexecutor.Options) (string, error) {
	ctx := context.Background()
	req := cliproxyexecutor.Request{Model: model}
	switch mode {
	case "select":
		selected, errSelect := manager.SelectAuth(ctx, "codex", model, opts)
		if errSelect != nil {
			return "", errSelect
		}
		return selected.ID, nil
	case "execute":
		response, errExecute := manager.Execute(ctx, []string{"codex"}, req, opts)
		return string(response.Payload), errExecute
	case "count":
		response, errCount := manager.ExecuteCount(ctx, []string{"codex"}, req, opts)
		return string(response.Payload), errCount
	default:
		opts.Stream = true
		result, errStream := manager.ExecuteStream(ctx, []string{"codex"}, req, opts)
		if errStream != nil {
			return "", errStream
		}
		var payload strings.Builder
		for chunk := range result.Chunks {
			if chunk.Err != nil {
				return payload.String(), chunk.Err
			}
			payload.Write(chunk.Payload)
		}
		return payload.String(), nil
	}
}

func TestManagerQuotaAwareForecastReachesAllExecutionPaths(t *testing.T) {
	for _, mode := range []string{"select", "execute", "count", "stream"} {
		t.Run(mode, func(t *testing.T) {
			manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
			manager.SetRetryConfig(0, 0, 0)
			if manager.useSchedulerFastPath() {
				t.Fatal("quota-aware selection must reach the custom selector")
			}
			model := "quota-model-" + t.Name()
			exhausted, healthy := quotaManagerTestAuth(t, "a-exhausted"), quotaManagerTestAuth(t, "z-unknown")
			exhausted.Quota = quotaManagerTestObservation("100")
			quotaManagerTestRegister(t, manager, exhausted, model)
			quotaManagerTestRegister(t, manager, healthy, model)
			manager.RegisterExecutor(quotaManagerTestExecutor(func(_ context.Context, selected *Auth, req cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
				if req.Model != model {
					t.Errorf("executor model = %q, want exact requested model %q", req.Model, model)
				}
				return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
			}))
			// Ordinary round robin would select the lexically first exhausted
			// credential. Unknown observations must remain usable on every path.
			for pick := 0; pick < 3; pick++ {
				got, errPick := quotaManagerTestRun(manager, mode, model, cliproxyexecutor.Options{})
				if errPick != nil || got != healthy.ID {
					t.Fatalf("pick %d: auth=%q error=%v, want %q", pick, got, errPick, healthy.ID)
				}
			}
		})
	}
}

func TestManagerQuotaAwarePinnedAuthOverridesUrgentForecast(t *testing.T) {
	for _, mode := range []string{"select", "execute", "count", "stream"} {
		t.Run(mode, func(t *testing.T) {
			manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
			manager.SetRetryConfig(0, 0, 0)
			model := "quota-pinned-" + t.Name()
			urgent, pinned := quotaManagerTestAuth(t, "a-urgent"), quotaManagerTestAuth(t, "z-pinned")
			urgent.Quota, pinned.Quota = quotaManagerTestObservation("0"), quotaManagerTestObservation("100")
			quotaManagerTestRegister(t, manager, urgent, model)
			quotaManagerTestRegister(t, manager, pinned, model)
			manager.RegisterExecutor(quotaManagerTestExecutor(func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
				return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
			}))
			opts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: pinned.ID}}
			got, errPick := quotaManagerTestRun(manager, mode, model, opts)
			if errPick != nil || got != pinned.ID {
				t.Fatalf("pinned auth=%q error=%v, want %q despite zero forecast headroom", got, errPick, pinned.ID)
			}
		})
	}
}

func TestManagerQuotaAwarePreservesEligibility(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, path := range []string{"legacy", "mixed"} {
		for _, reason := range []string{"disabled", "status-disabled", "unauthorized", "cooldown", "model-disabled", "tried", "unsupported", "lower-priority"} {
			t.Run(path+"/"+reason, func(t *testing.T) {
				manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
				manager.RegisterExecutor(&mockCustomErrorExecutor{identifier: "codex"})
				model := "quota-eligibility-" + t.Name()
				ineligible, exhausted, healthy := quotaManagerTestAuth(t, "a-ineligible"), quotaManagerTestAuth(t, "x-exhausted"), quotaManagerTestAuth(t, "z-unknown")
				ineligible.Quota, exhausted.Quota = quotaManagerTestObservation("0"), quotaManagerTestObservation("100")
				tried := map[string]struct{}{}
				registeredModel := model
				switch reason {
				case "disabled":
					ineligible.Disabled = true
				case "status-disabled":
					ineligible.Status = StatusDisabled
				case "unauthorized":
					ineligible.Status, ineligible.Unavailable = StatusError, true
					ineligible.LastError = &Error{HTTPStatus: http.StatusUnauthorized, Code: "unauthorized", Message: "sign in required"}
				case "cooldown":
					ineligible.ModelStates = map[string]*ModelState{model: {Status: StatusError, Unavailable: true, NextRetryAfter: time.Now().Add(time.Hour)}}
				case "model-disabled":
					ineligible.ModelStates = map[string]*ModelState{model: {Status: StatusDisabled}}
				case "tried":
					tried[ineligible.ID] = struct{}{}
				case "unsupported":
					registeredModel = model + "-other"
				case "lower-priority":
					ineligible.Attributes = map[string]string{"priority": "-1"}
				}
				quotaManagerTestRegister(t, manager, ineligible, registeredModel)
				quotaManagerTestRegister(t, manager, exhausted, model)
				quotaManagerTestRegister(t, manager, healthy, model)
				for pick := 0; pick < 3; pick++ {
					var selected *Auth
					var errPick error
					if path == "legacy" {
						selected, _, errPick = manager.pickNextLegacy(context.Background(), "codex", model, cliproxyexecutor.Options{}, tried)
					} else {
						selected, _, _, errPick = manager.pickNextMixed(context.Background(), []string{"codex"}, model, cliproxyexecutor.Options{}, tried)
					}
					if errPick != nil || selected == nil || selected.ID != healthy.ID {
						t.Fatalf("pick %d: auth=%+v error=%v, want eligible %q", pick, selected, errPick, healthy.ID)
					}
				}
				if reason != "tried" && reason != "lower-priority" {
					opts := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: ineligible.ID}}
					if selected, errPick := manager.SelectAuth(context.Background(), "codex", model, opts); errPick == nil {
						t.Fatalf("pin bypassed %s eligibility: selected %+v", reason, selected)
					}
				}
			})
		}
	}
}

func TestManagerQuotaAwareAliasCooldownAndPrefix(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, prefix := range []string{"", "team"} {
		for _, mode := range []string{"select", "execute", "count", "stream"} {
			t.Run(prefix+"/"+mode, func(t *testing.T) {
				manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
				manager.SetRetryConfig(0, 0, 0)
				alias := "quota-alias-" + strings.ReplaceAll(t.Name(), "/", "-")
				target := alias + "-upstream"
				route := alias
				if prefix != "" {
					route = prefix + "/" + alias
				}
				exhausted, healthy := quotaManagerTestAuth(t, "a-exhausted"), quotaManagerTestAuth(t, "z-unknown")
				exhausted.Quota = quotaManagerTestObservation("100")
				for _, candidate := range []*Auth{exhausted, healthy} {
					candidate.Prefix = prefix
					quotaManagerTestRegister(t, manager, candidate, route, alias, target)
					retryAfter := time.Hour
					for _, oldModel := range []string{alias, route} {
						manager.MarkResult(context.Background(), Result{
							AuthID: candidate.ID, Provider: "codex", Model: oldModel, RetryAfter: &retryAfter,
							Error: &Error{HTTPStatus: http.StatusTooManyRequests, Message: "old route quota"},
						})
					}
				}
				manager.SetOAuthModelAlias(map[string][]internalconfig.OAuthModelAlias{
					"codex": {{Name: target, Alias: alias, Fork: true}},
				})
				manager.RegisterExecutor(quotaManagerTestExecutor(func(_ context.Context, selected *Auth, req cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
					if selected.Prefix != prefix || req.Model != target {
						t.Errorf("upstream prefix/model = %q/%q, want %q/%q", selected.Prefix, req.Model, prefix, target)
					}
					return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
				}))
				if _, errPick := quotaManagerTestRun(manager, mode, route, cliproxyexecutor.Options{}); statusCodeFromError(errPick) != http.StatusTooManyRequests {
					t.Fatalf("migrated alias cooldown error=%v, want 429", errPick)
				}
				manager.MarkResult(context.Background(), Result{AuthID: healthy.ID, Provider: "codex", Model: target, Success: true})
				got, errPick := quotaManagerTestRun(manager, mode, route, cliproxyexecutor.Options{})
				if errPick != nil || got != healthy.ID {
					t.Fatalf("resolved healthy alias auth=%q error=%v, want %q", got, errPick, healthy.ID)
				}
			})
		}
	}
}

func TestManagerQuotaAwareUsesNewSharedObservations(t *testing.T) {
	manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
	manager.RegisterExecutor(&mockCustomErrorExecutor{identifier: "codex"})
	model := "quota-new-observations-" + t.Name()
	first, second := quotaManagerTestAuth(t, "a-initially-exhausted"), quotaManagerTestAuth(t, "z-initially-unknown")
	first.Quota = quotaManagerTestObservation("100")
	quotaManagerTestRegister(t, manager, first, model)
	quotaManagerTestRegister(t, manager, second, model)
	selected, errSelect := manager.SelectAuth(context.Background(), "codex", model, cliproxyexecutor.Options{})
	if errSelect != nil || selected.ID != second.ID {
		t.Fatalf("initial auth=%+v error=%v, want %s", selected, errSelect, second.ID)
	}
	for _, observation := range []struct{ id, used string }{{second.ID, "100"}, {first.ID, "0"}} {
		ctx := internallogging.WithResponseHeadersHolder(context.Background())
		internallogging.SetResponseHeaders(ctx, quotaManagerTestHeaders(observation.used))
		manager.MarkResult(ctx, Result{AuthID: observation.id, Provider: "codex", Model: model, Success: true})
	}
	for pick := 0; pick < 3; pick++ {
		selected, errSelect = manager.SelectAuth(context.Background(), "codex", model, cliproxyexecutor.Options{})
		if errSelect != nil || selected.ID != first.ID {
			t.Fatalf("updated pick %d: auth=%+v error=%v, want %s", pick, selected, errSelect, first.ID)
		}
	}
}

func TestManagerQuotaAwarePartitionsRotationByRequestedRoute(t *testing.T) {
	manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
	manager.RegisterExecutor(&mockCustomErrorExecutor{identifier: "codex"})
	model := "quota-route-partition-" + t.Name()
	prefixedModel := "team/" + model
	first, second := quotaManagerTestAuth(t, "a-first"), quotaManagerTestAuth(t, "z-second")
	for _, candidate := range []*Auth{first, second} {
		candidate.Prefix = "team"
		quotaManagerTestRegister(t, manager, candidate, model, prefixedModel)
	}
	// Both routes resolve to the same upstream model. Each requested route
	// must still retain its own rotation cursor, including its account prefix.
	for _, wantID := range []string{first.ID, second.ID} {
		for _, route := range []string{model, prefixedModel} {
			selected, errSelect := manager.SelectAuth(context.Background(), "codex", route, cliproxyexecutor.Options{})
			if errSelect != nil || selected.ID != wantID || selected.Prefix != "team" {
				t.Fatalf("route %q: auth=%+v error=%v, want %s with prefix team", route, selected, errSelect, wantID)
			}
		}
	}
}

func TestManagerQuotaAwareShadowMatchesRoundRobin(t *testing.T) {
	for _, routing := range []string{"separate-models", "same-upstream-aliases"} {
		for _, mode := range []string{"select", "execute", "count", "stream"} {
			t.Run(routing+"/"+mode, func(t *testing.T) {
				shadow := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true}), nil)
				baseline := NewManager(nil, &RoundRobinSelector{}, nil)
				name := strings.ReplaceAll(t.Name(), "/", "-")
				routes := []string{"quota-shadow-a-" + name, "quota-shadow-b-" + name}
				models := append([]string(nil), routes...)
				if routing == "same-upstream-aliases" {
					target := "quota-shadow-upstream-" + name
					models = append(models, target)
					aliases := map[string][]internalconfig.OAuthModelAlias{
						"codex": {{Name: target, Alias: routes[0], Fork: true}, {Name: target, Alias: routes[1], Fork: true}},
					}
					shadow.SetOAuthModelAlias(aliases)
					baseline.SetOAuthModelAlias(aliases)
				}
				first, second := quotaManagerTestAuth(t, "a-exhausted"), quotaManagerTestAuth(t, "z-unknown")
				first.Quota = quotaManagerTestObservation("100")
				for _, candidate := range []*Auth{first, second} {
					quotaManagerTestRegister(t, shadow, candidate, models...)
					if _, errRegister := baseline.Register(context.Background(), candidate.Clone()); errRegister != nil {
						t.Fatal(errRegister)
					}
				}
				for _, manager := range []*Manager{shadow, baseline} {
					manager.SetRetryConfig(0, 0, 0)
					manager.RegisterExecutor(quotaManagerTestExecutor(func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
						return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
					}))
				}
				t.Logf("ordinary scheduler fast path=%t; shadow scheduler fast path=%t", baseline.useSchedulerFastPath(), shadow.useSchedulerFastPath())
				for pick, step := range []struct {
					route  int
					pinned bool
				}{{0, false}, {1, false}, {0, true}, {0, false}, {1, false}, {0, false}, {1, false}} {
					opts := cliproxyexecutor.Options{}
					if step.pinned {
						opts.Metadata = map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: second.ID}
					}
					actual, errActual := quotaManagerTestRun(shadow, mode, routes[step.route], opts)
					want, errBaseline := quotaManagerTestRun(baseline, mode, routes[step.route], opts)
					if errActual != nil || errBaseline != nil || actual != want {
						t.Errorf("pick %d route=%d pinned=%t: shadow=%q/%v round-robin=%q/%v", pick, step.route, step.pinned, actual, errActual, want, errBaseline)
					}
				}
			})
		}
	}
}

func TestManagerQuotaAwareRetryFallsBackToOnlyRemainingCandidate(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, mode := range []string{"execute", "count", "stream"} {
		t.Run(mode, func(t *testing.T) {
			manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
			manager.SetRetryConfig(0, 0, 0)
			model := "quota-retry-" + t.Name()
			fallback, preferred := quotaManagerTestAuth(t, "a-zero-fallback"), quotaManagerTestAuth(t, "z-unknown-preferred")
			fallback.Quota = quotaManagerTestObservation("100")
			quotaManagerTestRegister(t, manager, fallback, model)
			quotaManagerTestRegister(t, manager, preferred, model)
			var attempts []string
			manager.RegisterExecutor(quotaManagerTestExecutor(func(ctx context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
				attempts = append(attempts, selected.ID)
				cliproxyexecutor.MarkUpstreamAttempt(ctx)
				if selected.ID == preferred.ID {
					return cliproxyexecutor.Response{}, customStatusError{code: http.StatusServiceUnavailable, msg: "temporary upstream failure"}
				}
				return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
			}))
			got, errExecute := quotaManagerTestRun(manager, mode, model, cliproxyexecutor.Options{})
			if errExecute != nil || got != fallback.ID || !reflect.DeepEqual(attempts, []string{preferred.ID, fallback.ID}) {
				t.Fatalf("response=%q error=%v attempts=%v, want preferred then sole zero-forecast fallback", got, errExecute, attempts)
			}
		})
	}
}

func TestManagerQuotaAwareStartedStreamIsNotReplayed(t *testing.T) {
	withQuotaCooldownEnabled(t)
	manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
	manager.SetRetryConfig(0, 0, 0)
	model := "quota-started-stream-" + t.Name()
	exhausted, preferred := quotaManagerTestAuth(t, "a-exhausted"), quotaManagerTestAuth(t, "z-unknown")
	exhausted.Quota = quotaManagerTestObservation("100")
	quotaManagerTestRegister(t, manager, exhausted, model)
	quotaManagerTestRegister(t, manager, preferred, model)
	streamErr := errors.New("stream interrupted after response started")
	var attempts []string
	manager.RegisterExecutor(&customStreamMockExecutor{
		identifier: "codex",
		streamFn: func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
			attempts = append(attempts, selected.ID)
			chunks := make(chan cliproxyexecutor.StreamChunk, 2)
			chunks <- cliproxyexecutor.StreamChunk{Payload: []byte("data: started\n\n")}
			chunks <- cliproxyexecutor.StreamChunk{Err: streamErr}
			close(chunks)
			return &cliproxyexecutor.StreamResult{Headers: http.Header{"Content-Type": []string{"text/event-stream"}}, Chunks: chunks}, nil
		},
	})
	result, errStream := manager.ExecuteStream(context.Background(), []string{"codex"}, cliproxyexecutor.Request{Model: model}, cliproxyexecutor.Options{Stream: true})
	if errStream != nil {
		t.Fatalf("ExecuteStream() error = %v, want committed stream", errStream)
	}
	var chunks []cliproxyexecutor.StreamChunk
	for chunk := range result.Chunks {
		chunks = append(chunks, chunk)
	}
	if result.Headers.Get("Content-Type") != "text/event-stream" || len(chunks) != 2 || string(chunks[0].Payload) != "data: started\n\n" || chunks[1].Err != streamErr {
		t.Fatalf("started stream lost headers, payload, or original error: headers=%v chunks=%+v", result.Headers, chunks)
	}
	if !reflect.DeepEqual(attempts, []string{preferred.ID}) {
		t.Fatalf("started stream was replayed: attempts=%v", attempts)
	}
}
