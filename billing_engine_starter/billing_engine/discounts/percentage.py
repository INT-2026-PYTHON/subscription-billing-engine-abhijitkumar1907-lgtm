from decimal import Decimal

from billing_engine.money import Money
from billing_engine.discounts.base import Discount, DiscountContext


class PercentageDiscount(Discount):
    def __init__(self, percentage: Decimal) -> None:
        if isinstance(percentage, float):
            raise TypeError("percentage must be Decimal")

        if not isinstance(percentage, Decimal):
            raise TypeError("percentage must be Decimal")

        if percentage < Decimal("0") or percentage > Decimal("1"):
            raise ValueError("percentage must be between 0 and 1")

        self.percentage = percentage

    def apply(self, subtotal: Money, context: DiscountContext) -> Money:
        return subtotal * self.percentage