package shop;

import java.util.List;
import java.util.Map;

import shop.engine.PricingEngine;
import shop.ids.ItemId;
import shop.ids.OrderId;

public class App {
    public static void main(String[] args) {
        String currency = System.getenv("SHOP_CURRENCY");
        PricingEngine engine = new PricingEngine(Map.of());
        engine.evaluate(OrderId.of("o1"), List.of(ItemId.of("i1")));
    }
}
