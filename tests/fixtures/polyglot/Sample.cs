namespace Shop;

public record OrderId(string Value);

public enum Status { New, Paid }

public interface IPricer {
    Quote Evaluate(OrderId id);
}

public class PricingEngine : IPricer {
    private readonly Dictionary<string, long> prices;

    public PricingEngine(Dictionary<string, long> prices) { this.prices = prices; }

    public Quote Evaluate(OrderId id) {
        if (id.Value.Length == 0) throw new UnknownOrderException(id);
        var total = Compute(id);
        return new Quote(total);
    }

    private long Compute(OrderId id) => prices.Count;
}

[ApiController]
[Route("api/orders")]
public class OrdersController {
    public const int MaxItems = 10;

    [HttpGet("{id}")]
    public Quote Find(long id) { return null; }
}
