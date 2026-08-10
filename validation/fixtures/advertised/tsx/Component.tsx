export class GoldTsxBase {}
export class GoldTsxDerived extends GoldTsxBase {}
export function goldTsxFirst(value: number): number { return value + 1; }
export function goldTsxSecond(value: number): number { return value * 2; }
export function GoldTsxComponent() { return <gold-card data-gold="tsx" />; }
