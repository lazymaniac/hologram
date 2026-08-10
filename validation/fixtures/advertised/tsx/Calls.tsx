import { goldTsxFirst, goldTsxSecond } from "./Component";

export function goldTsxOrdered(value: number): number {
  goldTsxFirst(value);
  goldTsxSecond(value);
  return goldTsxFirst(value);
}
