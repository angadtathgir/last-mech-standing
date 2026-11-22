import math
import random
import time
import mechfight as mf

try:
    from worldmap import WorldMap
except Exception:
    WorldMap = None

WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}
UPGRADE_RANK = ["armor", "plating", "nanites", "lightning_efficiency", "laser_charge", "laser_damage",
                "gun_rate", "gun_damage", "lightning_range", "reactor", "capacitor", "gyros", "servos",
                "gun_mag", "lightning_chain", "gun_velocity", "jets", "sensors", "laser_pierce",
                "mortar_reload", "mortar_splash", "mortar_velocity"]


LASER_RANK = ["laser_damage", "laser_charge", "sensors", "armor", "plating", "nanites", "gyros", "servos",
              "gun_rate", "gun_damage", "reactor", "capacitor", "gun_velocity", "gun_mag", "jets", "laser_pierce",
              "lightning_efficiency", "lightning_range", "lightning_chain", "mortar_reload", "mortar_splash", "mortar_velocity"]


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(a):
    return [math.cos(a), math.sin(a)]


class Bot:
    name = "last-mech-standing"

    def __init__(self, hello):
        c = hello["config"]
        self.cfg = c
        random.seed(hello.get("seed", 0))
        self.reach = c["loot"]["pickup_radius"]
        self.regen_delay = c["mech"].get("regen_delay", 5)
        self.map = None
        if WorldMap is not None:
            try:
                self.map = WorldMap(hello["map"]["dir"])
            except Exception as e:
                mf.log("map load failed", repr(e))
        self.hist = []
        self.unstick = 0
        self.unstick_dir = 1
        self.strafe = 1
        self.strafe_t = 0.0
        self.threat_bearing = None
        self.threat_t = -99.0
        self.last_hit_by = None
        self.target_id = None
        self.retreating = False
        self.last_combat_t = -99.0
        self.scan_dir = 1
        self.dodge_until = -1.0
        self.dodge_dir = 1
        self.last_t = 0.0
        self.known = {}

    def cur_zone(self, obs):
        z = obs["zone"]
        t = obs["t"]
        c, r = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        if t >= z["shrink_start"]:
            span = max(1e-6, z["shrink_end"] - z["shrink_start"])
            f = min(1.0, (t - z["shrink_start"]) / span)
            c = [c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f]
            r = r + (nr - r) * f
        return c, r, nc, nr

    def in_safe(self, p, obs, slack=0.0):
        c, r, nc, nr = self.cur_zone(obs)
        return d2(p, c) < r - 6 + slack

    def steer(self, pos, ang):
        m = self.map
        if m is None:
            return unit(ang)
        for off in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.2, -2.2):
            a = ang + off
            ok = True
            for dist in (3.0, 6.0):
                if not m.walkable(pos[0] + math.cos(a) * dist, pos[1] + math.sin(a) * dist):
                    ok = False
                    break
            if ok:
                return unit(a)
        return unit(ang)

    def go_to(self, pos, goal):
        ang = math.atan2(goal[1] - pos[1], goal[0] - pos[0])
        m = self.map
        if m is not None:
            g = m.terrain(pos[0], pos[1])
            on_ground = pos[2] < g + 2.0
            if on_ground and not self.clear_path(pos, goal):
                fd = m.flow_dir(pos[0], pos[1]) if self.goal_is_zone else None
                if fd is not None:
                    return [fd[0][0], fd[0][1]]
            if on_ground:
                return self.steer(pos, ang)
        return unit(ang)

    def clear_path(self, a, b):
        m = self.map
        d = d2(a, b)
        n = int(d / 3.0)
        for k in range(1, n + 1):
            f = k / max(n, 1)
            if not m.walkable(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f):
                return False
        return True

    def eye(self, me):
        st = me["stats"]
        return [me["pos"][0], me["pos"][1], me["pos"][2] + st.get("muzzle_height", 7)]

    def center(self, m):
        s = 1 + 0.05 * m.get("cubes", 0) if m["kind"] == "mech" else 1
        h = 4.5 * s if m["kind"] == "mech" else 1.75
        return [m["pos"][0], m["pos"][1], m["pos"][2] + h]

    def has_los(self, a, b):
        if self.map is None:
            return True
        return self.map.los(a, b)

    def lead(self, me, tgt, speed):
        e = self.eye(me)
        tc = self.center(tgt)
        vx, vy, vz = tgt.get("vel", [0, 0, 0])
        if tgt.get("grounded", True):
            vz = 0
        t = 0.0
        for _ in range(4):
            p = (tc[0] + vx * t, tc[1] + vy * t, tc[2] + vz * t * 0.5)
            t = math.dist(p, e) / speed
        p = (tc[0] + vx * t, tc[1] + vy * t, tc[2] + vz * t * 0.5)
        h = math.hypot(p[0] - e[0], p[1] - e[1])
        return [math.atan2(p[1] - e[1], p[0] - e[0]), math.atan2(p[2] - e[2], max(h, 0.1))], p

    def mortar_aim(self, me, tgt, v, g):
        e = self.eye(me)
        mx, my, mz = e[0], e[1], e[2] + 2.5
        tx, ty, tz = tgt["pos"]
        vx, vy, _ = tgt.get("vel", [0, 0, 0])
        h = math.hypot(tx - mx, ty - my)
        tof = h / (v * 0.7)
        tx, ty = tx + vx * tof, ty + vy * tof
        x = math.hypot(tx - mx, ty - my)
        y = tz - mz
        disc = v ** 4 - g * (g * x * x + 2 * y * v * v)
        if disc < 0 or x < 1:
            return None
        pitch = math.atan((v * v - math.sqrt(disc)) / (g * x))
        return [math.atan2(ty - my, tx - mx), pitch]

    def cover_point(self, me, threats, obs):
        m = self.map
        pos = me["pos"]
        if m is None or not threats:
            return None
        best = None
        for r in (8.0, 16.0, 26.0):
            for k in range(16):
                a = k * math.pi / 8
                p = [pos[0] + math.cos(a) * r, pos[1] + math.sin(a) * r]
                if not m.walkable(p[0], p[1]) or not self.in_safe(p, obs, -4):
                    continue
                if not self.clear_path(pos, p):
                    continue
                g = m.terrain(p[0], p[1])
                me_c = [p[0], p[1], g + 4.5]
                exposed = 0
                for t in threats:
                    te = [t["pos"][0], t["pos"][1], t["pos"][2] + 8]
                    if m.los(te, me_c) or m.los(te, [p[0], p[1], g + 8.5]):
                        exposed += 1
                closest = min(d2(t["pos"], p) for t in threats)
                score = exposed * 100 + r - min(closest, 120) * 0.3
                if best is None or score < best[0]:
                    best = (score, p, exposed)
            if best is not None and best[2] == 0:
                break
        return best

    def act(self, obs):
        t0 = time.perf_counter()
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        mh = me["stats"].get("muzzle_height", 7)
        act = {"tick": obs["tick"]}
        self.goal_is_zone = False

        if obs.get("offer"):
            ch = obs["offer"]["choices"]
            rank = LASER_RANK if "laser" in [w["kind"] for w in me["weapons"] if w] else UPGRADE_RANK
            act["pick"] = min(range(len(ch)), key=lambda i: rank.index(ch[i]["id"]) if ch[i]["id"] in rank else 99)

        events = obs.get("events", [])
        for ev in events:
            if ev["type"] == "damaged":
                if ev.get("weapon") not in ("zone",):
                    self.last_combat_t = t
                if ev.get("bearing") is not None:
                    self.threat_bearing = ev["bearing"]
                    self.threat_t = t
                if ev.get("by") is not None:
                    self.last_hit_by = ev["by"]
            elif ev["type"] == "hit":
                self.last_combat_t = t
            elif ev["type"] == "kill":
                self.rest = True

        c, r, nc, nr = self.cur_zone(obs)
        if self.map is not None:
            key = (round(nc[0], 1), round(nc[1], 1), round(nr, 1))
            self.map.start_field(key, nc[0], nc[1], max(4.0, nr * 0.55))

        w0, w1 = me["weapons"][0], me["weapons"][1]
        kinds = [w["kind"] if w else None for w in me["weapons"]]
        hp_frac = me["hp"] / max(1, me["max_hp"])

        take, best_pick = None, None
        vis_ids = set()
        for p in obs.get("pickups", []):
            self.known[p["id"]] = p
            vis_ids.add(p["id"])
        eye0 = self.eye(me)
        for pid in list(self.known):
            if pid in vis_ids:
                continue
            kp = self.known[pid]["pos"]
            dk = d2(kp, pos)
            if dk < 12 or (dk < 150 and abs(wrap(math.atan2(kp[1] - pos[1], kp[0] - pos[0]) - me["aim"][0])) < 0.8 and self.has_los(eye0, [kp[0], kp[1], kp[2] + 1])):
                del self.known[pid]
        unarmed = w1 is None
        for p in self.known.values():
            if p["kind"] in kinds or p["kind"] == "gun":
                continue
            if abs(p["pos"][2] - pos[2]) > 5:
                continue
            cur = kinds[1]
            gain = WEAPON_RANK[p["kind"]] - (WEAPON_RANK[cur] if cur else -1)
            if gain <= 0:
                continue
            dd = d2(p["pos"], pos)
            if dd < self.reach and p["id"] in vis_ids:
                take = p
            if dd < (250 if unarmed else 110) and self.in_safe(p["pos"], obs, -10):
                s = gain * 40 - dd
                if best_pick is None or s > best_pick[0]:
                    best_pick = (s, p)
        if take is not None:
            if w1 is None:
                act["take"] = take["id"]
            elif not (w1["kind"] == "laser" and w1.get("charge", 0) > 0):
                act["take"] = take["id"]
                act["drop"] = 1

        eye = self.eye(me)
        enemies = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m["kind"] == "npc"]
        for m in enemies:
            m["_d"] = math.dist(m["pos"], pos)
            m["_los"] = self.has_los(eye, self.center(m))

        def tscore(m):
            s = m["_d"] + m.get("hp", 1400) * 0.06
            if not m["_los"]:
                s += 120
            if m["id"] == self.last_hit_by:
                s -= 30
            if m["id"] == self.target_id:
                s -= 30
            return s

        target = min(enemies, key=tscore) if enemies else None
        fighting_npc = False
        if target is None and npcs:
            hit_by_npc = any(ev["type"] == "damaged" and ev.get("weapon") == "npc" for ev in events)
            cand = [n for n in npcs if math.dist(n["pos"], pos) < 70 and self.has_los(eye, self.center(n))]
            if cand and (hit_by_npc or (hp_frac > 0.9 and t < 260)):
                target = min(cand, key=lambda n: math.dist(n["pos"], pos))
                fighting_npc = True
        self.target_id = target["id"] if target else None

        self.hist.append((t, pos[0], pos[1]))
        while self.hist and t - self.hist[0][0] > 1.2:
            self.hist.pop(0)

        near_threats = [m for m in enemies if m["_d"] < 160]
        if enemies:
            self.retreating = True
        if hp_frac < 0.5 and near_threats:
            self.retreating = True
        if getattr(self, "rest", False):
            if hp_frac < 0.85 and not any(m["_d"] < 60 and m["_los"] for m in enemies):
                self.retreating = True
            self.rest = False
        if self.retreating and (hp_frac > 0.8 or not enemies and t - self.last_combat_t > 12):
            self.retreating = False
        if self.retreating and target is not None and target.get("hp", 9999) < 140 and target["_d"] < 60:
            self.retreating = False

        zone_urgent = not self.in_safe(pos, obs)
        nxt_out = d2(pos, nc) > nr - 8
        zone_soon = nxt_out and t > obs["zone"]["shrink_start"] - 25

        goal = None
        boost = False
        if zone_urgent or zone_soon:
            self.goal_is_zone = True
            k = max(0.0, nr * 0.5) / max(d2(pos, nc), 1e-6)
            goal = [nc[0] + (pos[0] - nc[0]) * k, nc[1] + (pos[1] - nc[1]) * k]
            boost = zone_urgent or t > obs["zone"]["shrink_start"]

        crate_goal = None
        if t < 240 and hp_frac > 0.9:
            gc = [cr for cr in obs.get("crates", []) if abs(cr["pos"][2] - pos[2]) < 5 and 45 < d2(cr["pos"], pos) < 160 and self.in_safe(cr["pos"], obs, -10)]
            if gc:
                crate_goal = min(gc, key=lambda cr: d2(cr["pos"], pos))["pos"]

        move = [0.0, 0.0]
        mode = "idle"
        if self.retreating:
            mode = "retreat"
            threats = near_threats or enemies
            cp = self.cover_point(me, threats, obs)
            if cp is not None:
                move = self.go_to(pos, cp[1]) if d2(cp[1], pos) > 2 else [0.0, 0.0]
            elif threats:
                tc = min(threats, key=lambda m: m["_d"])
                away = math.atan2(pos[1] - tc["pos"][1], pos[0] - tc["pos"][0])
                move = self.steer(pos, away)
            elif self.threat_bearing is not None and t - self.threat_t < 6:
                move = self.steer(pos, wrap(self.threat_bearing + math.pi))
            if goal is not None and zone_urgent:
                move = self.go_to(pos, goal)
            boost = me["energy"] > 25 and bool(threats or t - self.threat_t < 4)
        elif target is not None and not fighting_npc:
            mode = "fight"
            tp = target["pos"]
            dist = d2(tp, pos)
            to = math.atan2(tp[1] - pos[1], tp[0] - pos[0])
            if t - self.strafe_t > 0.7 + random.random() * 0.9:
                self.strafe = -self.strafe
                self.strafe_t = t
            if "laser" in kinds:
                ideal = 185 if "sensors" in me.get("upgrades", []) else 175
            elif "lightning" in kinds:
                ideal = 35
            else:
                ideal = 80
            radial = 0.0
            if not target["_los"]:
                radial = 1.0
            elif dist > ideal + 15:
                radial = 1.0
            elif dist < ideal - 15:
                radial = -0.8
            sa = to + math.pi / 2 * self.strafe
            mx = math.cos(to) * radial + math.cos(sa) * 0.9
            my = math.sin(to) * radial + math.sin(sa) * 0.9
            if goal is not None:
                gy = math.atan2(goal[1] - pos[1], goal[0] - pos[0])
                mx = mx * 0.35 + math.cos(gy)
                my = my * 0.35 + math.sin(gy)
            ang = math.atan2(my, mx)
            if radial > 0 and not target["_los"] and goal is None:
                move = self.go_to(pos, tp)
            else:
                move = self.steer(pos, ang) if self.map else unit(ang)
            boost = boost or (dist < 60 and "lightning" in kinds and dist > ideal and me["energy"] > 40)
        else:
            if goal is None:
                cubes = [cb for cb in obs.get("cubes", []) if abs(cb["pos"][2] - pos[2]) < 5 and d2(cb["pos"], pos) < 110]
                if unarmed and best_pick is not None:
                    goal = best_pick[1]["pos"]
                    mode = "loot"
                elif cubes:
                    goal = min(cubes, key=lambda cb: d2(cb["pos"], pos))["pos"]
                    mode = "cube"
                elif best_pick is not None:
                    goal = best_pick[1]["pos"]
                    mode = "loot"
                elif crate_goal is not None:
                    goal = crate_goal
                    mode = "crate"
                elif fighting_npc:
                    tp = target["pos"]
                    if d2(tp, pos) > 40:
                        goal = tp
                    mode = "npc"
                elif d2(pos, nc) > nr * 0.6:
                    self.goal_is_zone = True
                    goal = nc
                    mode = "drift"
            if goal is not None:
                move = self.go_to(pos, goal) if d2(goal, pos) > 1.2 else [0.0, 0.0]

        for m in enemies:
            if m.get("charging") and m["_los"] and m["_d"] < 290:
                b = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
                if abs(wrap(m["aim"][0] - b)) < 0.12:
                    if t > self.dodge_until:
                        self.dodge_dir = random.choice([-1, 1])
                    self.dodge_until = t + 0.3
                    break
        for p in obs.get("projectiles", []):
            if p["kind"] != "mortar":
                continue
            v = p["vel"]
            g = self.cfg["weapons"]["mortar"].get("gravity", 22)
            px, py, pz = p["pos"]
            gz = pos[2]
            a, b_, cc = -0.5 * g, v[2], pz - gz
            disc = b_ * b_ - 4 * a * cc
            if disc < 0:
                continue
            tl = (-b_ - math.sqrt(disc)) / (2 * a)
            if tl <= 0 or tl > 4:
                continue
            land = [px + v[0] * tl, py + v[1] * tl]
            if d2(land, pos) < 12:
                away = math.atan2(pos[1] - land[1], pos[0] - land[0])
                move = self.steer(pos, away) if self.map else unit(away)
                boost = True
                break
        if t < self.dodge_until and target is not None:
            b = math.atan2(target["pos"][1] - pos[1], target["pos"][0] - pos[0]) + math.pi / 2 * self.dodge_dir
            move = self.steer(pos, b) if self.map else unit(b)
            boost = me["energy"] > 10

        if hp_frac < 0.5 and not fighting_npc:
            rx, ry = 0.0, 0.0
            for n in npcs:
                dn = d2(n["pos"], pos)
                if dn < 60:
                    w = (60 - dn) / 60
                    rx += (pos[0] - n["pos"][0]) / max(dn, 1) * w * 2
                    ry += (pos[1] - n["pos"][1]) / max(dn, 1) * w * 2
            if rx or ry:
                mx, my = move[0] + rx, move[1] + ry
                move = self.steer(pos, math.atan2(my, mx)) if self.map else unit(math.atan2(my, mx))

        jump = False
        moving = math.hypot(*move) > 0.5
        if len(self.hist) > 8 and moving:
            moved = math.hypot(self.hist[-1][1] - self.hist[0][1], self.hist[-1][2] - self.hist[0][2])
            if moved < 2.5 and self.unstick <= 0:
                self.unstick = 14
                self.unstick_dir = random.choice([-1, 1])
        if self.unstick > 0:
            self.unstick -= 1
            if self.unstick > 7:
                jump = me["grounded"]
                boost = True
            else:
                a = math.atan2(move[1], move[0]) + self.unstick_dir * math.pi / 2
                move = unit(a)
                boost = me["energy"] > 20 and not me["grounded"]
        if me["energy"] < 12 and not zone_urgent:
            boost = False

        fire = [False, False]
        aim = None
        hold_fire = True and t - self.threat_t > 1.5
        if target is not None:
            gspeed = w0["stats"]["speed"] if w0 and w0["kind"] == "gun" else 252
            gun_aim, _ = self.lead(me, target, gspeed)
            aim = gun_aim
            dist3 = math.dist(target["pos"], pos)
            los = target.get("_los", True) if not fighting_npc else True
            ang_tol = max(0.015, math.atan2(2.6, max(dist3, 1)))
            yaw_err = abs(wrap(me["aim"][0] - gun_aim[0]))
            pitch_err = abs(me["aim"][1] - gun_aim[1])
            on = yaw_err < ang_tol * 1.4 and pitch_err < ang_tol * 2
            if w0 and w0["kind"] == "gun" and los and not hold_fire:
                if dist3 < (130 if "laser" in kinds else w0["stats"]["range"] * 0.98) and on and w0.get("ammo", 1) > 0:
                    fire[0] = True
            if w1 is not None and not hold_fire:
                k = w1["kind"]
                ws = w1["stats"]
                if k == "laser":
                    tc = self.center(target)
                    la = [math.atan2(tc[1] - eye[1], tc[0] - eye[0]), math.atan2(tc[2] - eye[2], max(d2(tc, eye), 0.1))]
                    charge = w1.get("charge", 0)
                    if charge > 0 or w1.get("cooldown", 0) <= 0.05:
                        aim = la
                    on_l = abs(wrap(me["aim"][0] - la[0])) < ang_tol and abs(me["aim"][1] - la[1]) < ang_tol * 1.5
                    if dist3 < ws.get("range", 280) and (los or charge > 0):
                        if charge >= ws["charge"] * 0.98 and on_l and los:
                            fire[1] = False
                        elif w1.get("cooldown", 0) <= 0.05 or charge > 0:
                            fire[1] = True
                    if charge > 0:
                        fire[0] = False
                elif k == "lightning":
                    if dist3 < ws.get("range", 45) * 0.95 and los and abs(wrap(me["aim"][0] - gun_aim[0])) < math.radians(ws.get("cone_deg", 30)) * 0.8:
                        if me["energy"] > ws.get("energy", 3) + 3:
                            fire[1] = True
                elif k == "mortar":
                    if 25 < dist3 < 220 and w1.get("cooldown", 0) <= 0 and not fighting_npc:
                        ma = self.mortar_aim(me, target, ws["speed"], ws["gravity"])
                        if ma is not None:
                            aim = ma
                            if abs(wrap(me["aim"][0] - ma[0])) < 0.03 and abs(me["aim"][1] - ma[1]) < 0.03:
                                fire[1] = True
                                fire[0] = False
        elif self.threat_bearing is not None and t - self.threat_t < 3.0:
            aim = [self.threat_bearing, 0.0]
        else:
            loud = [s for s in obs.get("sounds", []) if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "boost", "step", "jet") and s["dist"] in ("near", "mid")]
            crates = [cr for cr in obs.get("crates", []) if math.dist(cr["pos"], pos) < 120]
            if loud:
                aim = [loud[0]["bearing"], 0.0]
                self.threat_bearing, self.threat_t = loud[0]["bearing"], t - 1.5
            elif False and crates and w0 and w0["kind"] == "gun" and w0.get("ammo", 0) > 0 and not self.retreating and hp_frac > 0.9:
                cr = min(crates, key=lambda cr: math.dist(cr["pos"], pos))
                cc = [cr["pos"][0], cr["pos"][1], cr["pos"][2] + 1.2]
                if self.has_los(eye, cc):
                    h = d2(cc, eye)
                    aim = [math.atan2(cc[1] - eye[1], cc[0] - eye[0]), math.atan2(cc[2] - eye[2], max(h, 0.1))]
                    if abs(wrap(me["aim"][0] - aim[0])) < 0.04 and abs(me["aim"][1] - aim[1]) < 0.06:
                        fire[0] = True
            if aim is None and unarmed:
                aim = [wrap(me["aim"][0] + 1.2), 0.0]
            if aim is None:
                if moving:
                    base = math.atan2(move[1], move[0])
                    aim = [wrap(base + 0.9 * math.sin(t * 1.3)), 0.0]
                else:
                    aim = [wrap(me["aim"][0] + 0.5), 0.0]

        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) <= 4 and w0.get("reload", 0) == 0 and (target is None or hold_fire):
            act["reload"] = True

        act.update(move=move, boost=bool(boost), jump=jump, aim=aim, fire=fire)

        if self.map is not None:
            spent = time.perf_counter() - t0
            self.map.work(max(0.0, 0.010 - spent), time.perf_counter)
        return act


if __name__ == "__main__":
    mf.run(Bot)
