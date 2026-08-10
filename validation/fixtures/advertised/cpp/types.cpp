#include "types.hpp"

int gold_cpp_first(int value) { return value + 1; }
int gold_cpp_second(int value) { return value * 2; }
int gold_cpp_ordered(int value) {
    gold_cpp_first(value);
    gold_cpp_second(value);
    return gold_cpp_first(value);
}
