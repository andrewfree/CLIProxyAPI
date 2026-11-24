package logging

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/MoeForge/moego/ojson"
)

func TestStreamLogQueueWaitsForRoom(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		queue := make(chan []byte, 1)
		queue <- []byte("first")
		go func() {
			time.Sleep(time.Millisecond)
			<-queue
		}()

		var drops StreamLogDrops
		drops.Enqueue(queue, []byte("second"))
		if chunks, byteCount := drops.Counts(); len(queue) != 1 || chunks != 0 || byteCount != 0 {
			t.Fatalf("queue length = %d, dropped %d chunks and %d bytes, want one queued chunk and no drops", len(queue), chunks, byteCount)
		}
		if got := string(<-queue); got != "second" {
			t.Fatalf("queued chunk = %q, want second", got)
		}
	})
}

func TestStreamLogQueueCountsDropsAtDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		queue := make(chan []byte, 1)
		queue <- []byte("first")

		var drops StreamLogDrops
		start := time.Now()
		drops.Enqueue(queue, []byte("missing"))
		if elapsed := time.Since(start); elapsed != streamLogQueueWait {
			t.Fatalf("wait = %s, want %s", elapsed, streamLogQueueWait)
		}
		if chunks, byteCount := drops.Counts(); chunks != 1 || byteCount != 7 {
			t.Fatalf("dropped %d chunks and %d bytes, want 1 chunk and 7 bytes", chunks, byteCount)
		}
	})
}

func TestFileStreamingLogMarksSpoolQueueDrops(t *testing.T) {
	dir := t.TempDir()
	body, errCreate := os.CreateTemp(dir, "response-body-*.tmp")
	if errCreate != nil {
		t.Fatal(errCreate)
	}
	t.Cleanup(func() { _ = body.Close() })

	writer := &FileStreamingLogWriter{
		logsDir:          dir,
		logFilename:      "stream.log",
		responseBodyPath: body.Name(),
		responseBodyFile: body,
		chunkChan:        make(chan []byte, 1),
		closeChan:        make(chan struct{}),
		errorChan:        make(chan error, 1),
	}

	// Keep the spooler paused until the queue fills and subsequent writes time out.
	writer.WriteChunkAsync([]byte("data: retained"))
	writer.WriteChunkAsync([]byte("missing"))
	writer.WriteChunkAsync([]byte("gone"))
	writer.RecordDroppedChunks(1, 2)
	go writer.asyncWriter()
	if errClose := writer.Close(); errClose != nil {
		t.Fatal(errClose)
	}

	raw, errRead := os.ReadFile(filepath.Join(dir, "stream.log"))
	if errRead != nil {
		t.Fatal(errRead)
	}
	assertStreamLogDropMarker(t, string(raw), "data: retained", "[RESPONSE STREAM CHUNKS DROPPED FROM LOG: 3 chunks, 13 bytes]")
}

func TestHomeStreamingLogMarksQueueDrops(t *testing.T) {
	original := currentHomeRequestLogClient
	t.Cleanup(func() { currentHomeRequestLogClient = original })
	stub := &stubHomeRequestLogClient{heartbeatOK: true}
	currentHomeRequestLogClient = func() homeRequestLogClient { return stub }
	writer := &homeStreamingLogWriter{
		method:    http.MethodPost,
		chunkChan: make(chan []byte, 1),
		doneChan:  make(chan struct{}),
	}

	writer.WriteChunkAsync([]byte("data: retained"))
	writer.WriteChunkAsync([]byte("missing"))
	writer.RecordDroppedChunks(1, 2)
	go writer.asyncWriter()
	if errClose := writer.Close(); errClose != nil {
		t.Fatal(errClose)
	}
	if len(stub.pushed) != 1 {
		t.Fatalf("home pushed %d logs, want one", len(stub.pushed))
	}
	logBody := ojson.GetBytes(stub.pushed[0], "request_log").String()
	assertStreamLogDropMarker(t, logBody, "data: retained", "[RESPONSE STREAM CHUNKS DROPPED FROM LOG: 2 chunks, 9 bytes]")
}

func assertStreamLogDropMarker(t *testing.T, logBody, retained, marker string) {
	t.Helper()
	_, response, found := strings.Cut(logBody, "=== RESPONSE ===\n")
	if !found || !strings.HasSuffix(response, retained+"\n"+marker+"\n") || strings.Count(logBody, marker) != 1 {
		t.Fatalf("request log missing a single response trailer %q: %q", marker, logBody)
	}
}
