package auth

import (
	"context"
	"net/http"
	"reflect"
	"strings"
	"testing"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func quotaShadowRun(manager *Manager, mode string, providers []string, model string, opts cliproxyexecutor.Options) (string, error) {
	ctx := context.Background()
	req := cliproxyexecutor.Request{Model: model}
	switch mode {
	case "select":
		selected, errSelect := manager.SelectAuth(ctx, providers[0], model, opts)
		if errSelect != nil {
			return "", errSelect
		}
		return selected.ID, nil
	case "execute":
		response, errExecute := manager.Execute(ctx, providers, req, opts)
		return string(response.Payload), errExecute
	case "count":
		response, errCount := manager.ExecuteCount(ctx, providers, req, opts)
		return string(response.Payload), errCount
	default:
		opts.Stream = true
		result, errStream := manager.ExecuteStream(ctx, providers, req, opts)
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

func quotaShadowRegisterPair(t *testing.T, shadow, baseline *Manager, candidate *Auth, models ...string) {
	t.Helper()
	quotaManagerTestRegister(t, shadow, candidate, models...)
	if _, errRegister := baseline.Register(context.Background(), candidate.Clone()); errRegister != nil {
		t.Fatal(errRegister)
	}
}

func quotaShadowRegisterExecutors(manager *Manager, providers []string) {
	for _, provider := range providers {
		executor := quotaManagerTestExecutor(func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
			return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
		}).(*customStreamMockExecutor)
		executor.identifier = provider
		manager.RegisterExecutor(executor)
	}
	manager.SetRetryConfig(0, 0, 0)
}

func TestManagerQuotaAwareShadowMixedProvidersMatchesRoundRobin(t *testing.T) {
	for _, aliases := range []bool{false, true} {
		name := "models"
		if aliases {
			name = "aliases"
		}
		for _, mode := range []string{"execute", "count", "stream"} {
			t.Run(name+"/"+mode, func(t *testing.T) {
				selector := NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true})
				shadow := NewManager(nil, selector, nil)
				baseline := NewManager(nil, &RoundRobinSelector{}, nil)
				providers := []string{"codex", "claude"}
				routes := []string{"mixed-a-" + t.Name(), "mixed-b-" + t.Name()}
				models := append([]string(nil), routes...)
				if aliases {
					target := "mixed-upstream-" + t.Name()
					models = append(models, target)
					mapping := make(map[string][]internalconfig.OAuthModelAlias)
					for _, provider := range providers {
						mapping[provider] = []internalconfig.OAuthModelAlias{{Name: target, Alias: routes[0], Fork: true}, {Name: target, Alias: routes[1], Fork: true}}
					}
					shadow.SetOAuthModelAlias(mapping)
					baseline.SetOAuthModelAlias(mapping)
				}
				first := quotaManagerTestAuth(t, "a-codex-exhausted")
				first.Quota = quotaManagerTestObservation("100")
				second := quotaManagerTestAuth(t, "b-codex-zero-weight")
				second.Attributes = map[string]string{AttributeWeight: "0"}
				third := quotaManagerTestAuth(t, "c-claude")
				third.Provider = "claude"
				for _, candidate := range []*Auth{first, second, third} {
					quotaShadowRegisterPair(t, shadow, baseline, candidate, models...)
				}
				quotaShadowRegisterExecutors(shadow, providers)
				quotaShadowRegisterExecutors(baseline, providers)
				if !shadow.useSchedulerFastPath() || isBuiltInSelector(selector) {
					t.Fatal("shadow must delegate to the native scheduler without making active quota-aware selection a built-in")
				}
				for pick := 0; pick < 18; pick++ {
					options := func() cliproxyexecutor.Options {
						opts := cliproxyexecutor.Options{Metadata: map[string]any{}}
						if pick == 3 || pick == 10 {
							opts.Metadata[cliproxyexecutor.PinnedAuthMetadataKey] = third.ID
						}
						return opts
					}
					actualOpts, baselineOpts := options(), options()
					actual, errActual := quotaShadowRun(shadow, mode, providers, routes[pick%2], actualOpts)
					want, errBaseline := quotaShadowRun(baseline, mode, providers, routes[pick%2], baselineOpts)
					if errActual != nil || errBaseline != nil || actual != want {
						t.Fatalf("pick %d: shadow=%q/%v round-robin=%q/%v", pick, actual, errActual, want, errBaseline)
					}
					if !reflect.DeepEqual(actualOpts.Metadata, baselineOpts.Metadata) {
						t.Fatalf("pick %d metadata: shadow=%v round-robin=%v", pick, actualOpts.Metadata, baselineOpts.Metadata)
					}
				}
				for _, route := range routes {
					if selector.states["mixed:"+canonicalModelKey(route)] == nil {
						t.Fatalf("shadow made no prediction for requested route %q", route)
					}
				}
				if !aliases && len(selector.roundRobin.lastPicked) != 0 {
					t.Fatal("scheduler predictions advanced the legacy round-robin cursor")
				}
			})
		}
	}
}

