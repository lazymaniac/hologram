package shop.engine;

import shop.ids.OrderId;

public record Quote(OrderId order, long totalCents) {
}
