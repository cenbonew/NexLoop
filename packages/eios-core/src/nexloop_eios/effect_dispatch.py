"""Trusted effect Worker orchestration; actual PG ports govern every admission.

Ledger methods are protected PG ports, never optional callbacks granting
authority. Runtime completion does not assert external business success.
"""
from nexloop_eios.effect_provider import HttpEffectProvider, EffectProviderUnknown, EffectProviderQueryUnavailable


class EffectDispatchUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('effect_dispatch_unavailable')


class ProviderRoutes:
    """NX-037: one provider per Action stable name (nexloop.service.request -> the NexLoop delivery service,
    nexloop.enterprise.deliver -> the enterprise HTTPS endpoint). An Action without a route is never dispatched: there is no
    default provider. Two routes never share an origin or a credential file, so one channel's credential cannot reach the
    other."""

    def __init__(self, routes):
        if type(routes) is not dict or not routes or any(type(name) is not str or not name or type(p) is not HttpEffectProvider
                                                         for name, p in routes.items()):
            raise ValueError('effect_dispatch_configuration_invalid')
        providers = list(routes.values())
        for i, a in enumerate(providers):
            for b in providers[i + 1:]:
                same_credential = a.configuration.credential_file is not None and a.configuration.credential_file == b.configuration.credential_file
                if a.configuration.origin == b.configuration.origin or same_credential or a.profile_digest == b.profile_digest:
                    raise ValueError('effect_dispatch_configuration_invalid')
        self.routes = dict(routes)

    def for_action(self, name):
        return self.routes.get(name)

    def providers(self):
        return list(self.routes.values())


class EffectDispatcher:
    def __init__(self, ledger, provider, *, lease_seconds=30):
        """`provider` is one HttpEffectProvider for every claimed intent (a single-channel deployment), or ProviderRoutes:
        the provider is chosen by the claimed intent's Action, and an intent whose Action has no route is not dispatched."""
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 300:
            raise ValueError('effect_dispatch_configuration_invalid')
        if type(provider) is ProviderRoutes:
            providers = provider.providers()
        elif type(provider) is HttpEffectProvider:
            providers = [provider]
        else:
            raise ValueError('effect_dispatch_configuration_invalid')
        if any(p.configuration.timeout > lease_seconds / 3 for p in providers):
            raise ValueError('effect_dispatch_configuration_invalid')
        self.ledger, self.lease_seconds = ledger, lease_seconds
        self.routes = provider if type(provider) is ProviderRoutes else None
        self.provider = provider if type(provider) is HttpEffectProvider else None
        self.provider_profile_digest = self.provider.profile_digest if self.provider else None

    def _origin_expired(self, identity):
        # 0174: an expired origin Run can never authorize a dispatch or a query again; stop instead of retrying with it.
        # Best effort: any failure here (database outage included) leaves the existing unavailable path unchanged.
        settle = getattr(self.ledger, 'settle_expired_origin', None)
        try:
            return settle(intent_id=identity['intent_id']) if settle else None
        except Exception:
            return None

    def _back_off(self, identity):
        back_off = getattr(self.ledger, 'back_off_unknown', None)
        if back_off:
            try:
                back_off(intent_id=identity['intent_id'])
            except Exception:
                pass

    def _bound_provider(self, identity):
        for candidate in self.routes.providers():
            try:
                return candidate, self.ledger.authorize_effect_query(**identity, provider_profile_digest=candidate.profile_digest)
            except Exception:
                continue
        raise EffectDispatchUnavailable()

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
        provider = self.provider
        if self.routes is not None and job['stage'] == 'fresh':
            # The Action comes from the PG claim; no route means no dispatch (the lease lapses, nothing is admitted or sent).
            provider = self.routes.for_action(job.get('action'))
            if provider is None:
                return {'claimed': True, 'status': 'unrouted', 'business_action_success': False}
        if job['stage'] == 'fresh':
            try:
                admitted = self.ledger.prepare_effect_dispatch(**identity,provider_profile_digest=provider.profile_digest)
                # The returned payload is the authoritative frozen record, not
                # unchecked contents of the initial claim response.
                receipt = provider.dispatch(intent_id=identity['intent_id'],
                    payload_digest=admitted['provider_payload_digest'],
                    parameters=admitted['parameters'])
            except EffectProviderUnknown:
                # This marker is best effort. If PG is unavailable or process
                # dies, committed dispatching itself drives query-first reclaim.
                try: self.ledger.record_effect_unknown(**identity)
                except Exception: pass
                self._back_off(identity)
                return {'claimed': True, 'status': 'unknown', 'business_action_success': False}
            except Exception:
                # Fail closed. Do not convert an uncertain PG commit into a send
                # or independently retry the original provider request.
                if self._origin_expired(identity) == 'failed':
                    return {'claimed': True, 'status': 'origin_expired', 'business_action_success': False}
                return {'claimed': True, 'status': 'admission_unavailable', 'business_action_success': False}
        else:
            try:
                # Query requires its own current authority. Source TTL expiring
                # blocks new dispatch, but must not invent a second effect.
                if self.routes is None:
                    admitted = self.ledger.authorize_effect_query(**identity,provider_profile_digest=provider.profile_digest)
                else:
                    # Unknown results are queried with the provider that sent them: the intent is bound to that provider's
                    # profile digest at dispatch (0044) and the ledger authorizes the query for that digest only.
                    provider, admitted = self._bound_provider(identity)
                receipt = provider.query(intent_id=identity['intent_id'],
                    payload_digest=admitted['provider_payload_digest'])
            except EffectProviderQueryUnavailable:
                try: self.ledger.record_effect_unknown(**identity)
                except Exception: pass
                self._back_off(identity)
                return {'claimed': True, 'status': 'unknown', 'business_action_success': False}
            except Exception:
                if self._origin_expired(identity) == 'stopped':
                    return {'claimed': True, 'status': 'origin_expired', 'business_action_success': False}
                return {'claimed': True, 'status': 'query_unavailable', 'business_action_success': False}
        if receipt.state == 'not_found':
            # This minimal policy deliberately has no automatic resend. A future
            # governed retry must prove provider's safe-to-retry protocol and all
            # current origin/executor/controls/budget again under the same key.
            try: self.ledger.record_effect_unknown(**identity)
            except Exception: pass
            self._back_off(identity)
            return {'claimed': True, 'status': 'unknown', 'business_action_success': False}
        try:
            record = self.ledger.record_effect_query_observation if job['stage'] == 'reconcile' else self.ledger.record_effect_observation
            query_args = {'query_id': admitted['query_id']} if job['stage'] == 'reconcile' else {}
            result = record(**identity, **query_args,
                provider_profile_digest=provider.profile_digest,
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
