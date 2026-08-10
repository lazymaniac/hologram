#include "types.h"

int gold_c_first(int value) { return value + 1; }
int gold_c_second(int value) { return value * 2; }
int gold_c_ordered(int value) {
    gold_c_first(value);
    gold_c_second(value);
    return gold_c_first(value);
}
