package shop.ids;

import static java.util.Objects.requireNonNull;

public record UserId(String value) {
    public static UserId of(String raw) {
        requireNonNull(raw);
        if (raw.isBlank()) {
            throw new IllegalArgumentException("blank id");
        }
        return new UserId(raw);
    }
}
