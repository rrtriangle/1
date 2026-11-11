extends SceneTree
## 8주(56일) 자동 시뮬레이션. 회의(채용·건설·탈환·감사 대비) → 배치·조명 → 근무 → 보고서 → 주간 결산.
## 사용: godot --headless --script res://_sim/sim4.gd -- runs=1000 policy=all
##   policy: passive, random, casual, careful, hider, expander, greedy, all
##   rest=prod|office|none (careful 계열이 휴게실을 짓는 곳), cctv=1, audit=1 (감사 유형별 결과), verbose=1

const POLICIES := ["passive", "random", "casual", "careful", "hider", "expander", "greedy"]
const SMART := ["careful", "hider", "expander"]

var runs := 300
var only := "all"
var verbose := false
var verbose_audit := false
var restloc := "prod"
var use_cctv := false
var no_hire := false
var rate_override := -1
var rng := RandomNumberGenerator.new()


func _init() -> void:
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=")
		if kv.size() != 2:
			continue
		match kv[0]:
			"runs":
				runs = int(kv[1])
			"policy":
				only = kv[1]
			"verbose":
				verbose = kv[1] == "1"
			"audit":
				verbose_audit = kv[1] == "1"
			"rest":
				restloc = kv[1]
			"cctv":
				use_cctv = kv[1] == "1"
			"nohire":
				no_hire = kv[1] == "1"
			"rate":
				rate_override = int(kv[1])
	rng.randomize()
	print("정책       클리어  교체  해임  인원 | 적자주 1차감사 정기(통과) 특별 중대사고 | 실종 위반  채용 탈환 | 최종예산 신뢰도 | 주간손익 1~8주")
	for p in POLICIES:
		if only == "all" or only == p:
			_bench(p)
	quit()


func _bench(policy: String) -> void:
	var agg := {"win": 0, "교체": 0, "해임": 0, "인원": 0, "deficit": 0, "audits": 0, "regular": 0, "regular_pass": 0,
		"specials": 0, "serious": 0, "vanish": 0, "viol": 0, "hires": 0, "reclaims": 0, "money": 0.0, "trust": 0,
		"w": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "wn": [0, 0, 0, 0, 0, 0, 0, 0], "shipped": 0.0,
		"breaks": 0, "omens": 0, "extras": 0, "idle": 0, "shifts": 0, "pshifts": 0, "mut": 0}
	var types := {}
	for i in runs:
		var r := _run(policy)
		if r["win"]:
			agg["win"] += 1
		else:
			agg[r["reason"]] += 1
		for k in ["deficit", "audits", "regular", "regular_pass", "specials", "serious", "vanish", "viol", "hires", "reclaims", "breaks", "omens", "extras", "idle", "shifts", "pshifts", "mut"]:
			agg[k] += r[k]
		agg["money"] += r["money"]
		agg["trust"] += r["trust"]
		agg["shipped"] += r["shipped"]
		var wh: Array = r["weeks"]
		for w in wh.size():
			if w < 8:
				agg["w"][w] += wh[w]
				agg["wn"][w] += 1
		for e in r["log"]:
			var key: String = e[1] + ":" + e[0]
			if not types.has(key):
				types[key] = [0, 0, 0.0, {}]
			types[key][0] += 1
			if e[3]:
				types[key][1] += 1
			types[key][2] += e[2]
			for nt in e[4]:
				var nk: String = nt.split(":")[0].split(" (")[0]
				if nk.begins_with("근로시간"):
					nk = "근로시간 위반"
				types[key][3][nk] = types[key][3].get(nk, 0) + 1
	var n := float(runs)
	var ws: PackedStringArray = []
	for w in 8:
		ws.append("%+d" % roundi(agg["w"][w] / maxf(1.0, agg["wn"][w])))
	print("%-9s %5.1f%% %4.1f%% %4.1f%% %4.1f%% | %4.2f  %4.2f  %4.2f(%3.0f%%) %4.2f  %4.2f  | %4.2f %5.1f %4.2f %4.2f | %6.0f  %3.0f | %s" % [
		policy, 100.0 * agg["win"] / n, 100.0 * agg["교체"] / n, 100.0 * agg["해임"] / n, 100.0 * agg["인원"] / n,
		agg["deficit"] / n, (agg["audits"] - agg["regular"]) / n, agg["regular"] / n,
		100.0 * agg["regular_pass"] / maxf(1.0, agg["regular"]), agg["specials"] / n, agg["serious"] / n,
		agg["vanish"] / n, agg["viol"] / n, agg["hires"] / n, agg["reclaims"] / n, agg["money"] / n, agg["trust"] / n, " ".join(ws)])
	print("    변이 %.2f명/판" % (agg["mut"] / n))
	if verbose_audit:
		print("    총 출하 %.0f드럼/판 · 붕괴 %.2f · 이상 행동 경고 %.2f · 이상현상 %.1f회 · 교대 %.0f 중 빈 교대 %.1f · 인·교대 %.0f" % [agg["shipped"] / n,
			agg["breaks"] / n, agg["omens"] / n, agg["extras"] / n, agg["shifts"] / n, agg["idle"] / n, agg["pshifts"] / n])
		for k in types.keys():
			var v: Array = types[k]
			print("    %s  %d회, 통과 %.0f%%, 평균 지적 %.1f, 내역 %s" % [k, v[0], 100.0 * v[1] / v[0], v[2] / v[0], str(v[3])])


