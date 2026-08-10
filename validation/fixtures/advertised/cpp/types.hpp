#pragma once

struct GoldCppBase { virtual int goldValue() const = 0; };
struct GoldCppDerived : GoldCppBase { int goldValue() const override { return 1; } };
int gold_cpp_first(int value);
int gold_cpp_second(int value);
