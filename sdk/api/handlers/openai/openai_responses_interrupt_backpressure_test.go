package openai

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/MoeForge/moego/ojson"
	"github.com/gorilla/websocket"
)

type interruptAckListener struct {
	net.Listener
	ctx      context.Context
	armed    *atomic.Bool
	entered  chan error
	release  <-chan struct{}
	writeErr error
}

func (l *interruptAckListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &interruptAckConn{Conn: conn, gate: l}, nil
}

type interruptAckConn struct {
	net.Conn
	gate *interruptAckListener
}

func (c *interruptAckConn) Write(payload []byte) (int, error) {
	if c.gate.armed.Swap(false) {
		c.gate.entered <- c.gate.ctx.Err()
		<-c.gate.release
		if c.gate.writeErr != nil {
			return 0, c.gate.writeErr
		}
	}
	return c.Conn.Write(payload)
}

func TestResponsesInterruptCancelsBeforeBlockedAcknowledgement(t *testing.T) {
	writeFailure := errors.New("acknowledgement write failed")
	for _, writeErr := range []error{nil, writeFailure} {
		name := "success"
		if writeErr != nil {
			name = "write_failure"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var armed atomic.Bool
			entered := make(chan error, 1)
			release := make(chan struct{})
			done := make(chan error, 1)
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := responsesWebsocketUpgrader.Upgrade(w, r, nil)
				if err != nil {
					done <- err
					return
				}
				defer func() { _ = conn.Close() }()
				armed.Store(true)
				output, responseID, pending, _, err := completeResponsesWebsocketLocalInterrupt(
					newResponsesWebsocketWriter(conn), nil, func(...interface{}) { cancel() },
					[]byte(`{"response_id":"r-blocked"}`), nil,
					[][]byte{[]byte(`{"type":"function_call","call_id":"call-1","name":"lookup","arguments":"{}"}`)},
					map[string]struct{}{"call-1": {}},
				)
				if responseID != "r-blocked" || !reflect.DeepEqual(pending, []string{"call-1"}) || ojson.GetBytesNoCopy(output, "0.call_id").String() != "call-1" {
					done <- errors.New("interrupt lost response identity, output, or pending tool calls")
					return
				}
				done <- err
			}))
			server.Listener = &interruptAckListener{Listener: server.Listener, ctx: ctx, armed: &armed, entered: entered, release: release, writeErr: writeErr}
			server.Start()
			defer server.Close()
			defer close(release)
			client, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = client.Close() }()
			select {
			case observed := <-entered:
				if !errors.Is(observed, context.Canceled) {
					t.Errorf("upstream cancellation at blocked acknowledgement = %v, want context canceled", observed)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("acknowledgement did not reach the downstream writer")
			}
			release <- struct{}{}
			if err := client.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
				t.Fatal(err)
			}
			_, payload, readErr := client.ReadMessage()
			if writeErr == nil {
				if readErr != nil || ojson.GetBytesNoCopy(payload, "type").String() != "response.incomplete" || ojson.GetBytesNoCopy(payload, "response.id").String() != "r-blocked" {
					t.Fatalf("unexpected acknowledgement %s, error %v", payload, readErr)
				}
			} else if readErr == nil {
				t.Fatal("failed acknowledgement was delivered")
			}
			if err := <-done; !errors.Is(err, writeErr) {
				t.Fatalf("interrupt write error = %v, want %v", err, writeErr)
			}
		})
	}
}
