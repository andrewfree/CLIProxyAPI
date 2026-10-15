package auth

import (
	"math"
	"net/http"
	"time"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v8/internal/config"
)

func quotaCooldownLimit(cfg *internalconfig.Config) time.Duration {
	if cfg == nil || cfg.Home.Enabled || cfg.QuotaCooldownMaxSeconds <= 0 {
		return 0
	}
	seconds := int64(cfg.QuotaCooldownMaxSeconds)
	if seconds > math.MaxInt64/int64(time.Second) {
		return time.Duration(math.MaxInt64)
	}
	limit := time.Duration(seconds) * time.Second
	if limit < minQuotaCooldownFloor {
		return minQuotaCooldownFloor
	}
	return limit
}

func limitQuotaDeadline(next, anchor time.Time, limit time.Duration) time.Time {
	if limit > 0 && next.After(anchor.Add(limit)) {
		return anchor.Add(limit)
	}
	return next
}

// Existing state can also carry a non-quota failure. Only a deadline owned by
// quota may move earlier; legacy records with another last error stay conservative.
func limitQuotaState(quota *QuotaState, nextRetry *time.Time, lastErr *Error, updatedAt, now time.Time, limit time.Duration) bool {
	if limit <= 0 || !quota.Exceeded || (quota.Reason != "quota" && quota.Reason != "credential_quota") {
		return false
	}
	if updatedAt.IsZero() || updatedAt.After(now) {
		updatedAt = now
	}
	previous := quota.NextRecoverAt
	next := limitQuotaDeadline(previous, updatedAt, limit)
	if next.Equal(previous) {
		return false
	}
	// Aggregates pair the earliest model retry with a later quota deadline.
	if !nextRetry.After(previous) && next.Before(*nextRetry) && (lastErr == nil || lastErr.HTTPStatus == http.StatusTooManyRequests) {
		*nextRetry = next
	}
	quota.NextRecoverAt = next
	return true
}

func (m *Manager) limitExistingQuotaCooldowns(cfg *internalconfig.Config) bool {
	limit := quotaCooldownLimit(cfg)
	if limit == 0 {
		return false
	}
	now := time.Now()
	var snapshots []*Auth
	m.mu.Lock()
	for _, auth := range m.auths {
		if auth == nil || auth.Disabled || auth.Status == StatusDisabled || hasUnauthorizedAuthFailure(auth) || quotaCooldownDisabledForAuthWithConfig(auth, cfg) {
			continue
		}
		quotaOnlyAggregate := auth.Quota.Reason == "quota" && !auth.NextRetryAfter.After(auth.Quota.NextRecoverAt) &&
			(auth.LastError == nil || auth.LastError.HTTPStatus == http.StatusTooManyRequests)
		changed := limitQuotaState(&auth.Quota, &auth.NextRetryAfter, auth.LastError, auth.UpdatedAt, now, limit)
		for _, state := range auth.ModelStates {
			if state == nil || state.Status == StatusDisabled {
				continue
			}
			updatedAt := state.UpdatedAt
			// Older versions did not timestamp sibling quota propagation.
			if state.Quota.Reason == "credential_quota" && auth.UpdatedAt.After(updatedAt) {
				updatedAt = auth.UpdatedAt
			}
			if limitQuotaState(&state.Quota, &state.NextRetryAfter, state.LastError, updatedAt, now, limit) {
				changed = true
			}
		}
		if changed && quotaOnlyAggregate && len(auth.ModelStates) > 0 {
			updateAggregatedAvailability(auth, now)
		}
		if changed {
			// Keep the failure timestamps: reloads must not renew a quota window.
			auth.Generation++
			snapshots = append(snapshots, auth.Clone())
		}
	}
	m.mu.Unlock()
	if m.scheduler != nil {
		for _, snapshot := range snapshots {
			m.scheduler.upsertAuth(snapshot)
		}
	}
	return len(snapshots) > 0
}
