import json
import math
import os
from array import array
from collections import deque

CELL = 2.0


def _pip(px, py, ring):
    inside = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class WorldMap:
    def __init__(self, mapdir):
        w = json.load(open(os.path.join(mapdir, "v2", "world.json")))
        t = w["terrain"]
        self.tnx, self.tny = t["nx"], t["ny"]
        self.tminx, self.tminy, self.tstep = t["minX"], t["minY"], t["step"]
        h = array("h")
        with open(os.path.join(mapdir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            h.frombytes(f.read())
        self.th = h
        b = w["bounds"]
        self.minx, self.miny = b["minX"], b["minY"]
        self.nx = int((b["maxX"] - b["minX"]) / CELL) + 1
        self.ny = int((b["maxY"] - b["minY"]) / CELL) + 1
        n = self.nx * self.ny
        self.ground = array("f", [0.0]) * n
        for j in range(self.ny):
            y = self.miny + (j + 0.5) * CELL
            for i in range(self.nx):
                self.ground[j * self.nx + i] = self.terrain(self.minx + (i + 0.5) * CELL, y)
        self.solids = {}
        for p in w["prisms"]:
            ring = p["ring"]
            holes = p.get("holes") or []
            xs, ys = ring[0::2], ring[1::2]
            i0, i1 = self.ci(min(xs)), self.ci(max(xs))
            j0, j1 = self.cj(min(ys)), self.cj(max(ys))
            for j in range(max(0, j0), min(self.ny - 1, j1) + 1):
                y = self.miny + (j + 0.5) * CELL
                for i in range(max(0, i0), min(self.nx - 1, i1) + 1):
                    x = self.minx + (i + 0.5) * CELL
                    if _pip(x, y, ring) and not any(_pip(x, y, hr) for hr in holes):
                        self.solids.setdefault(j * self.nx + i, []).append((p["base"], p["top"]))
        self.blocked = bytearray(n)
        for c, ivs in self.solids.items():
            g = self.ground[c]
            for base, top in ivs:
                if top > g + 1.3 and base < g + 9.5:
                    self.blocked[c] = 1
                    break
        self.nav = bytearray(self.blocked)
        nx = self.nx
        for c in range(n):
            if self.blocked[c]:
                i, j = c % nx, c // nx
                for dj in (-1, 0, 1):
                    for di in (-1, 0, 1):
                        ii, jj = i + di, j + dj
                        if 0 <= ii < nx and 0 <= jj < self.ny:
                            self.nav[jj * nx + ii] = 1
        self.field = None
        self.field_key = None
        self._job = None

    def ci(self, x):
        return int((x - self.minx) / CELL)

    def cj(self, y):
        return int((y - self.miny) / CELL)

    def cell(self, x, y):
        i, j = self.ci(x), self.cj(y)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def terrain(self, x, y):
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = min(max(int(fx), 0), self.tnx - 2)
        j = min(max(int(fy), 0), self.tny - 2)
        ax, ay = min(max(fx - i, 0.0), 1.0), min(max(fy - j, 0.0), 1.0)
        h = self.th
        k = j * self.tnx + i
        a = h[k] * (1 - ax) + h[k + 1] * ax
        bb = h[k + self.tnx] * (1 - ax) + h[k + self.tnx + 1] * ax
        return (a * (1 - ay) + bb * ay) * 0.01

    def solid_at(self, x, y, z):
        c = self.cell(x, y)
        if c < 0:
            return True
        if z < self.ground[c] - 0.3:
            return True
        ivs = self.solids.get(c)
        if ivs:
            for base, top in ivs:
                if base <= z <= top:
                    return True
        return False

    def los(self, a, b, step=2.0):
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        n = int(d / step)
        if n < 2:
            return True
        for k in range(1, n):
            f = k / n
            if self.solid_at(a[0] + dx * f, a[1] + dy * f, a[2] + dz * f):
                return False
        return True

    def walkable(self, x, y):
        c = self.cell(x, y)
        return c >= 0 and not self.nav[c]

    def start_field(self, key, cx, cy, r):
        if key == self.field_key or (self._job is not None and self._job[0] == key):
            return
        self._job = (key, self._bfs(cx, cy, r))

    def work(self, budget_s, clock):
        if self._job is None:
            return
        end = clock() + budget_s
        key, gen = self._job
        try:
            while clock() < end:
                res = next(gen)
                if res is not None:
                    self.field, self.field_key = res, key
                    self._job = None
                    return
        except StopIteration:
            self._job = None

    def _bfs(self, cx, cy, r):
        nx, ny = self.nx, self.ny
        INF = 65535
        dist = array("H", [INF]) * (nx * ny)
        q = deque()
        r2 = r * r
        i0, i1 = max(0, self.ci(cx - r)), min(nx - 1, self.ci(cx + r))
        j0, j1 = max(0, self.cj(cy - r)), min(ny - 1, self.cj(cy + r))
        for j in range(j0, j1 + 1):
            y = self.miny + (j + 0.5) * CELL - cy
            for i in range(i0, i1 + 1):
                x = self.minx + (i + 0.5) * CELL - cx
                c = j * nx + i
                if x * x + y * y <= r2 and not self.nav[c]:
                    dist[c] = 0
                    q.append(c)
        if not q:
            c = self.cell(cx, cy)
            if c >= 0:
                dist[c] = 0
                q.append(c)
        nav = self.nav
        cnt = 0
        while q:
            c = q.popleft()
            d = dist[c] + 1
            i = c % nx
            for nc, ok in ((c - 1, i > 0), (c + 1, i < nx - 1), (c - nx, c >= nx), (c + nx, c < nx * (ny - 1))):
                if ok and dist[nc] == INF and not nav[nc]:
                    dist[nc] = d
                    q.append(nc)
            cnt += 1
            if cnt % 3000 == 0:
                yield None
        yield dist

    def flow_dir(self, x, y):
        f = self.field
        if f is None:
            return None
        c = self.cell(x, y)
        if c < 0:
            return None
        nx = self.nx
        i, j = c % nx, c // nx
        best, bd = None, f[c]
        if self.nav[c]:
            bd = 65535
        for dj in (-2, -1, 0, 1, 2):
            for di in (-2, -1, 0, 1, 2):
                if di == 0 and dj == 0:
                    continue
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < self.ny:
                    v = f[jj * nx + ii]
                    if v < 65535:
                        score = v + 0.01 * (abs(di) + abs(dj))
                        if score < bd:
                            bd, best = score, (di, dj)
        if best is None:
            return None
        n = math.hypot(best[0], best[1])
        return (best[0] / n, best[1] / n), f[c]
