typedef struct { int num; int den; } Rational;

enum Force { ASSERTED, ENTAILED };

static int reduce(Rational *r) { return gcd(r->num, r->den); }

int rational_add(Rational a, Rational b);
