<?php
namespace Shop;

interface Pricer {
    public function quote(OrderId $id): int;
}

class PricingEngine implements Pricer {
    private array $prices;

    public function __construct(array $prices) {
        $this->prices = $prices;
    }

    public function quote(OrderId $id): int {
        if ($id->value === '') {
            throw new UnknownOrderException($id);
        }
        $total = $this->compute($id);
        return $total;
    }

    private function compute(OrderId $id): int {
        return count($this->prices);
    }
}

function demo(array $prices): int {
    $engine = new PricingEngine($prices);
    return $engine->quote(new OrderId('x'));
}
