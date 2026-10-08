"""Trusted effect Worker orchestration; actual PG ports govern every admission.

Ledger methods are protected PG ports, never optional callbacks granting
authority. Runtime completion does not assert external business success.
"""
from nexloop_eios.effect_provider import HttpEffectProvider, EffectProviderUnknown, EffectProviderQueryUnavailable


class EffectDispatchUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('effect_dispatch_unavailable')


class EffectDispatcher:
    def __init__(self, ledger, provider, *, lease_seconds=30):
        if type(provider) is not HttpEffectProvider or type(lease_seconds) is not int or not 3 <= lease_seconds <= 300:
            raise ValueError('effect_dispatch_configuration_invalid')
        if provider.configuration.timeout > lease_seconds / 3:
            raise ValueError('effect_dispatch_configuration_invalid')
        self.ledger, self.provider, self.lease_seconds = ledger, provider, lease_seconds
        self.provider_profile_digest = provider.profile_digest

    def run_once(self):
        """Claim once. An orphan dispatch is always query-first, never new send.

        Only 'fresh' PG stage may obtain prepare_dispatch. PG must transition to
        dispatching and commit before returning to this method. Failure before
        that commit produces zero POST. Claim ownership is independent of Run.
        """
        try:
            job = self.ledger.claim_effect(lease_seconds=self.lease_seconds)
        except Exception:
            raise EffectDispatchUnavailable() from None
        if job is None:
            return {'claimed': False}
        identity = {key: job[key] for key in ('intent_id', 'fence')}
        # Trusted ledger returns only fixed intent payload; provider configuration
        # and credentials never originate in stored model/tool request parameters.
        if job['stage'] not in {'fresh', 'reconcile'}:
            raise EffectDispatchUnavailable()
        if job['stage'] == 'fresh':
            try:
                admitted = self.ledger.prepare_effect_dispatch(**identity,provider_profile_digest=self.provider_profile_digest)
                # The returned payload is the authoritative frozen record, not
                # unchecked contents of the initial claim response.
                receipt = self.provider.dispatch(intent_id=identity['intent_id'],
                    payload_digest=admitted['provider_payload_digest'],
                    parameters=admitted['parameters'])
            except EffectProviderUnknown:
                # This marker is best effort. If PG is unavailable or process
                # dies, committed dispatching itself drives query-first reclaim.
                try: self.ledger.record_effect_unknown(**identity)
                except Exception: pass
                return {'claimed': True, 'status': 'unknown', 'business_action_success': False}
            except Exception:
                # Fail closed. Do not convert an uncertain PG commit into a send
                # or independently retry the original provider request.
                return {'claimed': True, 'status': 'admission_unavailable', 'business_action_success': False}
        else:
            try:
                # Query requires its own current authority. Source TTL expiring
                # blocks new dispatch, but must not invent a second effect.
                admitted = self.ledger.authorize_effect_query(**identity,provider_profile_digest=self.provider_profile_digest)
                receipt = self.provider.query(intent_id=identity['intent_id'],
                    payload_digest=admitted['provider_payload_digest'])
            except EffectProviderQueryUnavailable:
                try: self.ledger.record_effect_unknown(**identity)
                except Exception: pass
                return {'claimed': True, 'status': 'unknown', 'business_action_success': False}
            except Exception:
                return {'claimed': True, 'status': 'query_unavailable', 'business_action_success': False}
        if receipt.state == 'not_found':
            # This minimal policy deliberately has no automatic resend. A future
            # governed retry must prove provider's safe-to-retry protocol and all
            # current origin/executor/controls/budget again under the same key.
            try: self.ledger.record_effect_unknown(**identity)
            except Exception: pass
            return {'claimed': True, 'status': 'unknown', 'business_action_success': False}
        try:
            record = self.ledger.record_effect_query_observation if job['stage'] == 'reconcile' else self.ledger.record_effect_observation
            query_args = {'query_id': admitted['query_id']} if job['stage'] == 'reconcile' else {}
            result = record(**identity, **query_args,
                provider_profile_digest=self.provider_profile_digest,
                provider_payload_digest=receipt.payload_digest,
                provider_state=receipt.state,
                provider_reference=receipt.provider_reference)
        except Exception:
            # Provider already may have accepted/fulfilled. Existing committed
            # dispatching remains reconciliation work; no mark_retryable/resend.
            return {'claimed': True, 'status': 'record_unavailable', 'business_action_success': False}
        if (type(result) is not dict or result.get('state') not in
            {'accepted', 'dispatching', 'unknown', 'observed_fulfilled', 'fulfilled'}
            or result.get('provider_state') != receipt.state
            or receipt.state == 'accepted' and result['state'] != 'dispatching'
            or type(result.get('business_action_success')) is not bool
            or type(result.get('governed_claim_finalized')) is not bool
            or result['business_action_success'] and
               (result['state'] != 'fulfilled' or receipt.state != 'fulfilled' or not result['governed_claim_finalized'])):
            return {'claimed': True, 'status': 'record_unavailable', 'business_action_success': False}
        # Only actual PG's atomically finalized claim+receipt may assert business
        # success. A READ-authorized observation after revoke is not that grant.
        return {'claimed': True, 'status': result['state'], 'provider_state': result['provider_state'],
                'governed_claim_finalized': result['governed_claim_finalized'],
                'business_action_success': result['business_action_success'] is True}
