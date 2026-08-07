package shop.engine;

import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import shop.ids.ItemId;
import shop.ids.OrderId;

class PricingEngineTest {

    @Nested
    @DisplayName("bulk discounts")
    class BulkDiscounts {

        @Test
        @DisplayName("orders over ten items get ten percent off")
        void ordersOverTenItemsGetTenPercentOff() {
        }

        @Test
        @DisplayName("small orders pay full price")
        void smallOrdersPayFullPrice() {
        }
    }

    @Test
    @DisplayName("unknown item is rejected")
    void unknownItemIsRejected() {
    }
}
