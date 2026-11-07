package auth

import (
	"context"
	"fmt"
	"testing"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	log "github.com/sirupsen/logrus"
)

func TestManagerQuotaAwareShadowForecastIncludesLowerPositivePriority(t *testing.T) {
	hook := setupTestLoggerHook(t)
	log.SetLevel(log.DebugLevel)
	for _, highCount := range []int{1, 2} {
		for _, route := range []string{"scheduler-single", "scheduler-mixed", "legacy-single", "legacy-mixed"} {
			t.Run(fmt.Sprintf("high-count-%d/%s", highCount, route), func(t *testing.T) {
				shadow := NewManager(nil, NewQuotaAwareSelector(QuotaAwareSelectorOptions{Shadow: true}), nil)
				baseline := NewManager(nil, &RoundRobinSelector{}, nil)
				model := "quota-shadow-priority-" + t.Name()
				providers := []string{"codex"}
				if route == "scheduler-mixed" || route == "legacy-mixed" {
					providers = append(providers, "claude")
				}
				for i := 0; i < highCount; i++ {
					high := quotaManagerTestAuth(t, fmt.Sprintf("high-zero-%d", i))
					high.Attributes = map[string]string{"priority": "100", AttributeWeight: "0"}
					quotaShadowRegisterPair(t, shadow, baseline, high, model)
				}
				low := quotaManagerTestAuth(t, "low-positive")
				low.Provider = providers[len(providers)-1]
				low.Attributes = map[string]string{"priority": "0", AttributeWeight: "1"}
				quotaShadowRegisterPair(t, shadow, baseline, low, model)
				quotaShadowRegisterExecutors(shadow, providers)
				quotaShadowRegisterExecutors(baseline, providers)
				pick := func(manager *Manager) (*Auth, error) {
					ctx := context.Background()
					opts := cliproxyexecutor.Options{}
					switch route {
					case "scheduler-single":
						selected, _, errPick := manager.pickNext(ctx, providers[0], model, opts, nil)
						return selected, errPick
					case "scheduler-mixed":
						selected, _, _, errPick := manager.pickNextMixed(ctx, providers, model, opts, nil)
						return selected, errPick
					case "legacy-single":
						selected, _, errPick := manager.pickNextLegacy(ctx, providers[0], model, opts, nil)
						return selected, errPick
					default:
						selected, _, _, errPick := manager.pickNextMixedLegacy(ctx, providers, model, opts, nil)
						return selected, errPick
					}
				}
				for i := 0; i < 4; i++ {
					hook.Reset()
					actual, errActual := pick(shadow)
					if errActual != nil {
						t.Fatal(errActual)
					}
					ordinary, errOrdinary := pick(baseline)
					if errOrdinary != nil || actual.ID != ordinary.ID {
						t.Fatalf("shadow=%s, ordinary=%v, error=%v", actual.ID, ordinary, errOrdinary)
					}
					forecastFound := false
					for _, entry := range hook.AllEntries() {
						if entry.Message != "quota-aware selector shadow evaluation" {
							continue
						}
						forecastFound = true
						if entry.Data["candidate_count"] != highCount+1 || entry.Data["prediction_available"] != true || entry.Data["prediction_differs"] != true {
							t.Fatalf("forecast must include the lower positive tier without changing actual routing: %v", entry.Data)
						}
					}
					if !forecastFound {
						t.Fatal("shadow forecast diagnostic was not emitted")
					}
				}
			})
		}
	}
}
