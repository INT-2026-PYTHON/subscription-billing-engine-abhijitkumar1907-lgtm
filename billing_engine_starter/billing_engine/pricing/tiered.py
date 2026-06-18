from dataclasses import dataclass
from typing import Optional

from billing_engine.money import Money
from billing_engine.pricing.base import PricingStrategy


@dataclass(frozen=True)
class Tier:
    from_units: int
    to_units: Optional[int]
    unit_price: Money

class TieredPricing(PricingStrategy):
    """Charges across multiple price tiers based on cumulative quantity."""

    def __init__(self, tiers: list[Tier]) -> None:
        if not tiers:
            raise ValueError("tiers cannot be empty")

        currency = tiers[0].unit_price.currency

        for i, tier in enumerate(tiers):
            if tier.unit_price.currency != currency:
                raise ValueError("all tiers must use the same currency")

            if i < len(tiers) - 1:
                if tier.to_units is None:
                    raise ValueError("only the last tier may be open-ended")

                if tiers[i + 1].from_units != tier.to_units:
                    raise ValueError("tiers must be contiguous")

        if tiers[-1].to_units is not None:
            raise ValueError("top tier must be open-ended")

        self.tiers = tiers

    def calculate(self, quantity: int) -> Money:
        if quantity < 0:
            raise ValueError("quantity cannot be negative")

        currency = self.tiers[0].unit_price.currency
        total = Money.zero(currency)

        for tier in self.tiers:
            if tier.to_units is None:
                units = max(0, quantity - tier.from_units)
            else:
                if quantity > tier.from_units:
                    units = min(quantity, tier.to_units) - tier.from_units
                else:
                    units = 0

            total += tier.unit_price * units

        return total