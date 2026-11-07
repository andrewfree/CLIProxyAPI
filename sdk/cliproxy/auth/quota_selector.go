package auth

import (
	"context"
	"sync"
	"time"

	log "github.com/sirupsen/logrus"

	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

// QuotaAwareSelectorOptions configures passive quota-based selection.
type QuotaAwareSelectorOptions struct {
	// Shadow evaluates quota-based choices while returning ordinary round-robin choices.
	Shadow bool
}

// QuotaAwareSelector uses recent quota observations to spread requests across
// eligible credentials. Selection is serialized; it does not track active requests.
// Its zero value is ready for use.
type QuotaAwareSelector struct {
	mu         sync.Mutex
	states     map[string]*quotaAwareSelectionState
	maxKeys    int
	nowFunc    func() time.Time
	shadow     bool
	roundRobin RoundRobinSelector
}

type quotaAwareSelectionState struct {
	// Credits are fractions of one selection, so changing forecast magnitudes
	// cannot turn old credits into a large burst at the new weight scale.
	current    map[string]float64
	lastPicked string
}

// NewQuotaAwareSelector creates a selector with independent forecast and shadow rotation state.
func NewQuotaAwareSelector(opts QuotaAwareSelectorOptions) *QuotaAwareSelector {
	return &QuotaAwareSelector{shadow: opts.Shadow}
}

func quotaAwareShadowSelector(selector Selector) *QuotaAwareSelector {
	if quota, ok := selector.(*QuotaAwareSelector); ok && quota != nil && quota.shadow {
		return quota
	}
	return nil
}

func isActiveQuotaAwareSelector(selector Selector) bool {
	quota, ok := selector.(*QuotaAwareSelector)
	return ok && quota != nil && !quota.shadow
}

// selectorForActualRouting keeps shadow requests on the manager's ordinary
// round-robin path, including its scheduler and legacy cursor semantics.
func selectorForActualRouting(selector Selector) Selector {
	if shadow := quotaAwareShadowSelector(selector); shadow != nil {
		return &shadow.roundRobin
	}
	return selector
}

// Pick selects an eligible credential using bounded smooth weighted rotation.
// Passive observations never create cooldowns or override an explicit auth pin.
func (s *QuotaAwareSelector) Pick(ctx context.Context, provider, model string, opts cliproxyexecutor.Options, auths []*Auth) (*Auth, error) {
	if pinnedID := pinnedAuthIDFromMetadata(opts.Metadata); pinnedID != "" {
		pinned := make([]*Auth, 0, 1)
		for _, candidate := range auths {
			if candidate != nil && candidate.ID == pinnedID {
				pinned = append(pinned, candidate)
				break
			}
		}
		auths = pinned
	}
	now := time.Now()
	if s.nowFunc != nil {
		now = s.nowFunc()
	}
	// Keep lower priority tiers until configured zero weights are removed. A
	// zero-weight high tier must not hide a usable lower tier in pooled routing.
	eligible, errAvailable := getSelectorAvailableAuthsAcrossPriorities(ctx, auths, provider, model, now)
	if errAvailable != nil {
		return nil, errAvailable
	}
	if ctx == nil {
		ctx = context.Background()
	}
	validatedCtx := context.WithValue(ctx, prevalidatedAuthCandidatesKey{}, true)

	if !s.shadow {
		s.mu.Lock()
		defer s.mu.Unlock()
		predicted, _, errPredicted := s.pickQuotaAware(validatedCtx, provider, model, eligible, now)
		return predicted, errPredicted
	}
	s.roundRobin.mu.Lock()
	s.roundRobin.maxKeys = s.maxKeys
	s.roundRobin.mu.Unlock()
	actual, errActual := s.roundRobin.Pick(validatedCtx, provider, model, opts, eligible)
	if errActual != nil {
		return nil, errActual
	}
	s.observeShadow(validatedCtx, provider, model, eligible, actual)
	return actual, nil
}

// observeShadow only advances forecast state. The manager or wrapped selector
// has already made the actual choice using its normal eligibility and cursors.
func (s *QuotaAwareSelector) observeShadow(ctx context.Context, provider, model string, eligible []*Auth, actual *Auth) {
	if s == nil || !s.shadow || actual == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	ctx = context.WithValue(ctx, prevalidatedAuthCandidatesKey{}, true)
	now := time.Now()
	if s.nowFunc != nil {
		now = s.nowFunc()
	}
	s.mu.Lock()
	predicted, known, errPredicted := s.pickQuotaAware(ctx, provider, model, eligible, now)
	s.mu.Unlock()
	log.WithFields(log.Fields{
		"candidate_count":       len(eligible),
		"known_candidate_count": known,
		"prediction_available":  errPredicted == nil,
		"prediction_differs":    predicted != nil && predicted.ID != actual.ID,
	}).Debug("quota-aware selector shadow evaluation")
}

// pickQuotaAware requires s.mu. Its state is separate from the actual shadow rotation.
func (s *QuotaAwareSelector) pickQuotaAware(ctx context.Context, provider, model string, eligible []*Auth, now time.Time) (*Auth, int, error) {
	candidates := eligible
	if len(candidates) > 1 {
		candidates = positiveWeightAuths(candidates)
	}
	available, errAvailable := getSelectorAvailableAuths(ctx, candidates, provider, model, now)
	if errAvailable != nil {
		return nil, 0, errAvailable
	}
	available = preferCodexWebsocketAuths(ctx, provider, available)
	key := provider + ":" + canonicalModelKey(model)
	if s.states == nil {
		s.states = make(map[string]*quotaAwareSelectionState)
	}
	limit := s.maxKeys
	if limit <= 0 {
		limit = 4096
	}
	if _, exists := s.states[key]; !exists && len(s.states) >= limit {
		s.states = make(map[string]*quotaAwareSelectionState)
	}
	state := s.states[key]
	if state == nil {
		state = &quotaAwareSelectionState{current: make(map[string]float64)}
		s.states[key] = state
	}
	if len(available) == 1 {
		state.lastPicked = available[0].ID
		state.prune(available)
		return available[0], 0, nil
	}
	weights := make([]int64, len(available))
	known := 0
	var total float64
	for index, candidate := range available {
		snapshot := quotaBalanceSnapshotFor(candidate, model, now, defaultQuotaBalanceMaxAge)
		if snapshot.Known {
			known++
		}
		weight := quotaBalanceEffectiveWeight(authWeight(candidate), snapshot, now)
		weights[index] = weight
		if weight > 0 {
			total += float64(weight)
		}
	}
	var picked *Auth
	if known == 0 || total <= 0 {
		// Missing observations use ordinary rotation. Exhausted forecast hints
		// also use rotation: only authoritative eligibility may block a request.
		picked = available[successorIndex(available, state.lastPicked)]
	} else {
		picked = state.pick(available, weights, total)
	}
	state.lastPicked = picked.ID
	state.prune(available)
	return picked, known, nil
}

func (s *quotaAwareSelectionState) pick(available []*Auth, weights []int64, total float64) *Auth {
	// Start ties after the last pick rather than repeatedly favoring the first
	// identity when observations change or candidates temporarily disappear.
	start := successorIndex(available, s.lastPicked)
	var picked *Auth
	var highest float64
	for offset := 0; offset < len(available); offset++ {
		index := (start + offset) % len(available)
		candidate := available[index]
		credit := quotaAwareBoundedCredit(s.current[candidate.ID])
		if weights[index] <= 0 {
			s.current[candidate.ID] = credit
			continue
		}
		credit = quotaAwareBoundedCredit(credit + float64(weights[index])/total)
		s.current[candidate.ID] = credit
		if picked == nil || credit > highest {
			picked = candidate
			highest = credit
		}
	}
	s.current[picked.ID] = quotaAwareBoundedCredit(s.current[picked.ID] - 1)
	return picked
}

func quotaAwareBoundedCredit(credit float64) float64 {
	if credit > 1 {
		return 1
	}
	if credit < -1 {
		return -1
	}
	return credit
}

func (s *quotaAwareSelectionState) prune(available []*Auth) {
	if len(s.current) <= maxSmoothWeightedStateEntries {
		return
	}
	present := make(map[string]struct{}, len(available))
	for _, candidate := range available {
		present[candidate.ID] = struct{}{}
	}
	for authID := range s.current {
		if _, exists := present[authID]; !exists {
			delete(s.current, authID)
		}
	}
}
