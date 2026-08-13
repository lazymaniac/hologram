module Shop
  class PricingEngine
    attr_reader :prices

    def initialize(prices)
      @prices = prices
    end

    def quote(id)
      compute(id)
    end

    private

    def compute(id)
      @prices.size
    end
  end
end

def normalize(items)
  items
end
