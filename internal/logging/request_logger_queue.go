package logging

import (
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"
)

const (
	// StreamLogQueueDepth is the maximum number of chunks buffered by each streaming log queue.
	StreamLogQueueDepth = 100
	// Give a busy spooler a chance to drain without making the client wait on disk.
	streamLogQueueWait = 3 * time.Millisecond
)

// StreamLogDrops counts chunks omitted from a response log and their payload bytes.
type StreamLogDrops struct {
	chunks atomic.Uint64
	bytes  atomic.Uint64

	mu       sync.Mutex // serializes enqueues and guards the timer and dropping state
	timer    *time.Timer
	dropping bool
}

// Enqueue sends an owned chunk to the spooler or counts it if the queue stays full.
func (d *StreamLogDrops) Enqueue(queue chan<- []byte, chunk []byte) {
	d.enqueueUntil(queue, chunk, nil)
}

func (d *StreamLogDrops) enqueueUntil(queue chan<- []byte, chunk []byte, stopWaiting <-chan struct{}) {
	d.mu.Lock()
	defer d.mu.Unlock()

	select {
	case queue <- chunk:
		d.dropping = false
		return
	default:
	}
	if d.dropping {
		d.Add(1, uint64(len(chunk)))
		return
	}
	select {
	case <-stopWaiting:
		d.Add(1, uint64(len(chunk)))
		return
	default:
	}

	if d.timer == nil {
		d.timer = time.NewTimer(streamLogQueueWait)
	} else {
		d.timer.Reset(streamLogQueueWait)
	}
	defer d.timer.Stop()
	select {
	case queue <- chunk:
		return
	case <-d.timer.C:
		d.dropping = true
	case <-stopWaiting:
	}
	d.Add(1, uint64(len(chunk)))
}

// Add records chunks and bytes dropped from a queue.
func (d *StreamLogDrops) Add(chunks, byteCount uint64) {
	d.chunks.Add(chunks)
	d.bytes.Add(byteCount)
}

// Counts returns the final chunk and byte counts after producers finish.
func (d *StreamLogDrops) Counts() (chunks, byteCount uint64) {
	return d.chunks.Load(), d.bytes.Load()
}

// StreamLogDropMarker formats the response trailer on its own line, even after a partial SSE line.
func StreamLogDropMarker(chunks, byteCount uint64) string {
	return fmt.Sprintf("\n[RESPONSE STREAM CHUNKS DROPPED FROM LOG: %d chunks, %d bytes]\n", chunks, byteCount)
}

func (d *StreamLogDrops) writeMarker(w io.Writer) error {
	chunks, byteCount := d.Counts()
	if chunks == 0 {
		return nil
	}
	_, errWrite := io.WriteString(w, StreamLogDropMarker(chunks, byteCount))
	return errWrite
}
