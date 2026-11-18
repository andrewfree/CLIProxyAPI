package ops

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
)

// The fleet verifier compares fleet-release.json runtime_policy keys with the
// top-level JSON that GET /v0/management/config serves for the loaded config.
// A renamed tag or a new omitempty would hide a pinned value from the verifier.
func TestRuntimePolicyKeysAreServedByManagementConfig(t *testing.T) {
	raw, errRead := os.ReadFile("fleet-release.json")
	if errRead != nil {
		t.Fatalf("read manifest: %v", errRead)
	}
	var release struct {
		RuntimePolicy map[string]any `json:"runtime_policy"`
	}
	if errDecode := json.Unmarshal(raw, &release); errDecode != nil {
		t.Fatalf("decode manifest: %v", errDecode)
	}
	if len(release.RuntimePolicy) == 0 {
		t.Fatal("manifest has no runtime_policy")
	}
	served, errMarshal := json.Marshal(config.Config{})
	if errMarshal != nil {
		t.Fatalf("marshal config: %v", errMarshal)
	}
	var fields map[string]any
	if errFields := json.Unmarshal(served, &fields); errFields != nil {
		t.Fatalf("decode config: %v", errFields)
	}
	for key := range release.RuntimePolicy {
		if _, ok := fields[key]; !ok {
			t.Errorf("runtime_policy key %q is not a top-level management config field for a zero value", key)
		}
	}
}
