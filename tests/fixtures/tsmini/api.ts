export interface Quote {
  orderId: string;
  totalCents: number;
}

export class PricingClient {
  constructor(private baseUrl: string) {}

  async fetchQuote(orderId: string): Promise<Quote> {
    const res = await fetch(`${this.baseUrl}/quotes/${orderId}`);
    return res.json();
  }
}

export function formatCents(totalCents: number): string {
  return (totalCents / 100).toFixed(2);
}
