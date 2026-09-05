"""Bounding-volume hierarchy over a triangle soup.

Broad phase for the disc sweep: the query is a *capsule* (the segment the
disc centre travels, inflated by the disc radius), because the collider is a
0.618 m ring, not a point.  Traversal runs on plain Python floats rather than
numpy scalars - per-node numpy indexing costs more than the arithmetic it
saves at this node count.
"""

import numpy as np

_LEAF_SIZE = 8


class BVH:
    def __init__(self, verts, tris):
        v = np.asarray(verts, dtype=np.float64)
        t = np.asarray(tris, dtype=np.int64)
        p0, p1, p2 = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        lo = np.minimum(np.minimum(p0, p1), p2)
        hi = np.maximum(np.maximum(p0, p1), p2)
        centroid = (p0 + p1 + p2) / 3.0

        n = len(t)
        self.order = np.arange(n, dtype=np.int64)
        # node arrays, appended during the build
        self._lo = []
        self._hi = []
        self._left = []
        self._right = []
        self._start = []
        self._count = []
        self._build(lo, hi, centroid, 0, n)

        self.node_lo = [tuple(x) for x in self._lo]
        self.node_hi = [tuple(x) for x in self._hi]
        self.left = self._left
        self.right = self._right
        self.start = self._start
        self.count = self._count
        del self._lo, self._hi, self._left, self._right, self._start, self._count

    def _build(self, lo, hi, centroid, start, end):
        idx = self.order[start:end]
        node = len(self._lo)
        self._lo.append(lo[idx].min(axis=0))
        self._hi.append(hi[idx].max(axis=0))
        self._left.append(-1)
        self._right.append(-1)
        self._start.append(start)
        self._count.append(end - start)

        if end - start <= _LEAF_SIZE:
            return node

        c = centroid[idx]
        axis = int(np.argmax(c.max(axis=0) - c.min(axis=0)))
        mid = (start + end) // 2
        # partial sort: only the split position has to be correct
        part = np.argpartition(c[:, axis], mid - start)
        self.order[start:end] = idx[part]
        if self.order[start:mid].size == 0 or self.order[mid:end].size == 0:
            return node

        self._count[node] = 0  # interior nodes hold no triangles
        self._left[node] = self._build(lo, hi, centroid, start, mid)
        self._right[node] = self._build(lo, hi, centroid, mid, end)
        return node

    def query_capsule(self, origin, direction, length, radius):
        """Triangle indices whose AABB may touch the swept capsule.

        Conservative: tests each node's AABB against the segment's AABB grown
        by `radius`.  Loose for long diagonal segments, but the narrow phase
        is exact and cheap, so a few extra candidates cost nothing.
        """
        ox, oy, oz = origin
        ex = ox + direction[0] * length
        ey = oy + direction[1] * length
        ez = oz + direction[2] * length
        qlo = (min(ox, ex) - radius, min(oy, ey) - radius, min(oz, ez) - radius)
        qhi = (max(ox, ex) + radius, max(oy, ey) + radius, max(oz, ez) + radius)

        node_lo = self.node_lo
        node_hi = self.node_hi
        left = self.left
        right = self.right
        start = self.start
        count = self.count

        out = []
        stack = [0]
        while stack:
            n = stack.pop()
            a = node_lo[n]
            b = node_hi[n]
            if (a[0] > qhi[0] or b[0] < qlo[0] or
                    a[1] > qhi[1] or b[1] < qlo[1] or
                    a[2] > qhi[2] or b[2] < qlo[2]):
                continue
            c = count[n]
            if c:
                s = start[n]
                out.append(self.order[s:s + c])
            else:
                stack.append(left[n])
                stack.append(right[n])
        if not out:
            return None
        return np.concatenate(out) if len(out) > 1 else out[0]