func _run(policy: String) -> Dictionary:
	var g := Game.new()
	var res := {"win": false, "reason": "", "deficit": 0, "audits": 0, "regular": 0, "regular_pass": 0, "specials": 0,
		"serious": 0, "vanish": 0, "viol": 0, "hires": 0, "reclaims": 0, "money": 0.0, "trust": 0, "weeks": [], "log": [], "shipped": 0.0,
		"breaks": 0, "omens": 0, "extras": 0, "idle": 0, "shifts": 0, "pshifts": 0, "mut": 0}
	var away := {}
	g.alert.connect(func(text: String, kind: String) -> void:
		if kind == "pressure":
			g.resolve_pressure(_pressure_choice(policy, g))
		elif kind == "vanish" and g.day > Game.PROTECT_DAYS:
			res["vanish"] += 1
		elif kind == "break" and text.contains("무너졌다"):
			res["breaks"] += 1
		if verbose:
			print("    [%s %s] %s" % [g.clock_text(), kind, text]))
	if verbose:
		g.log_line.connect(func(t: String) -> void: print("    " + t))
	var guard := 0
	while guard < 150:
		guard += 1
		if g.errand_pending():
			var cands := g.errand_candidates()
			var target: Staff = null
			for s in cands:
				if s.name == "이 주임":
					target = s
			if target == null and not cands.is_empty():
				target = cands[0]
			if policy == "random" and rng.randf() < 0.3:
				target = null
			g.do_errand(target)
		g.check_applicants()
		_meeting(policy, g, res)
		var plan := _plan(policy, g)
		for s in g.staff:
			s.room = "off"
			if s.can_be_assigned() and plan.has(s):
				s.room = plan[s]
		_lights(policy, g)
		g.start_shift()
		away.clear()
		for h in Game.HOURS_PER_SHIFT:
			g.tick_hour()
			_hourly(policy, g, away)
		res["shifts"] += 1
		res["extras"] += g.anomaly_count_shift
		var on := 0
		for s in g.staff:
			if s.worked_now:
				on += 1
			for nt in s.notes:
				if nt.begins_with("⚠"):
					res["omens"] += 1
		res["pshifts"] += on
		if on == 0:
			res["idle"] += 1
		var report := g.end_shift()
		if verbose:
			print(report)
		var inv := g.file_report(_conceal(policy, g))
		if verbose and inv != "":
			print(inv)
		if g.week_ended:
			var text := g.settle_week()
			if verbose:
				print(text)
				print("")
			if g.week_history[-1] < 0.0:
				res["deficit"] += 1
		if g.is_victory():
			res["win"] = true
			break
		if g.is_defeat():
			res["reason"] = g.defeat_reason()
			break
	res["audits"] = g.audits_held
	res["regular"] = g.regular_held
	for e in g.audit_log:
		if e[1] == "정기감사" and e[3]:
			res["regular_pass"] += 1
	res["specials"] = g.specials_held
	res["mut"] = g.mutations
	res["serious"] = g.serious_count
	res["viol"] = g.violations_total
	res["money"] = g.money
	res["trust"] = g.trust
	res["weeks"] = g.week_history
	res["log"] = g.audit_log
	res["shipped"] = g.total_shipped
	for z in Game.RECLAIM_ORDER:
		if g.zones[z]["open"]:
			res["reclaims"] += 1
	return res


