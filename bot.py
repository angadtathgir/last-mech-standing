import math
import random
import mechfight as mf

WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}
UPGRADE_RANK = ["plating", "armor", "nanites", "laser_charge", "laser_damage", "gun_rate", "gun_damage",
                "lightning_efficiency", "lightning_range", "gyros", "servos", "reactor", "capacitor",
                "gun_mag", "gun_velocity", "jets", "sensors", "mortar_reload", "laser_pierce",
                "lightning_chain", "mortar_splash", "mortar_velocity"]


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Bot:
    name = "last-mech-standing"

    def __init__(self, hello):
        c = hello["config"]
        self.cfg = c
        self.slot = hello["slot"]
        random.seed(hello.get("seed", 0))
        self.reach = c["loot"]["pickup_radius"]
        self.cube_reach = c["cubes"]["pickup_radius"]
        self.hist = []
        self.unstick = 0
        self.unstick_dir = 1
        self.strafe = 1
        self.strafe_t = 0.0
        self.threat_bearing = None
        self.threat_t = -99.0
        self.last_hit_by = None
        self.target_id = None

    def zone_goal(self, obs):
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

    def safe_point(self, obs, pos):
        c, r, nc, nr = self.zone_goal(obs)
        margin = max(8.0, nr * 0.35)
        dist = d2(pos, nc)
        if dist <= nr - margin:
            return None, nc, nr
        k = max(0.0, (nr - margin)) / max(dist, 1e-6)
        return [nc[0] + (pos[0] - nc[0]) * k, nc[1] + (pos[1] - nc[1]) * k], nc, nr

    def lead(self, me, tgt, speed, mh):
        mx, my, mz = me["pos"][0], me["pos"][1], me["pos"][2] + mh
        tx, ty, tz = tgt["pos"]
        tz += 4.5 if tgt["kind"] == "mech" else 1.75
        vx, vy, vz = tgt.get("vel", [0, 0, 0])
        t = 0.0
        for _ in range(4):
            px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t * 0.3
            t = math.dist((px, py, pz), (mx, my, mz)) / speed
        px, py, pz = tx + vx * t, ty + vy * t, tz + vz * t * 0.3
        h = math.hypot(px - mx, py - my)
        return [math.atan2(py - my, px - mx), math.atan2(pz - mz, max(h, 0.1))]

    def mortar_aim(self, me, tgt, v, g, mh):
        mx, my, mz = me["pos"][0], me["pos"][1], me["pos"][2] + mh + 0.5
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

    def act(self, obs):
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        st = me["stats"]
        mh = st.get("muzzle_height", 7)
        act = {"tick": obs["tick"]}

        if obs.get("offer"):
            ch = obs["offer"]["choices"]
            best = min(range(len(ch)), key=lambda i: UPGRADE_RANK.index(ch[i]["id"]) if ch[i]["id"] in UPGRADE_RANK else 99)
            act["pick"] = best

        for ev in obs.get("events", []):
            if ev["type"] == "damaged" and ev.get("bearing") is not None:
                self.threat_bearing = ev["bearing"]
                self.threat_t = t
                if ev.get("by") is not None:
                    self.last_hit_by = ev["by"]

        w0, w1 = me["weapons"][0], me["weapons"][1]
        kinds = [w["kind"] if w else None for w in me["weapons"]]

        take = None
        best_pick = None
        for p in obs.get("pickups", []):
            if p["kind"] in kinds or p["kind"] == "gun":
                continue
            if abs(p["pos"][2] - pos[2]) > 6:
                continue
            cur = kinds[1]
            gain = WEAPON_RANK[p["kind"]] - (WEAPON_RANK[cur] if cur else -1)
            if gain <= 0:
                continue
            dd = d2(p["pos"], pos)
            if dd < self.reach:
                take = p
            score = gain * 40 - dd
            if dd < 90 and (best_pick is None or score > best_pick[0]):
                best_pick = (score, p)
        if take is not None:
            if w1 is None:
                act["take"] = take["id"]
            elif not (w1["kind"] == "laser" and w1.get("charge", 0) > 0):
                act["take"] = take["id"]
                act["drop"] = 1

        enemies = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m["kind"] == "npc"]
        hp_frac = me["hp"] / max(1, me["max_hp"])

        def tscore(m):
            dd = math.dist(m["pos"], pos)
            s = dd + m.get("hp", 1400) * 0.05
            if m["id"] == self.last_hit_by:
                s -= 40
            if m["id"] == self.target_id:
                s -= 25
            return s

        target = min(enemies, key=tscore) if enemies else None
        if target is None:
            angry = [n for n in npcs if math.dist(n["pos"], pos) < 60]
            if angry and any(ev["type"] == "damaged" and ev.get("weapon") == "npc" for ev in obs.get("events", [])):
                target = min(angry, key=lambda n: math.dist(n["pos"], pos))
        self.target_id = target["id"] if target else None

        self.hist.append((t, pos[0], pos[1]))
        while self.hist and t - self.hist[0][0] > 1.2:
            self.hist.pop(0)

        safe, nc, nr = self.safe_point(obs, pos)
        goal = None
        boost = False
        if not me["in_zone"]:
            goal = safe or nc
            boost = True
        elif safe is not None and (t > obs["zone"]["shrink_start"] - 20 or d2(pos, nc) > nr + 40):
            goal = safe
            boost = obs["zone"]["dps"] >= 60 or t > obs["zone"]["shrink_start"]

        if goal is None and target is None:
            cubes = [c for c in obs.get("cubes", []) if abs(c["pos"][2] - pos[2]) < 6 and d2(c["pos"], pos) < 80]
            if cubes:
                goal = min(cubes, key=lambda c: d2(c["pos"], pos))["pos"]
            elif best_pick is not None:
                goal = best_pick[1]["pos"]

        move = [0.0, 0.0]
        if target is not None:
            tp = target["pos"]
            dist = d2(tp, pos)
            to = math.atan2(tp[1] - pos[1], tp[0] - pos[0])
            if t - self.strafe_t > 0.8 + random.random() * 0.8:
                self.strafe = -self.strafe
                self.strafe_t = t
            ideal = 30 if "lightning" in kinds else 80
            if hp_frac < 0.3:
                ideal = 180
            radial = 0.0
            if dist > ideal + 20:
                radial = 1.0
            elif dist < ideal - 15:
                radial = -1.0
            sx, sy = math.cos(to + math.pi / 2) * self.strafe, math.sin(to + math.pi / 2) * self.strafe
            mx = math.cos(to) * radial + sx * 0.8
            my = math.sin(to) * radial + sy * 0.8
            if goal is not None:
                gy = math.atan2(goal[1] - pos[1], goal[0] - pos[0])
                mx = mx * 0.4 + math.cos(gy)
                my = my * 0.4 + math.sin(gy)
            n = math.hypot(mx, my) or 1
            move = [mx / n, my / n]
            boost = boost or (hp_frac < 0.3 and dist < 120)
        elif goal is not None:
            gy = math.atan2(goal[1] - pos[1], goal[0] - pos[0])
            move = [math.cos(gy), math.sin(gy)] if d2(goal, pos) > 1.5 else [0.0, 0.0]

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
                move = [math.cos(a), math.sin(a)]
                boost = me["energy"] > 20 and not me["grounded"]
        if me["energy"] < 15 and me["in_zone"]:
            boost = False

        fire = [False, False]
        aim = None
        if target is not None:
            gun_aim = self.lead(me, target, w0["stats"]["speed"] if w0 and w0["kind"] == "gun" else 252, mh)
            aim = gun_aim
            dist3 = math.dist(target["pos"], pos)
            yaw_err = abs(wrap(me["aim"][0] - gun_aim[0]))
            pitch_err = abs(me["aim"][1] - gun_aim[1])
            ang = max(0.02, math.atan2(3.0, max(dist3, 1)))
            on = yaw_err < ang * 1.5 and pitch_err < ang * 2
            if w0 and w0["kind"] == "gun":
                rng = w0["stats"]["range"]
                if dist3 < rng and on:
                    fire[0] = True
                if w0.get("ammo", 1) == 0:
                    fire[0] = False
            if w1 is not None:
                k = w1["kind"]
                ws = w1["stats"]
                if k == "laser":
                    hs = [target["pos"][0], target["pos"][1], target["pos"][2]]
                    la = [math.atan2(hs[1] - pos[1], hs[0] - pos[0]),
                          math.atan2(hs[2] + 4.5 - pos[2] - mh, max(d2(hs, pos), 0.1))]
                    aim = la
                    yaw_err = abs(wrap(me["aim"][0] - la[0]))
                    on_l = yaw_err < ang and abs(me["aim"][1] - la[1]) < ang * 1.5
                    charge = w1.get("charge", 0)
                    if dist3 < ws.get("range", 280):
                        if charge >= ws["charge"] * 0.98 and on_l:
                            fire[1] = False
                        elif w1.get("cooldown", 0) <= 0.05 or charge > 0:
                            fire[1] = True
                    fire[0] = fire[0] and charge == 0
                elif k == "lightning":
                    if dist3 < ws.get("range", 45) * 0.95 and yaw_err < math.radians(ws.get("cone_deg", 30)) * 0.8:
                        if me["energy"] > ws.get("energy", 3) + 4:
                            fire[1] = True
                elif k == "mortar":
                    if 25 < dist3 < 220 and w1.get("cooldown", 0) <= 0:
                        ma = self.mortar_aim(me, target, ws["speed"], ws["gravity"], mh)
                        if ma is not None:
                            aim = ma
                            if abs(wrap(me["aim"][0] - ma[0])) < 0.03 and abs(me["aim"][1] - ma[1]) < 0.03:
                                fire[1] = True
                                fire[0] = False
        elif self.threat_bearing is not None and t - self.threat_t < 3.0:
            aim = [self.threat_bearing, 0.0]
        else:
            crates = [c for c in obs.get("crates", []) if math.dist(c["pos"], pos) < 60]
            if crates and w0 and w0["kind"] == "gun" and w0.get("ammo", 0) > 0:
                c = min(crates, key=lambda c: math.dist(c["pos"], pos))
                aim = self.lead(me, {"pos": c["pos"], "kind": "npc", "vel": [0, 0, 0]}, 252, mh)
                if abs(wrap(me["aim"][0] - aim[0])) < 0.05:
                    fire[0] = True
            elif goal is not None and moving:
                aim = [math.atan2(move[1], move[0]), 0.0]
            else:
                aim = [wrap(me["aim"][0] + 0.6), 0.0]

        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) <= 3 and w0.get("reload", 0) == 0 and target is None:
            act["reload"] = True

        act.update(move=move, boost=bool(boost), jump=jump, aim=aim, fire=fire)
        return act


mf.run(Bot)
