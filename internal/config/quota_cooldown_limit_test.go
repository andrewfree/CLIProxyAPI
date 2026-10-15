package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestQuotaCooldownLimitConfigRoundTrip(t *testing.T) {
	for _, raw := range []string{
		"quota-cooldown-max-seconds: 3600\n",
		"routing:\n  cooldown:\n    quota-cooldown-max-seconds: 3600\n",
	} {
		cfg, err := ParseConfigBytes([]byte(raw))
		if err != nil {
			t.Fatal(err)
		}
		if cfg.QuotaCooldownMaxSeconds != 3600 || cfg.CloneForRuntime().QuotaCooldownMaxSeconds != 3600 {
			t.Fatal("quota cap lost during parsing or snapshot")
		}
		path := filepath.Join(t.TempDir(), "config.yaml")
		if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if err := SaveConfigPreserveComments(path, cfg); err != nil {
			t.Fatal(err)
		}
		reloaded, err := LoadConfig(path)
		if err != nil {
			t.Fatal(err)
		}
		if reloaded.QuotaCooldownMaxSeconds != 3600 {
			t.Fatal("quota cap lost during save")
		}
	}
}