# ------------------------------------------------------------------ 회의: 채용·건설·탈환·감사 대비

func _meeting(policy: String, g: Game, res: Dictionary) -> void:
	match policy:
		"passive":
			return
		"random":
			g.reactor_rate = [60, 70, 80, 90, 100, 110, 120][rng.randi() % 7]
			if rng.randf() < 0.2:
				g.post_ad()
			if not g.applicants.is_empty() and rng.randf() < 0.5 and g.alive_count() < g.to_cap:
				g.hire(g.applicants[rng.randi() % g.applicants.size()])
				res["hires"] += 1
			if rng.randf() < 0.1:
				var id: String = g.slot_order[rng.randi() % g.slot_order.size()]
				var opts := g.build_options(id)
				if not opts.is_empty():
					g.build(id, opts[rng.randi() % opts.size()])
			if rng.randf() < 0.05:
				g.start_reclaim(Game.RECLAIM_ORDER[rng.randi() % 3])
			if rng.randf() < 0.1:
				g.request_extension()
			if rng.randf() < 0.1:
				g.correct_reports()
			return
	# 그 밖의 성향: 공고와 채용
	if g.can_post_ad() and not no_hire:
		g.post_ad()
	if not g.applicants.is_empty() and g.alive_count() < g.to_cap:
		var best: Staff = g.applicants[0]
		for a in g.applicants:
			if a.operation + a.observation > best.operation + best.observation:
				best = a
		g.hire(best)
		res["hires"] += 1
	for s in g.staff:
		if s.gone():
			continue
		if policy in SMART and s.contamination >= 70.0:
			if g.can_purify_staff(s) and g.money >= 700.0:
				g.purify_staff(s)
			elif g.can_leave(s):
				g.start_leave(s)
		elif policy == "casual" and s.contamination >= 90.0 and g.can_leave(s):
			g.start_leave(s)
	match policy:
		"casual":
			g.reactor_rate = 100 if g.audit_stage == 0 else 80
			if g.rooms_of("rest").is_empty() and g.money >= 450.0:
				g.build("prod_M2", "rest")
		"greedy":
			g.reactor_rate = 120
			g.request_extension()
		_:
			g.reactor_rate = 90 if g.audit_stage == 0 else 80
			if g.rooms_of("rest").is_empty() and g.money >= 450.0 and restloc != "none":
				g.build("prod_M2" if restloc == "prod" else "office_M", "rest")
			if use_cctv and g.money >= 700.0 and not g.rooms["wha_L"]["cctv"]:
				g.install("wha_L", "cctv")
			if g.audit_stage == 2:
				g.submit_docs()
				g.buy_intel()
				if g.grace_left == 1:
					g.correct_reports()
			if policy == "expander":
				_expand(g)
	if rate_override > 0 and g.audit_stage == 0:
		g.reactor_rate = rate_override


func _expand(g: Game) -> void:
	for z in Game.RECLAIM_ORDER:
		if not g.zones[z]["open"] and not g.zones[z]["reclaiming"]:
			if g.reclaim_block(z) == "" and g.money >= Game.RECLAIM[z]["cost"] + 500.0:
				g.start_reclaim(z)
			break
	# 물류 창고 B를 되찾으면 2호 반응실부터
	if g.zones["wh_b"]["open"] and g.rooms_of("reactor").size() < 2 and g.money >= 1100.0:
		g.build("whb_L1", "reactor")
	# 사람이 한 줄(3명)보다 넉넉하면 두 번째 줄을 짓는다
	elif g.alive_count() >= 8 and g.money >= 1200.0:
		if g.rooms_of("refine").size() < 2:
			for id in ["ref_M1", "prod_M2"]:
				if g.build(id, "refine"):
					break
		elif g.rooms_of("reactor").size() < 3:
			g.build("r2_L1", "reactor")
		elif g.rooms_of("warehouse").size() < 2:
			g.build("whb_L1", "warehouse")


func _conceal(policy: String, g: Game) -> bool:
	if g.shift_incidents.is_empty():
		return false
	match policy:
		"greedy":
			return true
		"random":
			return rng.randf() < 0.5
		"hider":
			if g.in_audit():
				var p: float = Game.DETECT[g.auditor["type"]][g.shift_incidents[0]["kind"]]
				return p < 0.5
			return g.week_profit() < 0.0 and g.regular_next.is_empty()
	return false


