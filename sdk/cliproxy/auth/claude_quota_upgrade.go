package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v8/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

const (
	claudeRateLimitTierKey     = "claude_rate_limit_tier"
	claudeTierCheckBudget      = 10 * time.Second
	claudeTierCheckBackoff     = 15 * time.Minute
	claudeTierUpgradeRetry     = time.Minute
	claudeTierChecksPerRequest = 3
	claudeMaxTier              = "default_claude_max_20x"
)

type claudeOAuthRateLimitTierReader interface {
	ClaudeOAuthRateLimitTier(context.Context, *Auth) (string, error)
}

// claudeQuotaTierState binds a cooldown to the tier and identity known when it was recorded.
type claudeQuotaTierState struct {
	Tier      string `json:"tier"`
	Identity  string `json:"identity"`
	Attempted string `json:"attempted,omitempty"`
}

type claudeTierCheck struct {
	identity string
	inFlight bool
	nextAt   time.Time
}

type claudeUpgradeAttempt struct {
	auth     *Auth
	executor ProviderExecutor
	tier     string
	cooldown error
}

type claudeUpgradeAttemptKey struct{}

func claudeUpgradeAttemptFromContext(ctx context.Context) *claudeUpgradeAttempt {
	if ctx == nil {
		return nil
	}
	attempt, _ := ctx.Value(claudeUpgradeAttemptKey{}).(*claudeUpgradeAttempt)
	return attempt
}

func claudeTierRank(tier string) int {
	switch tier {
	case "default_claude_pro":
		return 1
	case "default_claude_max_5x":
		return 2
	case claudeMaxTier:
		return 3
	default:
		return 0
	}
}

func claudeTierIdentity(auth *Auth) string {
	if auth == nil || !strings.EqualFold(auth.Provider, "claude") || auth.AuthKind() != AuthKindOAuth {
		return ""
	}
	account := authMetadataString(auth, "account_uuid")
	organization := authMetadataString(auth, "organization_uuid")
	if account == "" || organization == "" {
		return ""
	}
	identity := sha256.Sum256([]byte(account + "\x00" + organization))
	return hex.EncodeToString(identity[:])
}

func claudeQuotaTierAtFailure(auth *Auth) claudeQuotaTierState {
	identity := claudeTierIdentity(auth)
	tier := authMetadataString(auth, claudeRateLimitTierKey)
	if identity == "" || claudeTierRank(tier) == 0 {
		return claudeQuotaTierState{}
	}
	return claudeQuotaTierState{Tier: tier, Identity: identity}
}

func hasActiveClaudeQuotaTier(auth *Auth, now time.Time) bool {
	return auth != nil && auth.Quota.Exceeded && auth.Quota.Reason == "credential_quota" &&
		auth.Quota.NextRecoverAt.After(now) && claudeTierRank(auth.Quota.claudeTier.Tier) > 0 &&
		auth.Quota.claudeTier.Identity != "" && auth.Quota.claudeTier.Identity == claudeTierIdentity(auth)
}

// claudeUpgradeModelView removes the credential-wide layer from a private
// snapshot. The selector still sees each model's independent restrictions.
func claudeUpgradeModelView(auth *Auth, now time.Time) *Auth {
	view := auth.Clone()
	for _, state := range view.ModelStates {
		if state == nil || state.Status == StatusDisabled || (!state.credentialQuotaPropagated && state.Quota.Reason != "credential_quota") {
			continue
		}
		if shadow := state.credentialQuotaShadow; shadow != nil {
			*state = *shadow
		} else {
			resetModelState(state, now)
		}
	}
	applyCooldownFields(&view.Quota, QuotaState{})
	view.Unavailable = false
	view.NextRetryAfter = time.Time{}
	updateAggregatedAvailability(view, now)
	return view
}

func claudeUpgradeSelectorAllowed(selector Selector) bool {
	if affinity, ok := selector.(*SessionAffinitySelector); ok && affinity != nil {
		if affinity.rebindOnNever {
			return false
		}
		selector = affinity.fallback
	}
	return isBuiltInSelector(selectorForActualRouting(selector)) || isActiveQuotaAwareSelector(selector)
}

