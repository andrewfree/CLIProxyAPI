// Package logging re-exports request logging primitives for SDK consumers.
package logging

import internallogging "github.com/router-for-me/CLIProxyAPI/v8/internal/logging"

const defaultErrorLogsMaxFiles = 10

// RequestLogger defines the interface for logging HTTP requests and responses.
type RequestLogger = internallogging.RequestLogger

// StreamingLogWriter handles real-time logging of streaming response chunks.
type StreamingLogWriter = internallogging.StreamingLogWriter

// StreamingLogDropReporter is an optional interface for recording middleware queue drops before Close.
// Implement it to include a drop marker in custom logs, storing the counts outside the chunk queue.
// Built-in file and Home writers include the marker when publishing the completed log.
// Custom writers without this interface receive only response chunks through WriteChunkAsync.
type StreamingLogDropReporter = internallogging.StreamingLogDropReporter

// FileRequestLogger implements RequestLogger using file-based storage.
type FileRequestLogger = internallogging.FileRequestLogger

// NewFileRequestLogger creates a new file-based request logger with default error log retention (10 files).
func NewFileRequestLogger(enabled bool, logsDir string, configDir string) *FileRequestLogger {
	return internallogging.NewFileRequestLogger(enabled, logsDir, configDir, defaultErrorLogsMaxFiles)
}

// NewFileRequestLoggerWithOptions creates a new file-based request logger with configurable error log retention.
func NewFileRequestLoggerWithOptions(enabled bool, logsDir string, configDir string, errorLogsMaxFiles int) *FileRequestLogger {
	return internallogging.NewFileRequestLogger(enabled, logsDir, configDir, errorLogsMaxFiles)
}