# ------------------------------------------------------------------ 배치

func _plan(policy: String, g: Game) -> Dictionary:
	match policy:
		"passive":
			return g.suggest_plan()
		"random":
			var out := {}
			var ids: Array = ["off", "off", "off"]
			ids.append_array(g.assignable_rooms())
			for s in g.staff:
				out[s] = ids[rng.randi() % ids.size()]
			return out
		"casual":
			var p := g.suggest_plan()
			for s in p.keys():
				if p[s] == "off":
					continue
				if s.fatigue >= 85.0 or (g.is_night() and s.omen >= 2):
					# 쉬게 하고, 쉬던 사람 중 규정에 걸리지 않는 사람으로 바꾼다
					var post: String = p[s]
					p[s] = "off"
					for o in p.keys():
						if p[o] == "off" and o != s and o.can_be_assigned() and o.state != "붕괴" and o.fatigue < 85.0 \
								and g.work_check(o) in ["", "연장"] and not (g.is_night() and o.omen >= 2):
							p[o] = post
							break
			return p
		"greedy":
			return _greedy_plan(g)
	return _smart_plan(g)


func _lines(g: Game) -> Array:
	var posts: Array = []
	var lists := [g.rooms_of("reactor"), g.rooms_of("refine"), g.rooms_of("warehouse")]
	for k in 3:
		for l in lists:
			if k < l.size():
				posts.append(l[k])
	return posts


func _smart_plan(g: Game) -> Dictionary:
	var out := {}
	var pool: Array[Staff] = []
	for s in g.staff:
		if s.gone():
			continue
		out[s] = "off"
		if not s.can_be_assigned() or s.state == "붕괴" or not (g.work_check(s) in ["", "연장"]):
			continue
		if s.fatigue >= 75.0 or s.stress >= 90.0 or (g.is_night() and s.omen >= 2):
			continue
		if g.in_audit() and s.has_trait("심부름") and Game.LEDGER_CAP[g.auditor["type"]] >= 2 and g.double_stamps >= Game.DOUBLE_STAMP_MIN - 1:
			continue
		pool.append(s)
	pool.sort_custom(func(a: Staff, b: Staff) -> bool:
		if a.shifts_week != b.shifts_week:
			return a.shifts_week < b.shifts_week
		return a.fatigue < b.fatigue)
	# 감사 주간에 서류를 보는 감사관이 오면 「심부름」 직원은 두 번까지만 쓴다
	var posts: Array = g.posts_for(pool.size())
	var n := mini(pool.size(), posts.size())
	var crew: Array[Staff] = pool.slice(0, n)
	crew.sort_custom(func(a: Staff, b: Staff) -> bool: return a.operation > b.operation)
	for i in n:
		out[crew[i]] = posts[i]
	var spare: Array[Staff] = pool.slice(n)
	if not g.is_night() and n >= 3:
		# 정화 작업 2명, 그다음 위험성평가 1명
		var purify := g.rooms_of("purify")
		if not purify.is_empty() and spare.size() >= 2:
			out[spare[0]] = purify[0]
			out[spare[1]] = purify[0]
			spare = spare.slice(2)
		if not g.risk_week and not spare.is_empty() and not g.rooms_of("qc").is_empty():
			out[spare[0]] = g.rooms_of("qc")[0]
	return out


func _greedy_plan(g: Game) -> Dictionary:
	var out := {}
	var pool: Array[Staff] = []
	for s in g.staff:
		if s.gone():
			continue
		out[s] = "off"
		if s.can_be_assigned() and s.state != "붕괴" and s.fatigue < 95.0:
			pool.append(s)
	pool.sort_custom(func(a: Staff, b: Staff) -> bool: return a.operation > b.operation)
	var posts := _lines(g)
	# 남는 사람은 반응실·정제실 두 번째 자리로
	posts.append_array(g.rooms_of("reactor"))
	posts.append_array(g.rooms_of("refine"))
	for i in mini(pool.size(), posts.size()):
		out[pool[i]] = posts[i]
	return out


# ------------------------------------------------------------------ 조명 (전력 한도 안에서)

