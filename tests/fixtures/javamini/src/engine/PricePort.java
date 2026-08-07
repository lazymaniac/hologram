package shop.engine;

import shop.ids.OrderId;

public interface PricePort {
    Quote quoteFor(OrderId order);

    boolean supports(OrderId order);
}