func (m *Manager) findClaudeUpgradeAttempt(ctx context.Context, providers []string, model string, opts cliproxyexecutor.Options, cooldown error) *claudeUpgradeAttempt {
	var modelCooldown *modelCooldownError
	generate, explicit := generateFromOptions(opts)
	if explicit && !generate || len(providers) != 1 || providers[0] != "claude" || m.HomeEnabled() || m.hasPluginScheduler() ||
		!errors.As(cooldown, &modelCooldown) || pinnedAuthIDFromMetadata(opts.Metadata) != "" ||
		model == "" || strings.Contains(model, "/") || strings.Contains(requestedModelAliasFromOptions(opts, model), "/") {
		return nil
	}
	profileCtx, cancelProfile := context.WithTimeout(ctx, claudeTierCheckBudget)
	defer cancelProfile()
	for checks := 0; checks < claudeTierChecksPerRequest && profileCtx.Err() == nil; checks++ {
		candidate, executor, check := m.reserveClaudeTierCheck(profileCtx, model, opts)
		if candidate == nil {
			return nil
		}
		reader := executor.(claudeOAuthRateLimitTierReader)
		tier, errProbe := reader.ClaudeOAuthRateLimitTier(profileCtx, candidate)
		higherTier := errProbe == nil && claudeUpgradeTierIncreased(candidate, tier)
		profileInterrupted := profileCtx.Err() != nil
		interval := claudeTierCheckBackoff
		if higherTier || profileInterrupted {
			interval = claudeTierUpgradeRetry
		}
		now := time.Now()
		m.mu.Lock()
		if m.claudeTierChecks[candidate.ID] == check {
			check.inFlight = false
			check.nextAt = now.Add(interval)
			m.claudeTierChecks[candidate.ID] = check
		}
		ready := higherTier && profileCtx.Err() == nil &&
			claudeUpgradeSameCredential(m.auths[candidate.ID], candidate, now)
		m.mu.Unlock()
		if ready {
			return &claudeUpgradeAttempt{auth: candidate, executor: executor, tier: tier, cooldown: cooldown}
		}
	}
	return nil
}

func (m *Manager) reserveClaudeTierCheck(ctx context.Context, model string, opts cliproxyexecutor.Options) (*Auth, ProviderExecutor, claudeTierCheck) {
	registration := registry.GetGlobalRegistry()
	eligibility := authSelectionEligibilityForRequest(ctx, opts)
	now := time.Now()
	m.mu.Lock()
	defer m.mu.Unlock()
	executor, _ := m.executorLocked("claude")
	if _, ok := executor.(claudeOAuthRateLimitTierReader); !ok || !claudeUpgradeSelectorAllowed(m.selector) {
		return nil, nil, claudeTierCheck{}
	}
	var candidate *Auth
	for _, auth := range m.auths {
		if !hasActiveClaudeQuotaTier(auth, now) || auth.RequiresSignIn || !eligibility.allows(auth) ||
			!m.authSupportsRouteModel(registration, auth, model) || authWeight(auth) <= 0 ||
			auth.Quota.claudeTier.Tier == claudeMaxTier || auth.Quota.claudeTier.Attempted == claudeMaxTier {
			continue
		}
		check := m.claudeTierChecks[auth.ID]
		if check.inFlight || check.identity == auth.Quota.claudeTier.Identity && check.nextAt.After(now) {
			continue
		}
		view := claudeUpgradeModelView(auth, now)
		if blocked, _, _ := isAuthBlockedForModelForRequest(ctx, view, m.selectionModelKeyForAuth(auth, model), now, opts.Headers); blocked {
			continue
		}
		if candidate == nil || authPriority(auth) > authPriority(candidate) || authPriority(auth) == authPriority(candidate) && auth.ID < candidate.ID {
			candidate = auth
		}
	}
	if candidate == nil {
		return nil, nil, claudeTierCheck{}
	}
	if m.claudeTierChecks == nil {
		m.claudeTierChecks = make(map[string]claudeTierCheck)
	}
	check := claudeTierCheck{identity: candidate.Quota.claudeTier.Identity, inFlight: true, nextAt: now}
	m.claudeTierChecks[candidate.ID] = check
	return candidate.Clone(), executor, check
}

