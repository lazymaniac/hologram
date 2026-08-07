package shop.ids;

import static java.util.Objects.requireNonNull;

public record OrderId(String value) {
    public static OrderId of(String raw) {
        requireNonNull(raw);
        if (raw.isBlank()) {
            throw new IllegalArgumentException("blank id");
        }
        return new OrderId(raw);
    }
}