func _occupied(g: Game) -> Array:
	var ids: Array = []
	for s in g.staff:
		if s.room != "off" and not s.gone() and not ids.has(s.room):
			ids.append(s.room)
	return ids


func _lights(policy: String, g: Game) -> void:
	if policy == "passive" or not g.is_night():
		return
	if policy == "random":
		for id in g.rooms:
			if rng.randf() < 0.2:
				g.set_room_light(id, not g.rooms[id]["light"])
		for z in g.zones:
			if rng.randf() < 0.2:
				g.set_corr_light(z, not g.zones[z]["corr_light"])
		return
	var occ := _occupied(g)
	if policy == "casual" or policy == "greedy":
		# 사람 없는 방의 불을 끄고, 사람 있는 방을 켠다. 복도는 그대로.
		for id in g.rooms:
			if not occ.has(id):
				g.set_room_light(id, false)
		for id in occ:
			g.set_room_light(id, true)
		return
	# 신중한 성향: 전부 끄고 중요한 순서대로 켠다
	for id in g.rooms:
		g.set_room_light(id, false)
	for z in g.zones:
		g.set_corr_light(z, false)
	for id in occ:
		g.set_room_light(id, true)
	var zs: Array = []
	for id in occ:
		var z: String = g.rooms[id]["zone"]
		if g.zones[z]["open"] and not zs.has(z):
			zs.append(z)
	for id in g.rooms_of("rest"):
		g.set_room_light(id, true)
		var z2: String = g.rooms[id]["zone"]
		if not zs.has(z2):
			zs.append(z2)
	for z in zs:
		g.set_corr_light(z, true)


func _pressure_choice(policy: String, g: Game) -> int:
	var stopper := g.stopwork_staff()
	match policy:
		"passive", "random":
			return rng.randi() % (3 if stopper != null else 2)
		"casual":
			if stopper != null and rng.randf() < 0.5:
				return 2
			return rng.randi() % 2
		"greedy":
			return 1
	var best := 0.0
	if not g.pending_pressure.is_empty():
		for s in g.working_in(g.pending_pressure[0]):
			best = maxf(best, s.operation)
	if best >= 70.0:
		return 1
	if stopper != null:
		return 2
	return 1 if best >= 60.0 else 0


# ------------------------------------------------------------------ 근무 중

func _hourly(policy: String, g: Game, away: Dictionary) -> void:
	var notice := 0.0
	match policy:
		"careful", "hider", "expander":
			notice = 0.6
		"casual":
			notice = 0.2
		"random":
			if rng.randf() < 0.1:
				var on: Array[Staff] = []
				for s in g.staff:
					if s.on_map() and s.can_work() and s.transit == 0:
						on.append(s)
				var ids := g.assignable_rooms()
				if not on.is_empty() and not ids.is_empty():
					g.order_move(on[rng.randi() % on.size()], ids[rng.randi() % ids.size()])
			return
		_:
			return
	# 지난 시간에 빼 둔 사람 복귀
	for s in away.keys():
		if s.on_map() and s.can_work() and g.rooms.has(away[s]):
			g.order_move(s, away[s])
	away.clear()
	if not g.is_night():
		return
	for id in g.rooms.keys():
		var r: Dictionary = g.rooms[id]
		if not r["extra"]:
			continue
		var p := notice
		if r["cctv"]:
			p = 1.0
		elif not r["light"]:
			continue
		elif g.auditor_room == id:
			p *= 0.6  # 감사관이 섞여 인원수가 헷갈린다
		if rng.randf() >= p:
			continue
		var v: Staff = r["victim"]
		if v == null or not v.on_map() or not v.can_work() or v.room != id or v.transit > 0:
			continue
		# 가장 가까운, 사람이 있는 다른 방이나 휴게실로 잠깐 뺀다
		var best := ""
		var best_h := 99
		for rid in g.rooms.keys():
			if rid == id:
				continue
			var t: String = g.rooms[rid]["type"]
			if t != "rest" and g.present_in(rid).is_empty():
				continue
			if t == "smoke" or t == "purify":
				continue
			var h := g.hops(r["zone"], g.rooms[rid]["zone"])
			if h < best_h:
				best_h = h
				best = rid
		if best != "" and (best_h <= 1 or policy == "casual"):
			away[v] = id
			g.order_move(v, best)
