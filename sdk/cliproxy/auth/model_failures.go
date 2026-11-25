package auth

import (
	"errors"
	"maps"
	"regexp"
	"strings"
	"time"

	"github.com/MoeForge/moego/ojson"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

// failureTokenPattern bounds the code and upstream type kept for a failure: a
// short machine-readable token such as overloaded_error, never free-form text.
var failureTokenPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

// ModelFailure is the latest upstream failure one credential returned for one
// model. Unlike ModelState.LastError, a later success does not clear it, so an
// operator can still see why requests failed after the model recovered. Only
// structured fields are kept; the upstream message body is not stored.
type ModelFailure struct {
	HTTPStatus int    `json:"http_status"`
	Code       string `json:"code,omitempty"`
	// Type is the upstream error.type, for example overloaded_error.
	Type string    `json:"type,omitempty"`
	At   time.Time `json:"at"`
}

// recordModelFailure stores err as the latest failure for model. kind is the
// upstream error.type when the caller already has it; otherwise it is read from
// err.Message. Failures without an upstream HTTP status, such as client
// cancellations, are skipped.
func (a *Auth) recordModelFailure(model string, err *Error, kind string, now time.Time) {
	if a == nil || model == "" || err == nil {
		return
	}
	status := statusCodeFromResult(err)
	if status == 0 {
		return
	}
	if kind == "" {
		kind = upstreamErrorType([]byte(err.Message))
	}
	if a.modelFailures == nil {
		a.modelFailures = make(map[string]ModelFailure)
	}
	code := strings.TrimSpace(err.Code)
	if !failureTokenPattern.MatchString(code) {
		code = "" // plugin errors can supply free-form codes
	}
	a.modelFailures[model] = ModelFailure{HTTPStatus: status, Code: code, Type: kind, At: now.UTC()}
}

// ModelFailuresSnapshot returns a copy of the latest failure per model.
func (a *Auth) ModelFailuresSnapshot() map[string]ModelFailure {
	if a == nil || len(a.modelFailures) == 0 {
		return nil
	}
	return maps.Clone(a.modelFailures)
}

// upstreamErrorTypeFromError reads error.type from a response body that an
// error carries apart from its message, such as a Claude Fast direct response.
func upstreamErrorTypeFromError(err error) string {
	var terminated *cliproxyexecutor.RequestTerminatedError
	if errors.As(err, &terminated) && terminated != nil {
		return upstreamErrorType(terminated.Body)
	}
	return ""
}

// upstreamErrorType returns error.type from an upstream JSON error body, or
// from the first JSON object in a message that wraps one.
func upstreamErrorType(body []byte) string {
	text := strings.TrimSpace(string(body))
	if !ojson.Valid(text) {
		idx := strings.Index(text, "{")
		if idx < 0 || !ojson.Valid(text[idx:]) {
			return ""
		}
		text = text[idx:]
	}
	kind := strings.TrimSpace(ojson.Get(text, "error.type").String())
	if !failureTokenPattern.MatchString(kind) {
		return ""
	}
	return kind
}
