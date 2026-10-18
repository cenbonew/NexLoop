"""Embedding calls in the tenant's model budget (owner 2026-10-18, option B: vector recall and candidate merge on stage).

Background model calls (extraction, matching) have no budget of their own; embeddings of the recall-index service and the
claim matcher are counted in the tenant's `model` budget (NX-022, the same budget Runs reserve): after each tick the tokens the
provider reported are priced by a private tariff (`nexloop-embedding-tariff/1`: model, currency, price per million input tokens;
the owner supplies the price, nothing here invents one) and reserved once (consumption id embedding:<service>:<tick id>). When
the tenant has no model budget, nothing is reserved (as for Runs: no configured budget means no budget gate). When it is
exhausted, a tick makes no embedding call at all; a tick whose cost goes past the limit reserves the rest of the limit, so the
next tick sees the budget exhausted. Amounts are rounded up to 0.000001."""
import json
import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from nexloop_eios.private_configuration import read_private_text

SCHEMA = 'nexloop-embedding-tariff/1'
QUANTUM = Decimal('0.000001')


class EmbeddingTariffError(ValueError):
    pass


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

    def allowed(self):
        """False only when the tenant's model budget is configured and exhausted."""
        status = self.controls.budget_status('model')
        return not (status.get('configured') and status.get('exhausted'))

    def record(self, provider):
        """Reserve the cost of the tokens the provider reported since the last record; returns the reservation (None: nothing)."""
        tokens = getattr(provider, 'usage_tokens', 0) or 0
        provider.usage_tokens = 0
        if tokens <= 0:
            return None
        status = self.controls.budget_status('model')
        if not status.get('configured'):
            return None
        amount = self.tariff.cost(tokens)
        if amount <= 0:
            return None
        remaining = Decimal(status['limit_amount']) - Decimal(status['used'])
        if amount > remaining:
            amount = remaining  # past the limit: the rest of it, so the budget reads exhausted from now on
        if amount <= 0:
            return None
        tick = str(uuid.uuid4())
        return self.controls.reserve_budget(budget_kind='model', consumption_id=f'embedding:{self.service}:{tick}', amount=str(amount),
                                            unit=self.tariff.currency, source_ref=f'embedding:{provider.profile_id}:{tokens}')
