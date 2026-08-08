export type UserId = string;
export type Money = { cents: number; currency: string };

export const api = {
  get(path: string): string { return fetchIt(path); },
  post: (path: string, body: string): string => path + body,
};

const fetchIt = (p: string): string => p;

export { OrderId, Quote as PriceQuote } from "./api";
