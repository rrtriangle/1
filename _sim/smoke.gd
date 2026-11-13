extends SceneTree
## 규칙만으로 데모 10일을 자동으로 돌린다 (화면 없음).
## 사용: godot --headless --script res://_sim/smoke.gd -- runs=100 policy=all
## 정책: first(첫 선택지), random(무작위), best(성공 확률이 가장 높은 것, 없으면 첫 번째)

var runs := 50
var policy := "all"


func _init() -> void:
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=")
		if kv.size() == 2 and kv[0] == "runs":
			runs = int(kv[1])
		if kv.size() == 2 and kv[0] == "policy":
			policy = kv[1]
	var pols := ["first", "random", "best"] if policy == "all" else [policy]
	print("정책    끝 예산   적자주  무전/교대(주·야)   적발  역압막음/사고  완벽  호출  실종  연구(한명더/역압)")
	for pol in pols:
		_run_policy(pol)
	quit()


func _run_policy(pol: String) -> void:
	var tot := {"money": 0.0, "deficit": 0.0, "rd": 0.0, "rn": 0.0, "det": 0.0, "pok": 0.0, "pbad": 0.0,
		"perf": 0.0, "calls": 0.0, "van": 0.0, "re": 0.0, "rp": 0.0}
	var min_r := 99
	var max_r := 0
	for i in runs:
		var sim := Sim.new()
		sim.setup(i * 7 + 1)
		var ap := AutoPlayer.new(pol, i)
		var day_r := 0
		var night_r := 0
		var day_n := 0
		var night_n := 0
		var guard := 0
		while not sim.finished and guard < 100:
			guard += 1
			var plan := {"posts": sim.default_posts(sim.crew_now(), sim.shift), "rate": 90 if pol != "random" else [80, 90, 100, 110][i % 4]}
			sim.begin_shift(plan)
			var steps := 0
			while not sim.shift_done and steps < 20000:
				steps += 1
				sim.advance(1.0)
				ap.step(sim)
			if sim.is_night():
				night_r += sim.radio_count
				night_n += 1
			else:
				day_r += sim.radio_count
				day_n += 1
			min_r = mini(min_r, sim.radio_count)
			max_r = maxi(max_r, sim.radio_count)
			var week := sim.advance_to_next_shift()
			if not week.is_empty() and week["profit"] < 0:
				tot["deficit"] += 1
		tot["money"] += sim.money
		tot["rd"] += float(day_r) / maxi(1, day_n)
		tot["rn"] += float(night_r) / maxi(1, night_n)
		tot["det"] += sim.stats["detected"]
		tot["pok"] += sim.stats["pressure_ok"]
		tot["pbad"] += sim.stats["pressure_bad"]
		tot["perf"] += sim.stats["perfect"]
		tot["calls"] += sim.stats["calls"]
		tot["van"] += sim.stats["vanished"]
		tot["re"] += sim.research["extra"]
		tot["rp"] += sim.research["pressure"]
	var n := float(runs)
	print("%-7s %7d   %.2f    %.1f · %.1f (최소 %d 최대 %d)   %.1f    %.1f / %.1f     %.1f  %.1f  %.2f  %.1f / %.1f" % [
		pol, tot["money"] / n, tot["deficit"] / n, tot["rd"] / n, tot["rn"] / n, min_r, max_r,
		tot["det"] / n, tot["pok"] / n, tot["pbad"] / n, tot["perf"] / n, tot["calls"] / n, tot["van"] / n,
		tot["re"] / n, tot["rp"] / n])


func _pick(sim: Sim, ev: Dictionary, pol: String) -> int:
	var ok: Array = []
	for i in ev["choices"].size():
		if not ev["choices"][i].get("disabled", false):
			ok.append(i)
	if ok.is_empty():
		return 0
	match pol:
		"random":
			return ok[sim.rng.randi() % ok.size()]
		"best":
			var best: int = ok[0]
			var bp := -1.0
			for i in ok:
				var p: float = ev["choices"][i]["pct"]
				var key: String = ev["choices"][i]["key"]
				if key in ["ignore", "skip", "refuse"]:
					continue
				if p > bp:
					bp = p
					best = i
			return best
	return ok[0]
