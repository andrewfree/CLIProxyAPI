package auth

import (
	"context"
	"fmt"
	"net/http"
	"testing"
	"time"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
)

func TestManagerQuotaAwareZeroWeightBeforePriority(t *testing.T) {
	for _, highCount := range []int{1, 2} {
		for _, mode := range []string{"select", "execute", "count", "stream"} {
			t.Run(fmt.Sprintf("high-count-%d/%s", highCount, mode), func(t *testing.T) {
				manager := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), nil)
				model := "quota-capacity-" + t.Name()
				for i := 0; i < highCount; i++ {
					high := quotaManagerTestAuth(t, fmt.Sprintf("high-%d", i))
					high.Attributes = map[string]string{"priority": "100", AttributeWeight: "0"}
					quotaManagerTestRegister(t, manager, high, model)
				}
				low := quotaManagerTestAuth(t, "low-positive")
				low.Attributes = map[string]string{"priority": "0", AttributeWeight: "1"}
				quotaManagerTestRegister(t, manager, low, model)
				manager.RegisterExecutor(quotaManagerTestExecutor(func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
					return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
				}))
				selected, err := quotaManagerTestRun(manager, mode, model, cliproxyexecutor.Options{})
				if err != nil || selected != low.ID {
					t.Fatalf("selected=%q error=%v, want usable lower-priority %s", selected, err, low.ID)
				}
			})
		}
	}
}

func TestQuotaAwareAffinityPreservesTransientBinding(t *testing.T) {
	selector := NewSessionAffinitySelectorWithConfig(SessionAffinityConfig{
		Fallback: NewQuotaAwareSelector(QuotaAwareSelectorOptions{}), RebindOn: SessionAffinityRebindOnQuota,
	})
	t.Cleanup(selector.Stop)
	authA := &Auth{ID: "auth-a", Provider: "codex"}
	authB := &Auth{ID: "auth-b", Provider: "codex"}
	prompt := cliproxyexecutor.Options{OriginalRequest: []byte(`{"prompt_cache_key":"prompt"}`)}
	combined := cliproxyexecutor.Options{OriginalRequest: []byte(`{"prompt_cache_key":"prompt","conversation":"conversation"}`)}
	conversation := cliproxyexecutor.Options{OriginalRequest: []byte(`{"conversation":"conversation"}`)}
	guardianTestPick(t, selector, "codex", "model", prompt, []*Auth{authB}, "auth-b")
	authB.ModelStates = map[string]*ModelState{
		"model": {Unavailable: true, NextRetryAfter: time.Now().Add(time.Hour), LastError: &Error{HTTPStatus: 503}},
	}
	guardianTestPick(t, selector, "codex", "model", combined, []*Auth{authA, authB}, "auth-a")
	authB.ModelStates = nil
	guardianTestPick(t, selector, "codex", "model", conversation, []*Auth{authA, authB}, "auth-b")
}

func TestQuotaAwareAffinityTransientMapHonorsConfiguredWeight(t *testing.T) {
	for _, shadow := range []bool{false, true} {
		t.Run(fmt.Sprintf("shadow-%t", shadow), func(t *testing.T) {
			affinity := NewSessionAffinitySelectorWithConfig(SessionAffinityConfig{
				Fallback: NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: shadow}), RebindOn: SessionAffinityRebindOnQuota,
			})
			t.Cleanup(affinity.Stop)
			manager := NewManager(nil, affinity, nil)
			candidate := quotaManagerTestAuth(t, "transient")
			candidate.Attributes = map[string]string{AttributeWeight: "0"}
			candidate.ModelStates = map[string]*ModelState{
				"model": {Unavailable: true, NextRetryAfter: time.Now().Add(time.Hour), LastError: &Error{HTTPStatus: 503}},
			}
			ids := manager.transientlyUnavailableSessionAffinityAuthIDs(affinity, []*Auth{candidate}, "model", time.Now())
			if _, preserved := ids[candidate.ID]; preserved != shadow {
				t.Fatalf("zero weight preserved=%t, shadow=%t", preserved, shadow)
			}
			candidate.Attributes[AttributeWeight] = "1"
			ids = manager.transientlyUnavailableSessionAffinityAuthIDs(affinity, []*Auth{candidate}, "model", time.Now())
			if _, preserved := ids[candidate.ID]; !preserved {
				t.Fatal("positive-weight transient binding was discarded")
			}
		})
	}
}

