package shop.ids;

import static java.util.Objects.requireNonNull;

public record ItemId(String value) {
    public static ItemId of(String raw) {
        requireNonNull(raw);
        if (raw.isBlank()) {
            throw new IllegalArgumentException("blank id");
        }
        return new ItemId(raw);
    }
}
