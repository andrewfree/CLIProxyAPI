package management

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
)

func TestConfigV8QuotaAwareStrategies(t *testing.T) {
	gin.SetMode(gin.TestMode)
	filename := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(filename, []byte("port: 8317\nrouting: {strategy: round-robin}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.LoadConfig(filename)
	if err != nil {
		t.Fatal(err)
	}
	handler := &Handler{cfg: cfg, configFilePath: filename}
	router := gin.New()
	router.PUT("/v8/management/config/*path", handler.ConfigV8)
	for _, strategy := range []string{"quota-aware-shadow", "quota-aware", "round-robin"} {
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, httptest.NewRequest(http.MethodPut, "/v8/management/config/routing/strategy", strings.NewReader(`"`+strategy+`"`)))
		if recorder.Code != http.StatusOK {
			t.Fatalf("%s: status=%d body=%s", strategy, recorder.Code, recorder.Body.String())
		}
		saved, errLoad := config.LoadConfig(filename)
		if errLoad != nil || saved.Routing.Strategy != strategy {
			t.Fatalf("persisted strategy=%v, error=%v", saved, errLoad)
		}
	}
	before, err := os.ReadFile(filename)
	if err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, httptest.NewRequest(http.MethodPut, "/v8/management/config/routing/strategy", strings.NewReader(`{"unexpected":true}`)))
	if recorder.Code < 400 {
		t.Fatalf("malformed strategy accepted: %d", recorder.Code)
	}
	after, err := os.ReadFile(filename)
	if err != nil || string(before) != string(after) {
		t.Fatal("malformed update changed persisted config")
	}
}
