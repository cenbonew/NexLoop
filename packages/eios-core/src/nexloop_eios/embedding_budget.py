"""Embedding calls in the tenant's model budget (owner 2026-10-18, option B: vector recall and candidate merge on stage; PR #8
review).

Background model calls (extraction, matching) have no budget of their own; embeddings of the recall-index service and the
claim matcher are counted in the tenant's `model` budget (NX-022, the same budget Runs reserve), priced by a private tariff
(`nexloop-embedding-tariff/1`: model, currency, price per million input tokens; the owner supplies the price, nothing here invents
one). When the tenant has a model budget:
- before a tick claims any work, the budget must be open now (authz.nexloop_budget_open: inside its period, not used up);
  otherwise the tick claims nothing;
- before each provider call, the estimated cost (one token per character, an upper bound for these short texts) is reserved
  atomically (authz.nexloop_reserve_budget checks the period and the limit); a refusal raises EmbeddingBudgetDenied and no call
  is made;
- after the call, if the provider reported more tokens than estimated, the difference is reserved too (capped at what is left).
When the tenant has no model budget nothing is reserved (as for Runs: no configured budget means no budget gate). Amounts are
rounded up to 0.000001."""
import json
import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from nexloop_eios.private_configuration import read_private_text

SCHEMA = 'nexloop-embedding-tariff/1'
QUANTUM = Decimal('0.000001')


class EmbeddingTariffError(ValueError):
    pass


class EmbeddingBudgetDenied(RuntimeError):
    """The tenant model budget refused the reservation before the provider call (period closed or used up)."""


@dataclass(frozen=True)
class EmbeddingTariff:
    model: str
    currency: str
    per_million_tokens: Decimal

    def cost(self, tokens):
        return (Decimal(tokens) * self.per_million_tokens / Decimal(1_000_000)).quantize(QUANTUM, rounding=ROUND_CEILING)


def load_tariff(path, *, model=None):
    """The private tariff file (0600, the service's own), checked; model must be the embedding profile's when given."""
    try:
        value = json.loads(read_private_text(path, maximum=4096))
        if type(value) is not dict or set(value) != {'schema', 'model', 'currency', 'per_million_tokens'} or value['schema'] != SCHEMA:
            raise ValueError()
        price = Decimal(value['per_million_tokens']) if type(value['per_million_tokens']) is str else None
        if (type(value['model']) is not str or not 1 <= len(value['model']) <= 160 or type(value['currency']) is not str
                or len(value['currency']) != 3 or not value['currency'].isupper() or price is None or not price.is_finite()
                or price < 0 or price > 10000 or (model is not None and value['model'] != model)):
            raise ValueError()
        return EmbeddingTariff(value['model'], value['currency'], price)
    except (OSError, ValueError, TypeError, InvalidOperation):
        raise EmbeddingTariffError('embedding tariff rejected') from None


class EmbeddingBudget:
    def __init__(self, pool, session, *, tariff, service, controls=None):
        from nexloop_eios.goal_controls import ControlPlane
        if service not in ('recall-indexer', 'claim-matcher'):
            raise ValueError('embedding service')
        self.tariff, self.service = tariff, service
        self.controls = controls or ControlPlane(pool, session)
        self._configured = None

    def _open(self):
        state = self.controls.budget_open('model')
        self._configured = state.get('configured') is True
        return state

    def allowed(self):
        """Before a tick claims work: False when the tenant's model budget is configured and not open now."""
        state = self._open()
        return not state.get('configured') or state.get('open') is True

    def _reserve(self, amount, tokens, profile_id):
        return self.controls.reserve_budget(budget_kind='model', consumption_id=f'embedding:{self.service}:{uuid.uuid4()}', amount=str(amount),
                                            unit=self.tariff.currency, source_ref=f'embedding:{profile_id}:{tokens}')

    def wrap(self, provider):
        return BudgetedEmbedding(provider, self)


class BudgetedEmbedding:
    """The provider as the indexer and the gluer see it: every call reserved in the model budget first (when configured)."""

    def __init__(self, provider, budget):
        self._provider, self._budget = provider, budget
        self.model, self.dimension = provider.model, provider.dimension

    @property
    def profile_id(self):
        return self._provider.profile_id

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def embed(self, text):
        budget = self._budget
        if budget._configured is None:
            budget._open()
        if not budget._configured:
            return self._provider.embed(text)
        estimate = max(1, len(text) if type(text) is str else 1)
        reserved = budget.tariff.cost(estimate)
        try:
            if reserved > 0:
                budget._reserve(reserved, estimate, self.profile_id)
        except Exception:
            raise EmbeddingBudgetDenied('embedding budget refused') from None
        before = getattr(self._provider, 'usage_tokens', 0) or 0
        vector = self._provider.embed(text)
        used = (getattr(self._provider, 'usage_tokens', 0) or 0) - before
        extra = budget.tariff.cost(used) - reserved
        if extra > 0:
            state = budget._open()
            remaining = Decimal(state.get('limit_amount', '0')) - Decimal(state.get('used', '0'))
            amount = min(extra, remaining)
            if amount > 0:
                try:
                    budget._reserve(amount, used - estimate, self.profile_id)
                except Exception:
                    pass  # the period closed meanwhile: the next tick sees it closed and claims nothing
        return vector
