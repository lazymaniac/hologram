typedef struct { int x; int y; } Point;

enum Axis { HORIZONTAL, VERTICAL };

static int component_sum(Point *point) { return point->x + point->y; }

int point_add(Point a, Point b);
