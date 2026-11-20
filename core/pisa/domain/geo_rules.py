"""The location rules several features share, each value in one place. Pure, no I/O.

Two "precise enough" rules, not one: a pace (walking, moving) is measured between fixes seconds apart,
where a fix's error turns into speed, so it takes the tighter value; a position (staying somewhere, being
away from the plan's place) is compared with radii of 75 m and more, so it takes the looser one."""

HOME_M = 150  # a stay, a stop or a place this close to the home is at home
FOOT_MIN_MPS = 0.4  # slower than this at the median, they were standing about, not walking
PACE_ACCURACY_MAX_M = 100  # a fix counts for a pace (on foot, moving) only this precise
PLACE_ACCURACY_MAX_M = 150  # a fix counts for a position (a stay, away from a place) only this precise
