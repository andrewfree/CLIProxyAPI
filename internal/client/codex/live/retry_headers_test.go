package live

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
)

func TestLiveClientRetryHeaders(t *testing.T) {
	for name, copyHeaders := range map[string]func(http.Header, http.Header){
		"HTTP":      writeResponseHeaders,
		"websocket": copyRealtimeHandshakeHeaders,
	} {
		for _, wait := range []string{"30", "256"} {
			t.Run(name+wait, func(t *testing.T) {
				dst := make(http.Header)
				copyHeaders(dst, http.Header{"Retry-After": {wait}, "X-Request-Id": {"req-live"}})
				want := wait
				if wait == "256" {
					want = ""
				}
				if got := dst.Get("Retry-After"); got != want {
					t.Errorf("Retry-After = %q, want %q", got, want)
				}
				if dst.Get("X-Request-Id") != "req-live" {
					t.Fatal("request ID changed")
				}
			})
		}
	}
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/realtime", nil)
	writeSelectionError(c, auth.NewHomeConcurrencyBusyError("busy", 256*time.Second))
	if recorder.Header().Get("Retry-After") != "" || recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("selection error = %d %v", recorder.Code, recorder.Header())
	}
}