func claudeUpgradeTierIncreased(auth *Auth, tier string) bool {
	return claudeTierRank(tier) > claudeTierRank(auth.Quota.claudeTier.Tier) &&
		claudeTierRank(tier) > claudeTierRank(auth.Quota.claudeTier.Attempted)
}

func claudeUpgradeSameCredential(auth, observed *Auth, now time.Time) bool {
	return hasActiveClaudeQuotaTier(auth, now) && !auth.Disabled && auth.Status != StatusDisabled &&
		!auth.RequiresSignIn && !hasUnauthorizedAuthFailure(auth) &&
		auth.Generation == observed.Generation && auth.CredentialVersion == observed.CredentialVersion &&
		auth.RegistrationEpoch == observed.RegistrationEpoch && auth.Quota.claudeTier.Identity == observed.Quota.claudeTier.Identity &&
		auth.Quota.claudeTier.Tier == observed.Quota.claudeTier.Tier
}

func (m *Manager) claimClaudeUpgradeAttempt(ctx context.Context, attempt *claudeUpgradeAttempt, auth *Auth, routeModel, resultModel string, opts cliproxyexecutor.Options) bool {
	if ctx.Err() != nil || auth == nil || auth.ID != attempt.auth.ID ||
		auth.Generation != attempt.auth.Generation || auth.CredentialVersion != attempt.auth.CredentialVersion ||
		auth.RegistrationEpoch != attempt.auth.RegistrationEpoch || m.hasPluginScheduler() {
		return false
	}
	// Serialize the claim with cooldown store swaps. When a store is configured,
	// commit the spent tier before the upstream request so a restart cannot replay it.
	m.configCooldownMu.Lock()
	defer m.configCooldownMu.Unlock()
	m.mu.Lock()
	current := m.auths[auth.ID]
	now := time.Now()
	registration := registry.GetGlobalRegistry()
	if !claudeUpgradeSelectorAllowed(m.selector) || m.HomeEnabled() ||
		!claudeUpgradeSameCredential(current, attempt.auth, now) ||
		!claudeUpgradeTierIncreased(current, attempt.tier) ||
		!m.authSupportsRouteModel(registration, current, routeModel) {
		m.mu.Unlock()
		return false
	}
	view := claudeUpgradeModelView(current, now)
	if blocked, _, _ := isAuthBlockedForModelForRequest(ctx, view, resultModel, now, opts.Headers); blocked {
		m.mu.Unlock()
		return false
	}
	previous := current.Quota.claudeTier
	claimed := previous
	claimed.Attempted = attempt.tier
	current.Quota.claudeTier = claimed
	store := m.cooldownStore
	m.mu.Unlock()
	canDispatch := m.persistCooldownStatesToLocked(ctx, store) && ctx.Err() == nil && !m.hasPluginScheduler()
	m.mu.Lock()
	current = m.auths[auth.ID]
	stillClaimed := current != nil && current.RegistrationEpoch == auth.RegistrationEpoch && current.Quota.claudeTier == claimed
	if canDispatch && stillClaimed && ctx.Err() == nil && !m.HomeEnabled() && claudeUpgradeSelectorAllowed(m.selector) &&
		claudeUpgradeSameCredential(current, attempt.auth, time.Now()) {
		m.mu.Unlock()
		return true
	}
	if stillClaimed {
		current.Quota.claudeTier = previous
	}
	m.mu.Unlock()
	if stillClaimed {
		// No request was sent. Undo the saved claim even if the client disconnected.
		m.persistCooldownStatesToLocked(context.WithoutCancel(ctx), store)
	}
	return false
}

func (attempt *claudeUpgradeAttempt) appliesToResult(auth *Auth, result Result, now time.Time) bool {
	return attempt != nil && result.ExecutionSeq > 0 && !result.SkipQuotaObservation &&
		result.AuthID == attempt.auth.ID && result.RegistrationEpoch == attempt.auth.RegistrationEpoch &&
		result.CredentialVersion == attempt.auth.CredentialVersion && claudeUpgradeSameCredential(auth, attempt.auth, now) &&
		auth.Quota.claudeTier.Attempted == attempt.tier
}
