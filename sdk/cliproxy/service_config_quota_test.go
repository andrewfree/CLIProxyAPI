package cliproxy

import (
	"context"
	"strconv"
	"testing"
	"time"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func TestQuotaAwareRoutingReload(t *testing.T) {
	service := &Service{coreManager: coreauth.NewManager(nil, nil, nil)}
	for i, strategy := range []string{"round-robin", "quota-aware-shadow", "quota-aware", "round-robin"} {
		cfg := &internalconfig.Config{Routing: internalconfig.RoutingConfig{Strategy: strategy}}
		if !service.applyManagerConfig(context.Background(), configCommit{cfg: cfg, sequence: uint64(i + 1)}) {
			t.Fatalf("apply %s failed", strategy)
		}
		selector := service.coreManager.Selector()
		if strategy == "round-robin" {
			if _, ok := selector.(*coreauth.RoundRobinSelector); !ok {
				t.Fatalf("%s constructed %T", strategy, selector)
			}
			continue
		}
		if _, ok := selector.(*coreauth.QuotaAwareSelector); !ok {
			t.Fatalf("%s constructed %T", strategy, selector)
		}
		now := time.Now()
		candidates := []*coreauth.Auth{
			{ID: "a-empty", Provider: "claude", Status: coreauth.StatusActive, Quota: coreauth.QuotaState{
				ObservedAt: now, Signals: map[string]string{
					"Anthropic-Ratelimit-Unified-7d-Utilization": "1",
					"Anthropic-Ratelimit-Unified-7d-Reset":       strconv.FormatInt(now.Add(time.Hour).Unix(), 10),
				},
			}},
			{ID: "b-usable", Provider: "claude", Status: coreauth.StatusActive},
		}
		selected, err := selector.Pick(context.Background(), "claude", "claude-opus-5-5", cliproxyexecutor.Options{}, candidates)
		if err != nil {
			t.Fatal(err)
		}
		want := "b-usable"
		if strategy == "quota-aware-shadow" {
			want = "a-empty"
		}
		if selected.ID != want {
			t.Fatalf("%s selected %s, want %s", strategy, selected.ID, want)
		}
	}
}

func TestQuotaAwareRoutingNormalization(t *testing.T) {
	for _, strategy := range []string{"quota-aware", "quota-aware-shadow"} {
		state := normalizedRoutingRuntimeState(&internalconfig.Config{Routing: internalconfig.RoutingConfig{Strategy: "  " + strategy + "  "}})
		if state.strategy != strategy || state.sessionAffinity {
			t.Fatalf("state = %+v, want %s with affinity off", state, strategy)
		}
	}
	if state := normalizedRoutingRuntimeState(nil); state.strategy != "round-robin" {
		t.Fatalf("default strategy = %s", state.strategy)
	}
}
