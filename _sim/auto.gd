class_name AutoPlayer
extends RefCounted
## 자동 플레이어 (검증용). 짐작으로 만든 모델이다.
## first: 알림을 무시하고 무전은 첫 선택지 / random: 무작위 / best: 알림에 사람을 보내고 성공 확률 높은 쪽

var policy := "best"
var rng := RandomNumberGenerator.new()


func _init(p: String, seed_value: int) -> void:
	policy = p
	rng.seed = seed_value


func step(sim: Sim) -> void:
	while not sim.alerts.is_empty():
		_on_alert(sim, sim.pop_alert())
	while not sim.radio_queue.is_empty():
		var ev := sim.pop_radio()
		sim.resolve(ev, pick(sim, ev))
	if policy == "best" and sim.is_night() and sim.t % 120 == 60:
		sim.headcount()


func _on_alert(sim: Sim, a: Dictionary) -> void:
	if policy == "first":
		return
	if policy == "random" and rng.randf() < 0.5:
		return
	var room: String = a["room"]
	if room == "" or not Defs.ROOMS.has(room):
		return
	if room == "reactor" and sim._has_crisis("pressure"):
		var best: Person = null
		for p in sim.on_site_people():
			if p.on_duty and not p.is_staff() and p.room != "reactor" and (best == null or p.eff_skill() > best.eff_skill()):
				best = p
		if best != null:
			sim.order(best, "reactor")
		return
	if room == sim.entity_room or sim.door_open.has(room) or not sim.lit.get(room, true):
		var helper: Person = null
		var bd := 1e9
		for p in sim.on_site_people():
			if not p.on_duty or p.is_staff() or p.room == room or p.walking() or p.role == "반장":
				continue
			var d := float(sim.walk_minutes(p, room))
			if p.post == "patrol":
				d -= 30.0
			if d < bd:
				bd = d
				helper = p
		if helper != null:
			sim.order(helper, room)


func pick(sim: Sim, ev: Dictionary) -> int:
	var ok: Array = []
	for i in ev["choices"].size():
		if not ev["choices"][i].get("disabled", false):
			ok.append(i)
	if ok.is_empty():
		return 0
	if policy == "random":
		return ok[rng.randi() % ok.size()]
	if policy == "first":
		return ok[0]
	if ev["kind"] == "pressure":
		var who: Person = ev["who"]
		var left: float = sim._pressure_crisis().get("left", 0.0)
		if who.eff_skill() < 60 and left > 40:
			for i in ok:
				if ev["choices"][i]["key"] == "wait":
					return i
	var best: int = ok[0]
	var bp := -1.0
	for i in ok:
		var c: Dictionary = ev["choices"][i]
		if c["key"] in ["ignore", "skip", "refuse", "wait"]:
			continue
		if c["pct"] > bp:
			bp = c["pct"]
			best = i
	return best
