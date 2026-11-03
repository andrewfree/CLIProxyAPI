package handlers_test

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/interfaces"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/api/handlers"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/api/handlers/claude"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
)

type pluginRetryError string

func (e pluginRetryError) Error() string { return "plugin busy" }
func (e pluginRetryError) SafeResponseHeaders() http.Header {
	return http.Header{"Retry-After": {string(e)}}
}

func TestClientRetryPolicyAtErrorBoundary(t *testing.T) {
	gin.SetMode(gin.TestMode)
	base := handlers.NewBaseAPIHandlers(&config.SDKConfig{PassthroughHeaders: true}, nil)
	for name, write := range map[string]func(*gin.Context, *interfaces.ErrorMessage){
		"base":   base.WriteErrorResponse,
		"claude": claude.NewClaudeCodeAPIHandler(base).WriteErrorResponse,
	} {
		t.Run(name, func(t *testing.T) {
			for _, tc := range []struct {
				name   string
				err    error
				raw    string
				direct bool
				want   string
			}{
				{"Home long owns short raw", auth.NewHomeConcurrencyBusyError("busy", 256*time.Second), "30", false, ""},
				{"Home short owns long raw", auth.NewHomeConcurrencyBusyError("busy", 30*time.Second), "256", false, "30"},
				{"plugin long", pluginRetryError("256"), "30", false, ""},
				{"plugin short", pluginRetryError("30"), "256", false, "30"},
				{"raw long", errors.New("busy"), "256", false, ""},
				{"raw short", errors.New("busy"), "30", false, "30"},
				{"direct long", errors.New("busy"), "256", true, ""},
				{"direct short", errors.New("busy"), "30", true, "30"},
			} {
				t.Run(tc.name, func(t *testing.T) {
					recorder := httptest.NewRecorder()
					c, _ := gin.CreateTestContext(recorder)
					c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
					headers := http.Header{"Retry-After": {tc.raw}, "X-Request-Id": {"req-policy"}}
					write(c, &interfaces.ErrorMessage{StatusCode: 429, Error: tc.err, Addon: headers, Headers: headers, DirectResponse: tc.direct, Body: []byte(`{"error":"busy"}`)})
					if got := recorder.Header().Get("Retry-After"); got != tc.want {
						t.Errorf("client Retry-After = %q, want %q", got, tc.want)
					}
					if got := recorder.Header().Get("X-Request-Id"); got != "req-policy" {
						t.Errorf("unrelated header = %q", got)
					}
					if recorder.Code != 429 {
						t.Errorf("status = %d", recorder.Code)
					}
				})
			}
		})
	}
}