func TestManagerQuotaAwareShadowRetriesMatchRoundRobin(t *testing.T) {
	withQuotaCooldownEnabled(t)
	for _, mode := range []string{"execute", "count", "stream"} {
		t.Run(mode, func(t *testing.T) {
			shadow := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true}), nil)
			baseline := NewManager(nil, &RoundRobinSelector{}, nil)
			model := "shadow-retry-" + t.Name()
			first, second, third := quotaManagerTestAuth(t, "a-first"), quotaManagerTestAuth(t, "b-second"), quotaManagerTestAuth(t, "c-third")
			first.Quota = quotaManagerTestObservation("100")
			for _, candidate := range []*Auth{first, second, third} {
				quotaShadowRegisterPair(t, shadow, baseline, candidate, model)
			}
			var actualAttempts, baselineAttempts []string
			register := func(manager *Manager, attempts *[]string) {
				manager.SetRetryConfig(0, 0, 0)
				manager.RegisterExecutor(quotaManagerTestExecutor(func(ctx context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
					*attempts = append(*attempts, selected.ID)
					cliproxyexecutor.MarkUpstreamAttempt(ctx)
					if selected.ID == first.ID {
						return cliproxyexecutor.Response{}, customStatusError{code: http.StatusServiceUnavailable, msg: "temporary upstream failure"}
					}
					return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
				}))
			}
			register(shadow, &actualAttempts)
			register(baseline, &baselineAttempts)
			for request := 0; request < 3; request++ {
				actual, errActual := quotaShadowRun(shadow, mode, []string{"codex"}, model, cliproxyexecutor.Options{})
				want, errBaseline := quotaShadowRun(baseline, mode, []string{"codex"}, model, cliproxyexecutor.Options{})
				if errActual != nil || errBaseline != nil || actual != want || !reflect.DeepEqual(actualAttempts, baselineAttempts) {
					t.Fatalf("request %d: shadow=%q/%v attempts=%v; round-robin=%q/%v attempts=%v", request, actual, errActual, actualAttempts, want, errBaseline, baselineAttempts)
				}
			}
			if len(actualAttempts) < 2 || actualAttempts[0] != first.ID || actualAttempts[1] != second.ID {
				t.Fatalf("retry attempts = %v, want first credential then its successor", actualAttempts)
			}
		})
	}
}

func TestManagerQuotaAwareShadowAffinityMatchesRoundRobin(t *testing.T) {
	for _, aliases := range []bool{false, true} {
		name := "models"
		if aliases {
			name = "aliases"
		}
		for _, mode := range []string{"select", "execute", "count", "stream"} {
			t.Run(name+"/"+mode, func(t *testing.T) {
				quota := NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true})
				shadowAffinity := NewSessionAffinitySelector(quota)
				baselineAffinity := NewSessionAffinitySelector(&RoundRobinSelector{})
				t.Cleanup(shadowAffinity.Stop)
				t.Cleanup(baselineAffinity.Stop)
				shadow := NewManager(nil, shadowAffinity, nil)
				baseline := NewManager(nil, baselineAffinity, nil)
				routes := []string{"affinity-a-" + t.Name(), "affinity-b-" + t.Name()}
				models := append([]string(nil), routes...)
				if aliases {
					target := "affinity-upstream-" + t.Name()
					models = append(models, target)
					mapping := map[string][]internalconfig.OAuthModelAlias{
						"codex": {{Name: target, Alias: routes[0], Fork: true}, {Name: target, Alias: routes[1], Fork: true}},
					}
					shadow.SetOAuthModelAlias(mapping)
					baseline.SetOAuthModelAlias(mapping)
				}
				first := quotaManagerTestAuth(t, "a-exhausted")
				first.Quota = quotaManagerTestObservation("100")
				second := quotaManagerTestAuth(t, "b-unknown")
				zero := quotaManagerTestAuth(t, "c-zero")
				zero.Attributes = map[string]string{AttributeWeight: "0"}
				for _, candidate := range []*Auth{first, second, zero} {
					quotaShadowRegisterPair(t, shadow, baseline, candidate, models...)
				}
				quotaShadowRegisterExecutors(shadow, []string{"codex"})
				quotaShadowRegisterExecutors(baseline, []string{"codex"})
				if shadow.useSchedulerFastPath() {
					t.Fatal("affinity must retain its normal legacy path")
				}
				steps := []struct {
					route   int
					session string
					pinned  bool
				}{{0, "one", false}, {1, "one", false}, {0, "two", false}, {0, "one", false}, {1, "two", false}, {0, "one", true}, {0, "one", false}, {0, "", false}, {1, "", false}, {0, "three", false}}
				for pick, step := range steps {
					options := func() cliproxyexecutor.Options {
						opts := cliproxyexecutor.Options{Metadata: map[string]any{}}
						if step.session != "" {
							opts.Headers = http.Header{"X-Session-Id": []string{step.session}}
						}
						if step.pinned {
							opts.Metadata[cliproxyexecutor.PinnedAuthMetadataKey] = second.ID
						}
						return opts
					}
					actualOpts, baselineOpts := options(), options()
					actual, errActual := quotaShadowRun(shadow, mode, []string{"codex"}, routes[step.route], actualOpts)
					want, errBaseline := quotaShadowRun(baseline, mode, []string{"codex"}, routes[step.route], baselineOpts)
					if errActual != nil || errBaseline != nil || actual != want {
						t.Fatalf("pick %d: shadow=%q/%v round-robin=%q/%v", pick, actual, errActual, want, errBaseline)
					}
					if !reflect.DeepEqual(actualOpts.Metadata, baselineOpts.Metadata) {
						t.Fatalf("pick %d metadata: shadow=%v round-robin=%v", pick, actualOpts.Metadata, baselineOpts.Metadata)
					}
				}
				if len(quota.states) != len(routes) {
					t.Fatalf("forecast route count = %d, want %d", len(quota.states), len(routes))
				}
			})
		}
	}
}
