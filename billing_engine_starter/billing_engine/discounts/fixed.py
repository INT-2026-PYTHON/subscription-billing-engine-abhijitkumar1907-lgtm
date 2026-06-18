from billing_engine.money import Money
from billing_engine.discounts.base import Discount, DiscountContext


class FixedAmountDiscount(Discount):
    def __init__(self, amount: Money) -> None:
        if not isinstance(amount, Money):
            raise TypeError("amount must be Money")

        if amount.is_negative():
            raise ValueError("amount cannot be negative")

        self.amount = amount

    def apply(self, subtotal: Money, context: DiscountContext) -> Money:
        if self.amount.currency != subtotal.currency:
            raise ValueError("currency mismatch")

        return min(self.amount, subtotal)