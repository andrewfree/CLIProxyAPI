package auth

import (
	"net/http"
	"strings"
	"time"
)

func applyClaudeVerifiedQuotaRecovery(auth *Auth, result Result, model string, headers http.Header, now time.Time) bool {
	if result.quotaRecheckGeneration == nil || auth.Generation != *result.quotaRecheckGeneration ||
		!result.Success || !result.CompletedResponse || result.SkipQuotaObservation ||
		!strings.EqualFold(auth.Provider, "claude") || auth.Disabled || auth.Status == StatusDisabled || auth.RequiresSignIn ||
		hasUnauthorizedAuthFailure(auth) || auth.Quota.Reason != "credential_quota" || !auth.Quota.Exceeded {
		return false
	}
	if state := auth.ModelStates[model]; state != nil {
		if state.Status == StatusDisabled || state.credentialQuotaShadow != nil && state.credentialQuotaShadow.Status == StatusDisabled {
			return false
		}
	}
	signals := collectQuotaSignals("claude", headers)
	status := signals["Anthropic-Ratelimit-Unified-Status"]
	if (status != "allowed" && status != "allowed_warning") || hasRejectedClaudeUnifiedSignals(signals) {
		return false
	}
	applyCooldownFields(&auth.Quota, QuotaState{})
	for key, state := range auth.ModelStates {
		if state == nil {
			continue
		}
		if key == model {
			resetModelState(state, now)
			continue
		}
		if !state.credentialQuotaPropagated && state.Quota.Reason != "credential_quota" {
			continue
		}
		if shadow := state.credentialQuotaShadow; shadow != nil && isModelStateActiveCooldown(shadow, now) {
			*state = *shadow.Clone()
		} else {
			resetModelState(state, now)
		}
	}
	auth.Status = StatusActive
	auth.StatusMessage = ""
	auth.LastError = nil
	auth.Unavailable = false
	auth.NextRetryAfter = time.Time{}
	updateAggregatedAvailability(auth, now)
	return true
}