func TestManagerQuotaAwareAffinityZeroWeightBeforePriority(t *testing.T) {
	for _, identity := range []string{"none", "explicit", "content-prefix"} {
		for _, mode := range []string{"select", "execute", "count", "stream"} {
			t.Run(identity+"/"+mode, func(t *testing.T) {
				affinity := NewSessionAffinitySelector(NewQuotaAwareSelector(QuotaAwareSelectorOptions{}))
				t.Cleanup(affinity.Stop)
				manager := NewManager(nil, affinity, nil)
				model := "quota-affinity-capacity-" + t.Name()
				high, low := quotaManagerTestAuth(t, "high-zero"), quotaManagerTestAuth(t, "low-positive")
				high.Attributes = map[string]string{"priority": "100", AttributeWeight: "0"}
				low.Attributes = map[string]string{"priority": "0", AttributeWeight: "1"}
				quotaManagerTestRegister(t, manager, high, model)
				quotaManagerTestRegister(t, manager, low, model)
				manager.RegisterExecutor(quotaManagerTestExecutor(func(_ context.Context, selected *Auth, _ cliproxyexecutor.Request, _ cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
					return cliproxyexecutor.Response{Payload: []byte(selected.ID)}, nil
				}))
				opts := cliproxyexecutor.Options{}
				if identity == "explicit" {
					opts.Headers = http.Header{"X-Session-Id": []string{"quota-capacity-session"}}
				}
				if identity == "content-prefix" {
					opts.SourceFormat = sdktranslator.FormatOpenAI
					opts.OriginalRequest = []byte(`{"messages":[{"role":"system","content":"stable"},{"role":"user","content":"first"}]}`)
					opts.Metadata = map[string]any{cliproxyexecutor.CallerScopeMetadataKey: "quota-capacity-caller"}
				}
				for pick := 0; pick < 2; pick++ {
					selected, err := quotaManagerTestRun(manager, mode, model, opts)
					if err != nil || selected != low.ID {
						t.Fatalf("pick=%d selected=%q error=%v, want usable %s", pick, selected, err, low.ID)
					}
				}
				pinned := cliproxyexecutor.Options{Metadata: map[string]any{cliproxyexecutor.PinnedAuthMetadataKey: high.ID}}
				selected, err := quotaManagerTestRun(manager, mode, model, pinned)
				if err != nil || selected != high.ID {
					t.Fatalf("explicit pin selected=%q error=%v, want %s", selected, err, high.ID)
				}
			})
		}
	}
}

func TestQuotaAwareAffinityStopsUsingZeroWeightBinding(t *testing.T) {
	for _, identity := range []string{"explicit", "content-prefix"} {
		t.Run(identity, func(t *testing.T) {
			selector := NewSessionAffinitySelector(NewQuotaAwareSelector(QuotaAwareSelectorOptions{}))
			t.Cleanup(selector.Stop)
			high, low := quotaManagerTestAuth(t, "high"), quotaManagerTestAuth(t, "low")
			high.Attributes = map[string]string{"priority": "100", AttributeWeight: "1"}
			low.Attributes = map[string]string{AttributeWeight: "1"}
			opts := cliproxyexecutor.Options{Metadata: map[string]any{}}
			if identity == "explicit" {
				opts.Headers = http.Header{"X-Session-Id": []string{"quota-binding"}}
			} else {
				opts.SourceFormat = sdktranslator.FormatOpenAI
				opts.OriginalRequest = []byte(`{"messages":[{"role":"system","content":"stable"},{"role":"user","content":"first"}]}`)
				opts.Metadata[cliproxyexecutor.CallerScopeMetadataKey] = "quota-binding-caller"
			}
			candidates := []*Auth{high, low}
			first, err := selector.Pick(context.Background(), "codex", "quota-binding-model", opts, candidates)
			if err != nil || first.ID != high.ID {
				t.Fatalf("first=%v error=%v, want high priority", first, err)
			}
			if identity == "content-prefix" && opts.Metadata[cliproxyexecutor.LCPAffinitySessionIDMetadataKey] == nil {
				t.Fatal("content-prefix affinity was not exercised")
			}
			high.Attributes[AttributeWeight] = "0"
			second, err := selector.Pick(context.Background(), "codex", "quota-binding-model", opts, candidates)
			if err != nil || second.ID != low.ID {
				t.Fatalf("second=%v error=%v, want positive-weight credential", second, err)
			}
			opts.Metadata[cliproxyexecutor.PinnedAuthMetadataKey] = high.ID
			pinned, err := selector.Pick(context.Background(), "codex", "quota-binding-model", opts, candidates)
			if err != nil || pinned.ID != high.ID {
				t.Fatalf("pin=%v error=%v, want explicit pin", pinned, err)
			}
			delete(opts.Metadata, cliproxyexecutor.PinnedAuthMetadataKey)
			sole, err := selector.Pick(context.Background(), "codex", "quota-binding-model", opts, []*Auth{high})
			if err != nil || sole.ID != high.ID {
				t.Fatalf("sole=%v error=%v, want genuinely sole credential", sole, err)
			}
		})
	}
}
