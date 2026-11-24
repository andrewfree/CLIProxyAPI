package logging

import (
	"fmt"
	"io"
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
}

// Enqueue sends an owned chunk to the spooler or counts it if the queue stays full.
func (d *StreamLogDrops) Enqueue(queue chan<- []byte, chunk []byte) {
	select {
	case queue <- chunk:
		return
	default:
	}

	timer := time.NewTimer(streamLogQueueWait)
	defer timer.Stop()
	select {
	case queue <- chunk:
	case <-timer.C:
		d.Add(1, uint64(len(chunk)))
	}
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
