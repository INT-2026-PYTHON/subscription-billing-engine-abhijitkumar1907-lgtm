from billing_engine.money import Money
from billing_engine.discounts.base import Discount, DiscountContext


class FirstMonthFree(Discount):
    def apply(self, subtotal: Money, context: DiscountContext) -> Money:
        if context.invoice_count_so_far == 0:
            return subtotal

        return Money.zero(subtotal.currency)