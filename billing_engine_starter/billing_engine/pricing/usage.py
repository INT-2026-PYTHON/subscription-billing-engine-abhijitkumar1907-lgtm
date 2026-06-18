from billing_engine.money import Money
from billing_engine.pricing.base import PricingStrategy


class UsageBased(PricingStrategy):
    """Charges `unit_price * quantity`."""

    def __init__(self, unit_price: Money) -> None:
        if not isinstance(unit_price, Money):
            raise TypeError("unit_price must be a Money instance")

        if unit_price.is_negative():
            raise ValueError("unit_price cannot be negative")

        self.unit_price = unit_price

    def calculate(self, quantity: int) -> Money:
        if quantity < 0:
            raise ValueError("quantity cannot be negative")

        return self.unit_price * quantity