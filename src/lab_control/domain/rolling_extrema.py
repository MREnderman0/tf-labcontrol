"""Shared inclusive time-window deque operations for scalar extrema."""

WINDOW_SECONDS = 1800.0


def append_extrema(points, minima, maxima, point, value):
    points.append(point)
    while minima and value(minima[-1]) >= value(point):
        minima.pop()
    while maxima and value(maxima[-1]) <= value(point):
        maxima.pop()
    minima.append(point)
    maxima.append(point)


def expire_extrema(points, minima, maxima, cutoff):
    changed = False
    for queue in (points, minima, maxima):
        while queue and queue[0].at < cutoff:
            queue.popleft()
            changed = True
    return changed
