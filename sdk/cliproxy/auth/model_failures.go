package auth

import (
	"maps"
	"time"
)

// modelFailureMessageLimit caps the stored upstream message, in runes.
const modelFailureMessageLimit = 300

// ModelFailure is the latest upstream failure one credential returned for one
// model. Unlike ModelState.LastError, a later success does not clear it, so an
// operator can still see why requests failed after the model recovered.
type ModelFailure struct {
	Code       string    `json:"code,omitempty"`
	HTTPStatus int       `json:"http_status"`
	Message    string    `json:"message,omitempty"`
	At         time.Time `json:"at"`
}

// recordModelFailure stores err as the latest failure for model. Failures
// without an upstream HTTP status, such as client cancellations, are skipped.
func (a *Auth) recordModelFailure(model string, err *Error, now time.Time) {
	if a == nil || model == "" || err == nil {
		return
	}
	status := statusCodeFromResult(err)
	if status == 0 {
		return
	}
	message := err.Message
	if runes := []rune(message); len(runes) > modelFailureMessageLimit {
		message = string(runes[:modelFailureMessageLimit])
	}
	if a.modelFailures == nil {
		a.modelFailures = make(map[string]ModelFailure)
	}
	a.modelFailures[model] = ModelFailure{Code: err.Code, HTTPStatus: status, Message: message, At: now.UTC()}
}

// ModelFailuresSnapshot returns a copy of the latest failure per model.
func (a *Auth) ModelFailuresSnapshot() map[string]ModelFailure {
	if a == nil || len(a.modelFailures) == 0 {
		return nil
	}
	return maps.Clone(a.modelFailures)
}
