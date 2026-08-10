export class GoldTypeScriptBase {}
export class GoldTypeScriptDerived extends GoldTypeScriptBase {}
export function goldFirst(value: number): number { return value + 1; }
export function goldSecond(value: number): number { return value * 2; }
function GoldUnusedStrong(value: number): number { return value - 1; }
export function GoldPublicSurface(value: number): number { return value; }
function GoldDynamicCallback(value: number): number { return value + 3; }
function GoldSameFileUsed(value: number): number { return value + 4; }
function GoldStringOnlyStrong(value: number): number { return value + 5; }
const goldNote = "GoldStringOnlyStrong";
function goldExactCloneA(value: number): number {
  let result = value + 1;
  if (result > 10) result = result - 2;
  return Math.max(result, 0);
}
function goldExactCloneB(input: number): number {
  let output = input + 1;
  if (output > 10) output = output - 2;
  return Math.max(output, 0);
}
function goldSimilarNegativeA(value: number): number { return value + 7; }
function goldSimilarNegativeB(value: number): number { return value * 7; }
export function goldUseSameFile(value: number): number {
  return GoldSameFileUsed(value) + goldExactCloneA(value) + goldExactCloneB(value)
    + goldSimilarNegativeA(value) + goldSimilarNegativeB(value);
}
register({ handler: "GoldDynamicCallback" });
