extends SceneTree
## 검증용 분석: 무전 간격, 결정 밀도, 사람을 보냈을 때 도착 시점, 돈·연구 흐름.
## 사용: godot --headless --script res://_sim/trace.gd -- runs=30 policy=best

var runs := 30
var policy := "best"


func _init() -> void:
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=")
		if kv.size() == 2 and kv[0] == "runs":
			runs = int(kv[1])
		if kv.size() == 2 and kv[0] == "policy":
			policy = kv[1]
	var gaps_day: Array = []
	var gaps_night: Array = []
	var max_gap := {"day": [], "night": []}
	var per_hour := {}
	var kinds := {}
	var arrive := {"pressure_leader": [0, 0], "pressure_call": [0, 0], "pair": [0, 0]}
	var money_by_day := {}
	var research_day := {}
	var tut_day1 := 0
	var first_free_decision := []
	var idle_people := 0.0
	var idle_samples := 0
	for i in runs:
		var sim := Sim.new()
		sim.setup(i * 13 + 5)
		var guard := 0
		while not sim.finished and guard < 100:
			guard += 1
			sim.begin_shift({"posts": sim.default_posts(sim.crew_now(), sim.shift), "rate": 90})
			var n0 := sim.history.size()
			while not sim.shift_done:
				sim.advance(1.0)
				if sim.t % 30 == 0:
					# 근무 중인데 일 안 하는 사람 비율
					var on := 0
					var idle := 0
					for p in sim.people:
						if p.on_duty and p.on_site and not p.is_staff():
							on += 1
							if not p.working(sim.now()) and not p.walking():
								idle += 1
					if on > 0:
						idle_people += float(idle) / on
						idle_samples += 1
				while not sim.radio_queue.is_empty():
					var ev := sim.pop_radio()
					sim.resolve(ev, _pick(sim, ev))
			# 이번 교대 무전 시각
			var ts: Array = []
			for h in sim.history.slice(n0):
				if h["what"] == "radio" and h["day"] == sim.day and h["shift"] == sim.shift:
					if h["kind"] in ["result"]:
						continue
					ts.append(h["t"])
					var key: String = h["kind"]
					kinds[key] = kinds.get(key, 0) + 1
					var hr: int = h["t"] / 60
					var hk := "%s%02d" % [sim.shift, hr]
					per_hour[hk] = per_hour.get(hk, 0) + 1
					if h["kind"] == "tutorial" and sim.day == 1 and sim.shift == "day":
						tut_day1 += 1
				if h["what"] == "arrive":
					var a: Array = arrive[h["kind"]]
					a[0] += 1
					if h.get("left", 1) > 0 and h.get("still", true):
						a[1] += 1
			ts.sort()
			var prev := 0
			var mg := 0
			for x in ts + [720]:
				mg = maxi(mg, x - prev)
				prev = x
			max_gap[sim.shift].append(mg)
			var ws := sim.advance_to_next_shift()
			if sim.shift == "day":
				money_by_day[sim.day - 1] = money_by_day.get(sim.day - 1, 0.0) + sim.money
				research_day[sim.day - 1] = research_day.get(sim.day - 1, 0.0) + sim.research["extra"] + sim.research["pressure"]
	var n := float(runs)
	print("== 정책 %s, %d판" % [policy, runs])
	print("무전 종류 (판당):")
	var ks := kinds.keys()
	ks.sort_custom(func(a, b): return kinds[a] > kinds[b])
	for k in ks:
		print("  %-10s %.1f" % [k, kinds[k] / n])
	print("1일차 낮 안내 무전: %.1f개" % (tut_day1 / n))
	for sh in ["day", "night"]:
		var arr: Array = max_gap[sh]
		arr.sort()
		var avg := 0.0
		for x in arr:
			avg += x
		print("%s 교대 안에서 무전 없는 가장 긴 시간: 평균 %d분(1배속 %.1f분), 중앙 %d분, 최악 %d분" % [sh, avg / arr.size(), avg / arr.size() / 60.0 * Defs.HOUR_SEC / 60.0, arr[arr.size() / 2], arr[-1]])
	print("시간대별 무전 (교대당 평균):")
	for sh in ["day", "night"]:
		var line := "  %s " % sh
		for hr in 12:
			var k := "%s%02d" % [sh, hr]
			line += "%.2f " % (per_hour.get(k, 0) / (n * 10.0))
		print(line)
	for k in arrive:
		var a: Array = arrive[k]
		print("보낸 사람 도착 %s: %d번 중 제때 %d번" % [k, a[0], a[1]])
	print("근무 중 일 안 하는 사람 비율: %.0f%%" % (idle_people / maxi(1, idle_samples) * 100.0))
	var line2 := "날짜별 예산: "
	var line3 := "날짜별 연구 단계 합: "
	for d in range(1, 11):
		line2 += "%d:%d " % [d, money_by_day.get(d, 0.0) / n]
		line3 += "%d:%.1f " % [d, research_day.get(d, 0.0) / n]
	print(line2)
	print(line3)
	quit()


func _pick(sim: Sim, ev: Dictionary) -> int:
	var ok: Array = []
	for i in ev["choices"].size():
		if not ev["choices"][i].get("disabled", false):
			ok.append(i)
	if policy == "random":
		return ok[sim.rng.randi() % ok.size()]
	var best: int = ok[0]
	var bp := -1.0
	for i in ok:
		var c: Dictionary = ev["choices"][i]
		if c["key"] in ["ignore", "skip", "refuse"]:
			continue
		if c["pct"] > bp:
			bp = c["pct"]
			best = i
	return best
