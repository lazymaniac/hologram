package shop.engine;

import java.util.List;
import java.util.Map;

import shop.ids.ItemId;
import shop.ids.OrderId;

/** Rule-tree price evaluator; ordering of rules matters. */
public class PricingEngine implements PricePort {
    private final Map<ItemId, Long> basePrices;

    public PricingEngine(Map<ItemId, Long> basePrices) {
        this.basePrices = basePrices;
    }

    public Quote quoteFor(OrderId order) {
        return evaluate(order, List.of());
    }

    public boolean supports(OrderId order) {
        return true;
    }

    public Quote evaluate(OrderId order, List<ItemId> items) throws UnknownItemException {
        long total = 0;
        for (ItemId item : items) {
            Long price = basePrices.get(item);
            if (price == null) {
                throw new UnknownItemException(item);
            }
            total += price;
        }
        if (items.size() > 10) {
            total = total * 90 / 100;
        }
        return new Quote(order, total);
    }
}
