package shop.engine;

import shop.ids.ItemId;

public class UnknownItemException extends RuntimeException {
    public UnknownItemException(ItemId item) {
        super("unknown item " + item.value());
    }
}
