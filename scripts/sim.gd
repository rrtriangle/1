class_name Sim
extends RefCounted
## 게임 규칙 전부. 화면과 분리되어 있어 자동 시뮬레이션에서도 그대로 쓴다.
## 흐름: begin_shift(계획) → advance(분)을 반복 → 무전이 오면 화면이 멈추고 resolve(무전, 선택) → 교대 끝 → 다음 회의

signal log_line(text: String)
signal popup(text: String, room: String, col: Color)
signal sound(name: String)
signal highlight(room: String)

const GOOD := Color(0.55, 0.95, 0.6)
const BAD := Color(1.0, 0.45, 0.4)
const GOLD := Color(1.0, 0.85, 0.3)
const MONEY_IN := Color(0.6, 1.0, 0.65)
const MONEY_OUT := Color(1.0, 0.65, 0.5)

var rng := RandomNumberGenerator.new()
var people: Array[Person] = []

var day := 1
var shift := "day"            # "day" 또는 "night"
var t := 0                    # 교대 시작부터 지난 분 (0~720)
var sub := 0.0                # 1분 미만 누적
var shift_done := false
var finished := false         # 데모 끝

var money := Defs.START_MONEY
var week_income := 0.0
var week_cost := 0.0
var shift_income := 0.0
var shift_cost := 0.0
var rate := 90                # 반응기 가동률 (%)
var pair_rule := false        # 수칙 「2인 1조」 (연구로 해금)
var violations := 0           # 대타 위반 기록 (감사 때 돌아온다, v0.7)

var tank := 30.0
var buffer := 0.0
var outbox := 0.0
var stock := 10.0
var lit := {}                 # 방 → 불 켜짐
var reactor_down_until := -1
var pressure_mult := 1.0      # 정비 점검을 하면 낮아진다
var pressure_mult_until_day := 0
var raw_bad_today := false
var rush_order := false

var records := 0              # 관찰 기록
var research := {"extra": 0, "pressure": 0}
var research_progress := {"extra": 0, "pressure": 0}
var research_target := "extra"

var radio_queue: Array = []   # 아직 보여 주지 않은 무전
var crises: Array = []        # 진행 중인 위기 [{kind, room, left, ev}]
var entity_room := ""         # '한 명 더'가 있는 방
var entity_since := -1
var entity_reported := false
var last_radio_t := -999
var radio_count := 0
var next_gap := 90            # 다음 잔잔한 무전까지 (분). 무전이 끊기지 않게 한다
var kinds_shift := {}         # 이번 교대에 나온 무전 종류 (반복 줄이기)
var last_filler := ""
var used_text := {}           # 이미 쓴 문구
var owned := {}               # 설비 투자: "cctv", "chair", "em_<방>"
var party_week := -1
var perfect_shift := false    # ★ 완벽한 차단: 이번 교대 역압 면역
var anomaly_rooms_last_night: Array = []
var anomaly_rooms_tonight: Array = []

var shift_log: Array = []     # 보고서용 한 줄들
var week_log: Array = []
var stats := {"detected": 0, "pressure_ok": 0, "pressure_bad": 0, "perfect": 0, "vanished": 0,
	"calls": 0, "holds": 0, "park_right": 0, "park_wrong": 0}
var park_used := {}
var errand_state := ""        # "", "asked", "away", "back", "refused"
var errand_person: Person = null
var tut_fired := {}
var history: Array = []       # 분석용: [{day, shift, t, what, ...}]


func setup(seed_value: int = -1) -> void:
	if seed_value >= 0:
		rng.seed = seed_value
	else:
		rng.randomize()
	people.clear()
	for d in Defs.ROSTER:
		people.append(Person.new(d))
	for id in Defs.ROOMS:
		lit[id] = true


# ================================================================ 시간·근무표

func now() -> int:
	return ((day - 1) * 2 + (1 if shift == "night" else 0)) * Defs.SHIFT_MIN + t


func clock_text() -> String:
	var m := (8 * 60 + t) if shift == "day" else (20 * 60 + t)
	m = m % 1440
	return "%02d:%02d" % [m / 60, m % 60]


func is_night() -> bool:
	return shift == "night"


func crew_now() -> int:
	return Defs.crew_on(day, shift)


func next_shift() -> Array:
	if shift == "day":
		return [day, "night"]
	return [day + 1, "day"]


func crew_members(c: int) -> Array[Person]:
	var out: Array[Person] = []
	for p in people:
		if p.crew == c and not p.gone:
			out.append(p)
	return out


func leader_of(c: int) -> Person:
	for p in crew_members(c):
		if p.role == "반장":
			return p
	return null


func available(p: Person) -> bool:
	return not p.gone and p.injured_until_day <= day


func on_site_people() -> Array[Person]:
	var out: Array[Person] = []
	for p in people:
		if p.on_site and not p.gone:
			out.append(p)
	return out


func in_room(id: String) -> Array[Person]:
	var out: Array[Person] = []
	for p in people:
		if p.on_site and not p.gone and not p.walking() and p.room == id:
			out.append(p)
	return out


func duty_leader() -> Person:
	var l := leader_of(crew_now())
	if l != null and l.on_site and l.on_duty:
		return l
	for p in people:
		if p.on_duty and p.on_site:
			return p
	return null


## 지금 쉬고 있어서 부를 수 있는 사람 (휴무 조, 또는 다음 근무가 아닌 조)
func callable_people() -> Array[Person]:
	var out: Array[Person] = []
	for p in people:
		if p.is_staff() or p.on_site or p.gone or not available(p) or p.arrive_at >= 0:
			continue
		out.append(p)
	out.sort_custom(func(a: Person, b: Person) -> bool: return a.fatigue < b.fatigue)
	return out


# ================================================================ 회의: 기본 배치

## 조원 다섯 명의 기본 자리. 반장은 조정실, 숙련 높은 순으로 반응실·정제실, 관찰 높은 사람이 순찰.
func default_posts(c: int, for_shift: String) -> Dictionary:
	var out := {}
	var crew := crew_members(c).filter(func(p: Person) -> bool: return available(p))
	var others: Array = []
	for p in crew:
		if p.role == "반장":
			out[p] = "control"
		else:
			others.append(p)
	others.sort_custom(func(a: Person, b: Person) -> bool: return a.skill > b.skill)
	var order := ["reactor", "refine", "store"]
	if for_shift == "night":
		if pair_rule:
			order = ["reactor", "reactor", "refine", "refine"]
		else:
			order = ["reactor", "refine", "store", "patrol"]
	else:
		order = ["reactor", "refine", "store", "assist"]
	# 순찰은 관찰이 높은 사람이 맡는다
	if order.has("patrol") and others.size() >= 4:
		var best: Person = others[0]
		for p in others:
			if p.observe > best.observe:
				best = p
		others.erase(best)
		others.append(best)
	for i in others.size():
		out[others[i]] = order[i] if i < order.size() else "assist"
	if not out.values().has("control") and not others.is_empty():
		pass
	return out


# ================================================================ 교대 시작

## plan: {"posts": {Person: post}, "holds": [Person], "calls": [Person], "rate": int, "pair": bool, "target": String}
func begin_shift(plan: Dictionary) -> void:
	shift_done = false
	t = 0
	sub = 0.0
	shift_income = 0.0
	shift_cost = 0.0
	shift_log.clear()
	crises.clear()
	entity_room = ""
	entity_reported = false
	perfect_shift = false
	rush_order = false
	radio_count = 0
	last_radio_t = -999
	rate = plan.get("rate", rate)
	pair_rule = plan.get("pair", pair_rule)
	research_target = plan.get("target", research_target)
	if shift == "night":
		anomaly_rooms_tonight.clear()
	else:
		raw_bad_today = false
	var posts: Dictionary = plan.get("posts", {})
	var holds: Array = plan.get("holds", [])
	var calls: Array = plan.get("calls", [])
	# 지난 교대 근무자: 붙잡지 않은 사람은 교대해 줄 사람이 오면 퇴근
	for p in people:
		if p.gone:
			continue
		p.broken = p.stress >= Defs.STRESS_BREAK
		if p.on_site and not holds.has(p) and not posts.has(p):
			p.on_duty = false
			p.leaving = true
			if p.is_staff() or not Defs.PRODUCTION.has(p.post_room()) or p.post == "assist":
				_go_home(p)
	# 이번 근무자: 정문에서 걸어 들어온다
	for p in posts:
		var person: Person = p
		person.post = posts[p]
		person.on_duty = true
		person.leaving = false
		if holds.has(person):
			person.fatigue += Defs.HOLD_FATIGUE
			person.extra_shifts += 1
			violations += 1
			stats["holds"] += 1
			_pay(Defs.CALL_PAY, "붙잡기 수당")
			_note("%s을(를) 퇴근시키지 않고 붙잡았다. (위반 기록 1, 수당 −%d)" % [person.name, Defs.CALL_PAY])
			_send(person, person.post_room() if person.post != "patrol" else "control", _arrived_post.bind(person))
		elif person.on_site:
			_send(person, person.post_room() if person.post != "patrol" else "control", _arrived_post.bind(person))
		else:
			_enter_gate(person)
			_send(person, person.post_room() if person.post != "patrol" else "control", _arrived_post.bind(person))
	for p in calls:
		_call_in(p, "회의에서 미리 부름", 0)
	# 상근직: 낮에만 온다
	for p in people:
		if p.is_staff() and not p.gone:
			if shift == "day" and available(p):
				p.post = Defs.STAFF_POST[p.role]
				p.on_duty = true
				if not p.on_site:
					_enter_gate(p)
				_send(p, p.post, Callable())
			else:
				p.on_duty = false
				if p.on_site:
					_go_home(p)
	# 이번 교대의 잔잔한 무전 시점 (2개)
	kinds_shift.clear()
	next_gap = rng.randi_range(50, 80)
	door_open.clear()
	for p in people:
		p.contam_k = 0.7 if day < mask_until_day else 1.0
	sound.emit("night" if is_night() else "day")


func _arrived_post(p: Person) -> void:
	# 교대: 같은 방에서 일하던 지난 근무자는 이제 퇴근한다
	for q in people:
		if q != p and q.leaving and q.on_site and not q.walking() and q.room == p.room:
			_go_home(q)
	if p.post == "patrol":
		p.patrol_i = 0
		p.patrol_wait = 0


func _enter_gate(p: Person) -> void:
	p.on_site = true
	p.room = "gate"
	p.pos = Defs.room_center("gate") + Vector2(rng.randf_range(-40, 40), rng.randf_range(-20, 30))
	p.path.clear()


func _go_home(p: Person) -> void:
	p.on_duty = false
	p.leaving = false
	p.busy_until = -1
	_send(p, "gate", _left_site.bind(p))


func _left_site(p: Person) -> void:
	if p.on_duty:
		return
	p.on_site = false
	p.room = ""


## 사람을 방으로 걸어가게 한다. 도착하면 cb를 부른다.
func _send(p: Person, to: String, cb: Callable) -> void:
	if to == "":
		return
	if p.room == to and not p.walking():
		p.dest = to
		if cb.is_valid():
			cb.call()
		return
	var from_room := p.room
	p.path = Defs.path_to(p.pos, from_room, to)
	p.dest = to
	p.room = ""
	p.on_arrive = cb
	if p.pos.distance_to(p.path[0]) < 1.0:
		p.path.remove_at(0)


## 걷는 시간 예상 (분)
func walk_minutes(p: Person, to: String) -> int:
	var pts := Defs.path_to(p.pos, p.room, to)
	var d := 0.0
	var cur := p.pos
	for q in pts:
		d += cur.distance_to(q)
		cur = q
	return int(ceil(d / Defs.WALK_PX_PER_MIN))


## 쉬고 있는 사람을 부른다. delay 분 뒤 정문에 도착.
func _call_in(p: Person, why: String, delay: int = Defs.CALL_TRAVEL_MIN, post_override: String = "") -> void:
	if p == null:
		return
	if delay == Defs.CALL_TRAVEL_MIN:
		delay += rng.randi_range(-Defs.CALL_TRAVEL_SPREAD, Defs.CALL_TRAVEL_SPREAD)
	p.arrive_at = now() + delay
	p.on_duty = true
	p.post = post_override if post_override != "" else ("assist" if not is_night() else "patrol")
	p.fatigue += Defs.HOLD_FATIGUE
	p.extra_shifts += 1
	violations += 1
	stats["calls"] += 1
	_pay(Defs.CALL_PAY, "호출 수당")
	_note("%s 호출 (%s). 위반 기록 1, 수당 −%d." % [p.name, why, Defs.CALL_PAY])


# ================================================================ 공장장의 직접 지시

## 직원을 골라 방을 누르면 무전으로 지시한다. 반장 성격에 따라 안 따를 수도 있다.
func order(p: Person, id: String) -> String:
	if p == null or p.gone or not p.on_site:
		return "%s은(는) 지금 공장에 없다." % (p.name if p != null else "")
	if p.broken:
		return "%s은(는) 지금 지시를 들을 상태가 아니다." % p.name
	if p.has_trait("고집불통") and rng.randf() < 0.15:
		return "%s: \"괜찮아요, 제가 알아서 할게요.\" (고집불통: 지시를 따르지 않았다)" % p.name
	var rname: String = Defs.ROOMS[id]["name"]
	if not lit[id]:
		_send(p, id, _fix_light.bind(p, id))
		return "%s → %s: 가서 불을 켠다 (도착 후 %d분)." % [p.name, rname, Defs.FIX_LIGHT_MIN]
	p.post = id
	p.busy_until = -1
	_send(p, id, Callable())
	var w := walk_minutes(p, id)
	if id == "rest":
		return "%s → 휴게실: 쉰다. 시간당 피로 −15." % p.name
	return "%s → %s (걸어서 약 %d분)." % [p.name, rname, w]


# ================================================================ 설비 투자

func can_buy(item: String, room: String = "") -> bool:
	var cost: float = Defs.SHOP[item]["cost"]
	if money < cost:
		return false
	match item:
		"em":
			return not owned.has("em_" + room)
		"party":
			return party_week != (day - 1) / Defs.WEEK_DAYS
	return not owned.has(item)


func buy(item: String, room: String = "") -> void:
	if not can_buy(item, room):
		return
	var cost: float = Defs.SHOP[item]["cost"]
	_pay(cost, Defs.SHOP[item]["name"])
	match item:
		"em":
			owned["em_" + room] = true
			_note("%s에 비상 조명을 달았다 (−%d)." % [Defs.ROOMS[room]["name"], cost])
		"party":
			party_week = (day - 1) / Defs.WEEK_DAYS
			for p in people:
				if not p.gone:
					p.add_stress(-20.0)
			_note("회식을 했다. 전원 스트레스 −20 (−%d)." % cost)
		_:
			owned[item] = true
			_note("%s을(를) 들였다 (−%d)." % [Defs.SHOP[item]["name"], cost])
	sound.emit("build")


# ================================================================ 시간 흐름

## 게임 시간 minutes분 만큼 진행한다. 무전이 쌓이면 화면이 멈춘다.
func advance(minutes: float) -> void:
	if shift_done or finished:
		return
	_move_people(minutes)
	sub += minutes
	while sub >= 1.0 and not shift_done:
		sub -= 1.0
		_tick_minute()
		if not radio_queue.is_empty():
			break


func _move_people(minutes: float) -> void:
	for p in people:
		if not p.on_site or p.path.is_empty():
			continue
		var step := Defs.WALK_PX_PER_MIN * minutes
		while step > 0.0 and not p.path.is_empty():
			var target: Vector2 = p.path[0]
			var d := p.pos.distance_to(target)
			if d <= step:
				p.pos = target
				step -= d
				p.path.remove_at(0)
			else:
				p.pos += (target - p.pos) / d * step
				step = 0.0
		if p.path.is_empty():
			p.room = p.dest
			var cb := p.on_arrive
			p.on_arrive = Callable()
			if cb.is_valid():
				cb.call()


func _tick_minute() -> void:
	t += 1
	var n := now()
	# 호출받은 사람 도착
	for p in people:
		if p.arrive_at >= 0 and n >= p.arrive_at:
			p.arrive_at = -1
			_enter_gate(p)
			var target := p.post_room() if p.post != "patrol" else "control"
			_send(p, target, _arrived_post.bind(p))
			_park_says_arrival(p)
	if t == 30:
		for p in people:
			if p.leaving and p.on_site and not p.walking():
				_go_home(p)
	_produce(1.0)
	_patrol()
	_tick_crises()
	_tick_entity()
	_tutorial()
	if not is_night():
		_day_schedule()
	if t % 60 == 0:
		_hourly()
	# 무전이 한동안 없으면 잔잔한 일이 생긴다 (교대 끝 40분 전까지)
	if t - last_radio_t >= next_gap and radio_queue.is_empty() and t < Defs.SHIFT_MIN - 40 and not (day == 1 and shift == "day" and t < 240):
		_filler_event()
		next_gap = rng.randi_range(110, 160)
	if t >= Defs.SHIFT_MIN:
		_end_shift()


func _room_eff(id: String) -> float:
	var n := now()
	var ws: Array = []
	for p in people:
		if p.on_duty or p.leaving:
			if p.working(n) and p.room == id and p.post_room() == id and p.post != "patrol":
				ws.append(p)
	ws.sort_custom(func(a: Person, b: Person) -> bool: return a.eff_skill() > b.eff_skill())
	var e := 0.0
	for i in ws.size():
		var w: Person = ws[i]
		var f := clampf(w.eff_skill() / 60.0, 0.4, 1.4)
		if w.has_trait("꼼꼼함"):
			f *= 0.9
		e += f * (1.0 if i == 0 else Defs.SECOND_WORKER)
	if not lit[id]:
		e *= Defs.DARK_WORK
	return e


func _produce(m: float) -> void:
	var h := m / 60.0
	# 반응실
	var re := 0.0
	if reactor_down_until <= now():
		re = rate / 100.0 * _room_eff("reactor") * h
	re = minf(re, tank)
	re = minf(re, Defs.BUFFER_CAP - buffer)
	re = maxf(re, 0.0)
	tank -= re
	buffer += re
	# 정제실
	var rf_k := 0.7 if refine_slow_until > now() else 1.0
	var rf := minf(Defs.REFINE_RATE * _room_eff("refine") * h * rf_k, buffer)
	rf = minf(rf, Defs.OUTBOX_CAP - outbox)
	rf = maxf(rf, 0.0)
	buffer -= rf
	outbox += rf
	# 창고
	var workers := 0.0
	for p in in_room("store"):
		if (p.on_duty or p.leaving) and p.working(now()) and p.post_room() == "store" and p.post != "patrol":
			workers += 1.0 if lit["store"] else Defs.DARK_WORK
	var hl := minf(Defs.HAUL_RATE * workers * h, outbox)
	hl = minf(hl, Defs.STORE_CAP - stock)
	hl = maxf(hl, 0.0)
	outbox -= hl
	stock += hl


func _patrol() -> void:
	var n := now()
	for p in people:
		if p.post != "patrol" or not p.on_duty or not p.on_site or p.walking() or p.busy_until > n or p.broken:
			continue
		if p.room != "" and not lit.get(p.room, true):
			_fix_light(p, p.room)
			continue
		p.patrol_wait += 1
		if p.patrol_wait >= 25:
			p.patrol_wait = 0
			p.patrol_i = (p.patrol_i + 1) % Defs.PATROL_ROUTE.size()
			_send(p, Defs.PATROL_ROUTE[p.patrol_i], Callable())


func _fix_light(p: Person, id: String) -> void:
	for c in crises:
		if c["kind"] == "light" and c["room"] == id:
			return
	p.busy_until = now() + Defs.FIX_LIGHT_MIN
	p.busy_what = "불 켜는 중"
	crises.append({"kind": "light", "room": id, "left": Defs.FIX_LIGHT_MIN, "who": p})


func _hourly() -> void:
	var n := now()
	for p in on_site_people():
		if p.on_duty and not p.walking():
			if p.room == "rest":
				p.fatigue = maxf(0.0, p.fatigue - (30.0 if owned.has("chair") else 15.0))
				p.add_stress(-4.0)
			else:
				p.fatigue = minf(100.0, p.fatigue + (Defs.FATIGUE_NIGHT_H if is_night() else Defs.FATIGUE_WORK_H))
			if p.errand and is_night():
				p.add_contam(0.35)
	# 연구 (낮, 연구소에 연구원이 있을 때)
	if not is_night():
		for p in in_room("lab"):
			if p.role == "연구원" and p.working(n):
				_research_hour()
	# 역압
	if not perfect_shift and reactor_down_until <= n and not _has_crisis("pressure") and rate > 80:
		var k := Defs.PRESSURE_K * pow((rate - 80) / 20.0, 2.0) * pressure_mult
		k *= Defs.PRESSURE_NIGHT if is_night() else Defs.PRESSURE_DAY
		if raw_bad_today and not is_night():
			k *= 1.5
		if rng.randf() < k:
			_start_pressure()
	# 한 명 더 (밤, 혼자 있는 방)
	if is_night() and entity_room == "" and day > 1:
		for id in ["reactor", "refine", "store", "tank", "dock", "rest"]:
			var ppl := in_room(id).filter(func(q: Person) -> bool: return q.on_duty)
			var stage3 := ppl.any(func(q: Person) -> bool: return q.stage() >= 3)
			if ppl.size() != 1 and not stage3:
				continue
			var k2 := Defs.EXTRA_ALONE
			if not lit[id]:
				k2 += Defs.EXTRA_DARK
			if stage3:
				k2 += Defs.EXTRA_STAGE3
			if door_open.has(id):
				k2 += 0.04
			if rng.randf() < k2:
				_spawn_entity(id)
				break
	# 정전
	var ko := Defs.OUTAGE_NIGHT if is_night() else Defs.OUTAGE_DAY
	if rng.randf() < ko and _lit_count() >= 2:
		_outage()


func _lit_count() -> int:
	var c := 0
	for id in ["reactor", "refine", "store", "tank", "dock"]:
		if lit[id]:
			c += 1
	return c


func _has_crisis(kind: String) -> bool:
	for c in crises:
		if c["kind"] == kind:
			return true
	return false


func _tick_crises() -> void:
	for c in crises.duplicate():
		c["left"] -= 1
		if c["left"] > 0:
			continue
		crises.erase(c)
		match c["kind"]:
			"pressure":
				_result_radio("반응실", _pressure_blow())
			"errand":
				_errand_back(c["who"])
			"investigate":
				var iv: Person = c["who"]
				iv.busy_until = -1
				records += c["gain"]
				popup.emit("관찰 기록 +%d" % c["gain"], c["room"], GOOD)
				_note("%s이(가) 조사를 마쳤다. 관찰 기록 +%d." % [iv.name, c["gain"]])
				if iv.on_duty:
					_send(iv, iv.post_room() if iv.post != "patrol" else "control", Callable())
			"light":
				lit[c["room"]] = true
				var who: Person = c["who"]
				who.busy_until = -1
				if who.on_duty and who.post != "patrol" and who.post_room() != c["room"]:
					_send(who, who.post_room(), Callable())
				popup.emit("불 켜짐", c["room"], GOOD)
				sound.emit("light")
				_note("%s 불이 다시 들어왔다 (%s)." % [Defs.ROOMS[c["room"]]["name"], who.name])
			"rest":
				var p: Person = c["who"]
				p.busy_until = -1
				if p.on_duty and p.on_site:
					_send(p, p.post_room() if p.post != "patrol" else "control", Callable())


func _day_schedule() -> void:
	if t == Defs.RAW_DELIVERY_T:
		var need := floorf(Defs.TANK_CAP - tank)
		if rng.randf() < 0.15 and day > Defs.PROLOGUE_DAYS:
			_radio_raw_bad(need)
		else:
			_buy_raw(need)
	if Defs.TRUCK_T.has(t):
		_truck()
	if t == 20 and anomaly_rooms_last_night.size() > 0 and day > 2:
		_radio_trace()
	if t == 200 and day > Defs.PROLOGUE_DAYS and rng.randf() < 0.3:
		_radio_rush()
	if t == 300 and day > Defs.PROLOGUE_DAYS and rng.randf() < 0.3 and pressure_mult_until_day < day:
		_radio_maint_check()


func _buy_raw(n: float) -> void:
	if n <= 0:
		return
	tank += n
	var c := n * Defs.RAW_PRICE
	_pay(c, "원료")
	popup.emit("원료 %d드럼  −%d" % [n, c], "tank", MONEY_OUT)
	_note("원료 %d드럼이 들어왔다 (−%d)." % [n, c])


func _truck() -> void:
	var loaders := in_room("store").filter(func(q: Person) -> bool: return q.on_duty and q.working(now()))
	if loaders.is_empty() and stock >= 1.0:
		_radio_no_loader()
		return
	_load_truck(1.0)


func _load_truck(k: float) -> void:
	var cap := Defs.TRUCK_CAP
	var extra := 0
	if rush_order and t >= 480:
		cap += Defs.RUSH_DRUMS
	var n := int(minf(stock, cap * k))
	stock -= n
	var normal := mini(n, Defs.TRUCK_CAP)
	var income := normal * Defs.SELL_PRICE
	if rush_order and t >= 480:
		extra = n - normal
		income += extra * Defs.RUSH_PRICE
		if extra < Defs.RUSH_DRUMS:
			income -= Defs.RUSH_PENALTY
			_note("긴급 주문 %d드럼 중 %d드럼만 실었다. 위약금 −%d." % [Defs.RUSH_DRUMS, extra, Defs.RUSH_PENALTY])
		else:
			_note("긴급 주문 %d드럼을 모두 실었다." % Defs.RUSH_DRUMS)
		rush_order = false
	if n <= 0 and income >= 0:
		_note("트럭이 빈 채로 떠났다. 창고에 드럼이 없다.")
		popup.emit("빈 트럭", "dock", BAD)
		return
	_earn(income)
	popup.emit("트럭 %d드럼  +%d" % [n, income], "dock", MONEY_IN)
	sound.emit("truck")
	_note("트럭이 세정액 %d드럼을 싣고 갔다 (+%d)." % [n, income])


func _pay(v: float, _why: String) -> void:
	money -= v
	shift_cost += v
	week_cost += v


func _earn(v: float) -> void:
	money += v
	shift_income += v
	week_income += v


func _note(s: String) -> void:
	shift_log.append("%s · %s" % [clock_text(), s])
	log_line.emit(s)


# ================================================================ 교대 끝

func _end_shift() -> void:
	shift_done = true
	var wages := people.filter(func(p: Person) -> bool: return not p.gone).size() * Defs.WAGE_PER_SHIFT
	_pay(wages + Defs.UPKEEP_PER_SHIFT, "월급·유지비")
	popup.emit("월급·유지비  −%d" % (wages + Defs.UPKEEP_PER_SHIFT), "office", MONEY_OUT)
	# 쉬는 사람 회복
	for p in people:
		if p.gone:
			continue
		if not p.on_site and p.arrive_at < 0:
			p.fatigue = maxf(0.0, p.fatigue - Defs.FATIGUE_REST_SHIFT)
			p.add_stress(-8.0)
			if not p.errand:
				p.contam = maxf(0.0, p.contam - 0.5)
	# 남은 위기는 넘어간다
	for c in crises.duplicate():
		if c["kind"] == "light":
			lit[c["room"]] = true
			var who: Person = c["who"]
			who.busy_until = -1
	crises.clear()
	if entity_room != "":
		entity_room = ""
	if is_night():
		anomaly_rooms_last_night = anomaly_rooms_tonight.duplicate()
		for id in lit:
			lit[id] = true  # 아침이면 정비가 불을 다 고친다


## 다음 교대로 넘긴다. 주간 결산이 있으면 그 내용을 돌려준다.
func advance_to_next_shift() -> Dictionary:
	var out := {}
	if shift == "day":
		shift = "night"
	else:
		if day % Defs.WEEK_DAYS == 0:
			out = {"week": day / Defs.WEEK_DAYS, "income": week_income, "cost": week_cost,
				"profit": week_income - week_cost, "violations": violations}
			week_income = 0.0
			week_cost = 0.0
		if day >= Defs.DEMO_LAST_DAY:
			finished = true
		day += 1
		shift = "day"
	t = 0
	return out


func shift_report() -> Dictionary:
	var changes: Array = []
	for p in people:
		if p.on_duty or p.extra_shifts > 0:
			var bits: Array = []
			if p.fatigue >= Defs.FATIGUE_SLOW:
				bits.append("피로 %d (능력 −20%%)" % p.fatigue)
			if p.stress >= 70:
				bits.append("스트레스 %d" % p.stress)
			if p.stage() >= 1:
				bits.append(["", "다크서클", "초점 없는 눈", "검은 무늬", "변이"][p.stage()])
			if p.broken:
				bits.append("붕괴")
			if not bits.is_empty():
				changes.append("%s: %s" % [p.name, ", ".join(bits)])
	return {"income": shift_income, "cost": shift_cost, "log": shift_log.duplicate(), "people": changes,
		"park": park_comment()}


# ================================================================ 연구

func _research_hour() -> void:
	var tgt := research_target
	var stages: Array = Defs.RESEARCH_STAGES[tgt]
	if research[tgt] >= stages.size():
		# 다 끝났으면 다른 쪽
		tgt = "pressure" if tgt == "extra" else "extra"
		stages = Defs.RESEARCH_STAGES[tgt]
		if research[tgt] >= stages.size():
			return
	if records <= 0:
		return
	if rng.randf() < 0.5:
		return  # 한 기록을 두 시간쯤 들여다본다
	records -= 1
	research_progress[tgt] += 1
	var need: int = stages[research[tgt]]
	if research_progress[tgt] >= need:
		research[tgt] += 1
		_radio_research_done(tgt, research[tgt])


func research_line() -> String:
	var tgt := research_target
	var stages: Array = Defs.RESEARCH_STAGES[tgt]
	if research[tgt] >= stages.size():
		return "%s 연구 끝" % Defs.RESEARCH_NAMES[tgt]
	return "%s %d단계: %d/%d (남은 관찰 기록 %d)" % [Defs.RESEARCH_NAMES[tgt], research[tgt] + 1,
		research_progress[tgt], stages[research[tgt]], records]


# ================================================================ 무전

func _new_radio(kind: String, speaker: String, text: String, room: String, choices: Array) -> Dictionary:
	var ev := {"kind": kind, "speaker": speaker, "text": text, "room": room, "choices": choices,
		"t": t, "clock": clock_text()}
	history.append({"day": day, "shift": shift, "t": t, "what": "radio", "kind": kind})
	radio_queue.append(ev)
	last_radio_t = t
	kinds_shift[kind] = kinds_shift.get(kind, 0) + 1
	radio_count += 1
	highlight.emit(room)
	sound.emit("radio")
	return ev


## choices 한 개: {"label", "pct" (−1이면 판정 없음), "why", "key", "disabled"}
func _choice(label: String, key: String, pct: float = -1.0, why: String = "", disabled: bool = false) -> Dictionary:
	return {"label": label, "key": key, "pct": clampf(pct, 5.0, 98.0) if pct >= 0.0 else -1.0, "why": why, "disabled": disabled}


func pop_radio() -> Dictionary:
	if radio_queue.is_empty():
		return {}
	return radio_queue.pop_front()


## 무전에 답한다. {"text", "tone": good/bad/perfect/info} 를 돌려준다.
func resolve(ev: Dictionary, idx: int) -> Dictionary:
	_last_choice = idx
	var c: Dictionary = ev["choices"][idx]
	var key: String = c["key"]
	var ok := true
	if c["pct"] >= 0.0:
		ok = rng.randf() * 100.0 < c["pct"]
	var fn: String = "_r_" + ev["kind"]
	var r: Dictionary = {"text": "", "tone": "info"}
	if has_method(fn):
		r = call(fn, ev, key, ok)
	# 보고서에 남긴다 (무전 → 고른 것 → 결과)
	if r["tone"] != "none" and r["text"] != "" and not ev["kind"] in ["tutorial", "research", "result"]:
		var first_line: String = r["text"].split("\n")[0]
		var core := first_line.trim_prefix("실패. ").trim_prefix("성공. ").substr(0, 12)
		for old in shift_log.slice(maxi(0, shift_log.size() - 3)):
			if core != "" and String(old).contains(core):
				shift_log.erase(old)
		var mark: String = {"good": "○", "bad": "×", "perfect": "★", "info": "·"}.get(r["tone"], "·")
		shift_log.append("%s %s %s: %s → %s" % [ev["clock"], mark, ev["speaker"], c["label"], first_line])
	return r


func _tone(ok: bool) -> String:
	return "good" if ok else "bad"


# ---------------------------------------------------------------- 역압

func _start_pressure() -> void:
	var ops := in_room("reactor").filter(func(q: Person) -> bool: return q.on_duty and q.working(now()))
	var op: Person = ops[0] if ops.size() > 0 else null
	var leader := duty_leader()
	var ev_text := "공장장님, 반응실 압력계 수치가 계속 올라갑니다! 그런데 배관 소리는 오히려 조용해요."
	var speaker := op.name if op != null else (leader.name if leader != null else "무전")
	if research["pressure"] >= 1:
		ev_text += "\n(연구 1단계: 계기가 이상하다. 숫자를 그대로 믿으면 안 된다)"
	ev_text += "\n\n%d분 안에 막지 못하면 터집니다." % Defs.PRESSURE_WINDOW
	var choices: Array = []
	if op != null:
		var pct := 30.0 + op.eff_skill() * 0.45 + (5.0 if op.has_trait("꼼꼼함") else 0.0)
		choices.append(_choice("%s이(가) 계기대로 감압한다" % op.name, "self", pct, "%s 숙련 %d" % [op.name, op.eff_skill()]))
		if research["pressure"] >= 3:
			var pct2 := 30.0 + op.eff_skill() * 0.65 + 15.0
			choices.append(_choice("수칙: 빨간색이면 반대쪽 밸브", "valve", pct2, "연구로 찾은 방법. 숙련 %d" % op.eff_skill()))
	if leader != null and leader.room != "reactor":
		var w := walk_minutes(leader, "reactor")
		var pct3 := 30.0 + leader.eff_skill() * 0.65 + (5.0 if leader.has_trait("꼼꼼함") else 0.0)
		choices.append(_choice("%s이(가) 직접 가서 막는다 (걸어서 %d분)" % [leader.name, w], "leader", pct3,
			"%s 숙련 %d. 도착 전에 터지면 실패" % [leader.name, leader.eff_skill()]))
	var vets := callable_people()
	vets.sort_custom(func(a: Person, b: Person) -> bool: return a.skill > b.skill)
	if not vets.is_empty():
		var v: Person = vets[0]
		var eta := Defs.CALL_TRAVEL_MIN + walk_minutes_from_gate("reactor")
		choices.append(_choice("숙련자 %s 호출 (%d~%d분 뒤 도착)" % [v.name, eta - Defs.CALL_TRAVEL_SPREAD, eta + Defs.CALL_TRAVEL_SPREAD], "call",
			30.0 + v.eff_skill() * 0.65, "%s 숙련 %d. 남은 시간 %d분 — 늦으면 터진다. 위반 기록 1, 수당 %d" % [v.name, v.skill, Defs.PRESSURE_WINDOW, Defs.CALL_PAY]))
	var stop_why := "확실하지만 반응실이 3시간 멈춘다"
	if leader != null and leader.has_trait("안전 제일"):
		stop_why = "윤 반장이 먼저 권한다: \"세웁시다.\" 3시간 정지"
	choices.append(_choice("반응기를 세운다", "stop", -1.0, stop_why))
	var ev := _new_radio("pressure", speaker, ev_text, "reactor", choices)
	crises.append({"kind": "pressure", "room": "reactor", "left": Defs.PRESSURE_WINDOW, "ev": ev})
	sound.emit("pressure")
	anomaly_rooms_tonight.append("reactor")
	_add_records_witness(in_room("reactor"))


func walk_minutes_from_gate(to: String) -> int:
	var pts := Defs.path_to(Defs.room_center("gate"), "gate", to)
	var d := 0.0
	var cur := Defs.room_center("gate")
	for q in pts:
		d += cur.distance_to(q)
		cur = q
	return int(ceil(d / Defs.WALK_PX_PER_MIN))


func _pressure_crisis() -> Dictionary:
	for c in crises:
		if c["kind"] == "pressure":
			return c
	return {}


func _end_pressure() -> void:
	var c := _pressure_crisis()
	if not c.is_empty():
		crises.erase(c)


func _pressure_success(who: Person, text: String) -> Dictionary:
	_end_pressure()
	stats["pressure_ok"] += 1
	who.add_stress(-10.0)
	if rng.randf() < Defs.PERFECT_P:
		perfect_shift = true
		stats["perfect"] += 1
		records += 2
		who.add_stress(-10.0)
		popup.emit("★ 완벽한 차단", "reactor", GOLD)
		sound.emit("perfect")
		_note("★ %s의 완벽한 차단. 이번 교대는 역압이 오지 않는다. 관찰 기록 +2." % who.name)
		return {"text": "★ 완벽한 차단!\n%s\n이번 교대 동안 역압이 오지 않는다. 관찰 기록 +2." % text, "tone": "perfect"}
	popup.emit("차단 성공", "reactor", GOOD)
	sound.emit("ok")
	_note("%s이(가) 역압을 막았다." % who.name)
	return {"text": "성공. " + text, "tone": "good"}


func _pressure_fail(who: Person) -> Dictionary:
	_end_pressure()
	return _pressure_blow(who)


func _pressure_blow(who: Person = null) -> Dictionary:
	_end_pressure()
	stats["pressure_bad"] += 1
	reactor_down_until = now() + 240
	_pay(Defs.REPAIR_COST, "수리")
	popup.emit("역압 사고  −%d" % Defs.REPAIR_COST, "reactor", BAD)
	sound.emit("fail")
	var hurt := ""
	for p in in_room("reactor"):
		p.add_stress(20.0)
		if day > Defs.PROLOGUE_DAYS and rng.randf() < 0.2:
			p.injured_until_day = day + 2
			hurt = p.name
			_go_home(p)
	var s := "반응실 배관이 터졌다. 수리비 −%d, 반응실 4시간 정지." % Defs.REPAIR_COST
	if hurt != "":
		s += " %s이(가) 다쳐 이틀 쉰다." % hurt
	if who != null:
		s = "%s이(가) 막지 못했다. " % who.name + s
	_note(s)
	return {"text": "실패. " + s, "tone": "bad"}


func _r_pressure(ev: Dictionary, key: String, ok: bool) -> Dictionary:
	if _pressure_crisis().is_empty():
		return {"text": "이미 끝난 일이다.", "tone": "info"}
	var ops := in_room("reactor").filter(func(q: Person) -> bool: return q.on_duty)
	match key:
		"self", "valve":
			var op: Person = ops[0] if ops.size() > 0 else duty_leader()
			if ok:
				return _pressure_success(op, "%s이(가) 압력을 잡았다." % op.name)
			return _pressure_fail(op)
		"stop":
			_end_pressure()
			reactor_down_until = now() + 180
			popup.emit("반응기 정지", "reactor", GOOD)
			_note("반응기를 세웠다. 3시간 동안 생산이 멈춘다.")
			return {"text": "반응기를 세웠다. 사고는 없지만 3시간 동안 생산이 멈춘다.", "tone": "info"}
		"leader":
			var l := duty_leader()
			var pct: float = ev["choices"].filter(func(c: Dictionary) -> bool: return c["key"] == "leader")[0]["pct"]
			_send(l, "reactor", _leader_at_pressure.bind(l, pct))
			_note("%s이(가) 반응실로 뛰어간다." % l.name)
			return {"text": "%s: \"갑니다!\" (도착하면 판정. 남은 시간은 반응실 위 막대)" % l.name, "tone": "info"}
		"call":
			var vets := callable_people()
			vets.sort_custom(func(a: Person, b: Person) -> bool: return a.skill > b.skill)
			var v: Person = vets[0]
			_call_in(v, "역압", Defs.CALL_TRAVEL_MIN, "reactor")
			var pct: float = ev["choices"].filter(func(c: Dictionary) -> bool: return c["key"] == "call")[0]["pct"]
			v.on_arrive = Callable()
			_pending_call_pressure = {"who": v, "pct": pct}
			return {"text": "%s: \"지금 출발합니다.\" (정문을 지나면 박 씨가 알려 준다)" % v.name, "tone": "info"}
	return {"text": "", "tone": "info"}


var _pending_call_pressure := {}


func _leader_at_pressure(l: Person, pct: float) -> void:
	history.append({"day": day, "shift": shift, "t": t, "what": "arrive", "kind": "pressure_leader", "left": _pressure_crisis().get("left", -1)})
	if _pressure_crisis().is_empty():
		_send(l, "control", Callable())
		return
	var r: Dictionary
	if rng.randf() * 100.0 < pct:
		r = _pressure_success(l, "%s이(가) 도착해 밸브를 잡았다." % l.name)
	else:
		r = _pressure_fail(l)
	_result_radio(l.name, r)
	_send(l, "control", Callable())


## 판정 결과를 무전으로 알린다 (사람이 도착한 뒤 판정할 때)
func _result_radio(speaker: String, r: Dictionary) -> void:
	var ev := _new_radio("result", speaker, r["text"], "", [_choice("확인", "ok")])
	ev["tone"] = r["tone"]


func _r_result(_ev: Dictionary, _key: String, _ok: bool) -> Dictionary:
	return {"text": "", "tone": "none"}


# ---------------------------------------------------------------- 한 명 더

func _spawn_entity(id: String) -> void:
	entity_room = id
	entity_since = now()
	entity_reported = false
	anomaly_rooms_tonight.append(id)
	if research["extra"] >= 1 or owned.has("cctv"):
		_note("[인원 대조] %s: 명단보다 한 명 많다." % Defs.ROOMS[id]["name"])
		popup.emit("인원 불일치", id, BAD)
		sound.emit("alert")


func _tick_entity() -> void:
	if entity_room == "":
		return
	var ppl := in_room(entity_room).filter(func(q: Person) -> bool: return q.on_duty)
	# 사람이 둘 이상 모이면 그것은 사라진다 (돌보기가 성공했을 때와 다르게, 적발은 아니다)
	if ppl.is_empty():
		entity_room = ""
		return
	if ppl.size() >= 2:
		var id := entity_room
		_detected(ppl, id)
		return
	var elapsed := now() - entity_since
	for p in ppl:
		if elapsed % 30 == 0 and elapsed > 0:
			p.add_contam(3.0)
	if not entity_reported and elapsed >= Defs.EXTRA_REPORT_DELAY:
		entity_reported = true
		_radio_extra(ppl[0])


func _radio_extra(p: Person) -> void:
	var id := entity_room
	var rname: String = Defs.ROOMS[id]["name"]
	var text := "%s에 모르는 사람이 있어요. 아까부터 같이 일하고 있었는데... 명단에 없는 얼굴이에요." % rname
	if p.has_trait("호기심 많음"):
		text += "\n(정 사원은 벌써 그쪽으로 다가가고 있다)"
	var choices: Array = []
	var pct := 20.0 + p.eff_observe() * 0.8 + (15.0 if research["extra"] >= 3 and lit[id] else 0.0)
	choices.append(_choice("불 켜고 인원을 세라", "count", pct, "%s 관찰 %d%s" % [p.name, p.eff_observe(), "" if lit[id] else " / 불이 꺼져 있어 못 한다"], not lit[id]))
	var helper := _nearest_helper(id, p)
	if helper != null:
		var w := walk_minutes(helper, id)
		var pct2 := minf(95.0, 25.0 + (p.eff_observe() + helper.eff_observe()) * 0.55)
		choices.append(_choice("%s을(를) 보내 둘이 확인한다 (%d분)" % [helper.name, w], "pair", pct2,
			"관찰 %d + %d. 그 사이 %s 자리가 빈다" % [p.eff_observe(), helper.eff_observe(), Defs.POST_NAMES.get(helper.post, helper.post)]))
	var off := callable_people()
	if not off.is_empty():
		var v: Person = off[0]
		choices.append(_choice("휴무 조 %s 호출해 같이 확인 (약 1시간)" % v.name, "call",
			minf(95.0, 25.0 + (p.eff_observe() + v.eff_observe()) * 0.55), "자리는 안 비지만 늦다. 위반 기록 1, 수당 %d" % Defs.CALL_PAY))
	choices.append(_choice("당장 그 방에서 나와라", "leave", -1.0, "확실히 피하지만 %s 일이 멈춘다" % rname))
	var warn := "" if p.ignored == 0 else " (경고: %s은(는) 벌써 %d번 무시했다)" % [p.name, p.ignored]
	choices.append(_choice("신경 쓰지 말고 일해라", "ignore", -1.0, "아무 일도 안 하면 오염이 쌓인다%s" % warn))
	var ev := _new_radio("extra", p.name, text, id, choices)
	ev["who"] = p
	sound.emit("alert")


func _nearest_helper(id: String, not_p: Person) -> Person:
	var best: Person = null
	var bd := 1e9
	for q in people:
		if q == not_p or not q.on_duty or not q.on_site or q.broken or q.is_staff():
			continue
		if q.busy_until > now() or (q == errand_person and errand_state == "away"):
			continue
		if q.room == id:
			continue
		var d := walk_minutes(q, id)
		if q.post == "patrol":
			d -= 15  # 순찰은 원래 돌아다니는 사람
		if d < bd:
			bd = d
			best = q
	return best


func _detected(who: Array, id: String) -> Dictionary:
	entity_room = ""
	stats["detected"] += 1
	records += 2
	for p in who:
		var person: Person = p
		person.add_stress(-8.0)
	popup.emit("적발!", id, GOOD)
	sound.emit("ok")
	var names := ", ".join(who.map(func(p: Person) -> String: return p.name))
	_note("%s에서 '한 명 더'를 적발했다 (%s). 관찰 기록 +2." % [Defs.ROOMS[id]["name"], names])
	return {"text": "적발! 다시 세어 보니 한 명이 비었다. 아무도 나가는 걸 보지 못했다.\n관찰 기록 +2", "tone": "good"}


func _missed(who: Array, id: String) -> Dictionary:
	for p in who:
		var person: Person = p
		person.add_stress(15.0)
		person.add_contam(10.0)
	popup.emit("놓침", id, BAD)
	sound.emit("bad")
	var names := ", ".join(who.map(func(p: Person) -> String: return p.name))
	_note("%s에서 '한 명 더'를 놓쳤다. %s 스트레스·오염 상승." % [Defs.ROOMS[id]["name"], names])
	entity_room = ""
	return {"text": "숫자가 맞았다. 그런데 누가 누군지 모르겠다. 확인하던 사람이 식은땀을 흘린다.\n스트레스 +15, 오염 +10", "tone": "bad"}


func _r_extra(ev: Dictionary, key: String, ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	var id: String = ev["room"]
	if entity_room != id:
		return {"text": "다시 보니 아무도 없다.", "tone": "info"}
	match key:
		"count":
			return _detected([p], id) if ok else _missed([p], id)
		"pair":
			var helper := _nearest_helper(id, p)
			var pct: float = ev["choices"].filter(func(c: Dictionary) -> bool: return c["key"] == "pair")[0]["pct"]
			_send(helper, id, _pair_arrived.bind(helper, p, id, pct))
			return {"text": "%s이(가) %s(으)로 간다. 도착하면 둘이 확인한다." % [helper.name, Defs.ROOMS[id]["name"]], "tone": "info"}
		"call":
			var v: Person = callable_people()[0]
			var pct: float = ev["choices"].filter(func(c: Dictionary) -> bool: return c["key"] == "call")[0]["pct"]
			_call_in(v, "한 명 더 확인", Defs.CALL_TRAVEL_MIN, id)
			_pending_pair_call = {"who": v, "with": p, "room": id, "pct": pct}
			return {"text": "%s: \"...지금요? 알겠습니다.\" (정문을 지나면 박 씨가 알려 준다)" % v.name, "tone": "info"}
		"leave":
			entity_room = ""
			p.add_stress(5.0)
			p.busy_until = now() + 60
			p.busy_what = "피해서 쉬는 중"
			_send(p, "control", Callable())
			crises.append({"kind": "rest", "room": "control", "left": 60, "who": p})
			_note("%s이(가) %s에서 나왔다. 1시간 동안 그 자리가 빈다." % [p.name, Defs.ROOMS[id]["name"]])
			return {"text": "%s이(가) 조정실로 피했다. 1시간 뒤 돌아간다." % p.name, "tone": "info"}
		"ignore":
			p.ignored += 1
			p.add_contam(15.0)
			p.add_stress(10.0)
			var s := "%s은(는) 하던 일을 계속했다. 오염 +15." % p.name
			if p.ignored >= 3 and day > Defs.PROTECT_DAY:
				_vanish(p)
				return {"text": "아침에 %s의 자리는 비어 있었다. 출입 기록에는 나간 기록이 없다." % p.name, "tone": "bad"}
			if p.ignored >= 2:
				s += "\n경고: 한 번 더 무시하면 무슨 일이 생길지 모른다."
			entity_room = ""
			_note(s)
			return {"text": s, "tone": "bad"}
	return {"text": "", "tone": "info"}


var _pending_pair_call := {}


func _pair_arrived(helper: Person, p: Person, id: String, pct: float) -> void:
	history.append({"day": day, "shift": shift, "t": t, "what": "arrive", "kind": "pair", "still": entity_room == id})
	var r: Dictionary
	if entity_room != id:
		r = {"text": "%s이(가) 도착했을 땐 이미 아무도 없었다." % helper.name, "tone": "info"}
	elif rng.randf() * 100.0 < pct:
		r = _detected([p, helper], id)
	else:
		r = _missed([p, helper], id)
	_result_radio(helper.name, r)
	if helper.on_duty and helper.post_room() != id:
		_send(helper, helper.post_room() if helper.post != "patrol" else "control", Callable())


func _vanish(p: Person) -> void:
	p.gone = true
	p.on_site = false
	p.on_duty = false
	stats["vanished"] += 1
	entity_room = ""
	for q in people:
		if q.crew == p.crew and not q.gone:
			q.add_stress(30.0)
	sound.emit("vanish")
	_note("%s이(가) 사라졌다." % p.name)


# ---------------------------------------------------------------- 정전

func _outage() -> void:
	var cand := ["reactor", "refine", "store", "tank", "dock"]
	cand.shuffle()
	var n := 1 if rng.randf() < 0.6 else 2
	var rooms: Array = []
	for id in cand:
		if lit[id] and not owned.has("em_" + id):
			lit[id] = false
			rooms.append(id)
		if rooms.size() >= n:
			break
	if rooms.is_empty():
		_note("정전. 비상 조명 덕분에 불은 꺼지지 않았다.")
		popup.emit("정전 (비상 조명 유지)", "control", GOOD)
		return
	var names := ", ".join(rooms.map(func(id: String) -> String: return Defs.ROOMS[id]["name"]))
	var l := duty_leader()
	var choices: Array = []
	var patrol: Person = null
	for q in people:
		if q.post == "patrol" and q.on_duty and q.on_site:
			patrol = q
	if patrol != null:
		choices.append(_choice("순찰 %s이(가) 돌며 켠다" % patrol.name, "patrol", -1.0, "방마다 %d분. 그동안 순찰이 빈다" % Defs.FIX_LIGHT_MIN))
	var mech: Person = null
	for q in people:
		if q.role == "정비원" and not q.gone:
			mech = q
	if mech != null and mech.on_site and mech.on_duty:
		choices.append(_choice("정비원 %s을(를) 보낸다" % mech.name, "mech", -1.0, "낮이라 바로 간다"))
	elif mech != null and available(mech):
		choices.append(_choice("정비원 %s 호출 (약 %d분)" % [mech.name, Defs.CALL_TRAVEL_MIN + 20], "callmech", -1.0, "도착하면 한 번에 다 고친다. 위반 기록 1, 수당 %d" % Defs.CALL_PAY))
	choices.append(_choice("그대로 둔다", "leave", -1.0, "불 꺼진 방은 작업 −40%, '한 명 더'가 잘 생긴다"))
	var speaker := l.name if l != null else "무전"
	_new_radio("outage", speaker, "정전입니다! %s 불이 나갔어요." % names, rooms[0] if rooms.size() > 0 else "", choices)["rooms"] = rooms
	sound.emit("outage")
	_note("정전. %s 불이 꺼졌다." % names)


func _r_outage(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var rooms: Array = ev.get("rooms", [])
	match key:
		"patrol":
			for q in people:
				if q.post == "patrol" and q.on_duty and q.on_site:
					if rooms.size() > 0:
						_send(q, rooms[0], Callable())
			return {"text": "순찰이 꺼진 방으로 간다. 도착하면 하나씩 켠다.", "tone": "info"}
		"mech":
			for q in people:
				if q.role == "정비원":
					_send(q, rooms[0], _mech_fix.bind(q, rooms))
			return {"text": "정비원이 간다.", "tone": "info"}
		"callmech":
			for q in people:
				if q.role == "정비원":
					_call_in(q, "정전", Defs.CALL_TRAVEL_MIN, "maint")
					_pending_mech = {"who": q, "rooms": rooms}
			return {"text": "정비원이 집에서 출발했다.", "tone": "info"}
	return {"text": "불 꺼진 채로 둔다.", "tone": "info"}


var _pending_mech := {}


func _mech_fix(q: Person, rooms: Array) -> void:
	for id in rooms:
		lit[id] = true
	popup.emit("불 켜짐", rooms[0], GOOD)
	sound.emit("light")
	_note("%s이(가) 불을 고쳤다." % q.name)
	if is_night():
		_go_home(q)
	else:
		_send(q, "maint", Callable())


## 호출한 사람이 정문을 지나면 박 씨가 알려 준다
func _park_says_arrival(p: Person) -> void:
	popup.emit("%s 정문 통과" % p.name, "gate", Color(0.8, 0.85, 1.0))
	log_line.emit("박 씨: \"%s 방금 정문 통과했어요.\"" % p.name)
	if not _pending_call_pressure.is_empty() and _pending_call_pressure["who"] == p:
		var pct: float = _pending_call_pressure["pct"]
		_pending_call_pressure = {}
		_send(p, "reactor", _vet_at_pressure.bind(p, pct))
	elif not _pending_pair_call.is_empty() and _pending_pair_call["who"] == p:
		var d := _pending_pair_call
		_pending_pair_call = {}
		_send(p, d["room"], _pair_arrived.bind(p, d["with"], d["room"], d["pct"]))
	elif not _pending_mech.is_empty() and _pending_mech["who"] == p:
		var d2 := _pending_mech
		_pending_mech = {}
		_send(p, d2["rooms"][0], _mech_fix.bind(p, d2["rooms"]))


func _vet_at_pressure(v: Person, pct: float) -> void:
	history.append({"day": day, "shift": shift, "t": t, "what": "arrive", "kind": "pressure_call", "left": _pressure_crisis().get("left", -1)})
	if _pressure_crisis().is_empty():
		_note("%s이(가) 도착했지만 이미 끝난 뒤였다." % v.name)
		return
	var r: Dictionary
	if rng.randf() * 100.0 < pct:
		r = _pressure_success(v, "%s이(가) 도착해 압력을 잡았다." % v.name)
	else:
		r = _pressure_fail(v)
	_result_radio(v.name, r)


# ---------------------------------------------------------------- 잔잔한 무전 (교대마다 2번쯤)

func _filler_event() -> void:
	var opts: Array = []
	var tired := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.fatigue >= 45 and not q.walking() and q.busy_until <= now() and q.role != "반장")
	if not tired.is_empty():
		opts.append("drowsy")
	var smokers := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.has_trait("줄담배") and q.working(now()))
	if not smokers.is_empty():
		opts.append("smoke")
	if _crew_pair().size() == 2:
		opts.append("quarrel")
	if is_night():
		opts += ["noise", "phone", "door", "cold", "headcount"]
		if day > 1 and kinds_shift.get("park", 0) == 0:
			opts.append("park")
	else:
		opts += ["visitor", "filter", "supply"]
		if day > 1:
			opts.append("complaint")
	# 한 교대에 같은 종류는 한 번, 바로 앞과 같은 것은 피한다
	opts = opts.filter(func(k: String) -> bool: return kinds_shift.get(k, 0) == 0 and k != last_filler)
	if opts.is_empty():
		return
	# 말다툼·추위 같은 일상은 덜 자주
	var weights := {"quarrel": 0.4, "cold": 0.6, "supply": 0.5, "smoke": 0.7}
	var total := 0.0
	for o in opts:
		total += weights.get(o, 1.0)
	var roll := rng.randf() * total
	var k: String = opts[0]
	for o in opts:
		roll -= weights.get(o, 1.0)
		if roll <= 0.0:
			k = o
			break
	last_filler = k
	match k:
		"drowsy":
			_radio_drowsy(tired[rng.randi() % tired.size()])
		"smoke":
			_radio_smoke(smokers[0])
		"quarrel":
			_radio_quarrel()
		"noise":
			_radio_noise()
		"phone":
			_radio_phone()
		"door":
			_radio_door()
		"cold":
			_radio_cold()
		"headcount":
			_radio_headcount()
		"park":
			_radio_park()
		"visitor":
			_radio_visitor()
		"filter":
			_radio_filter()
		"supply":
			_radio_supply()
		"complaint":
			_radio_complaint()


## 아직 안 쓴 문구 하나를 고른다. 다 썼으면 처음부터.
func _pick_text(key: String, pool: Array) -> String:
	var left := pool.filter(func(x: String) -> bool: return not used_text.has(key + x))
	if left.is_empty():
		for x in pool:
			used_text.erase(key + x)
		left = pool
	var c: String = left[rng.randi() % left.size()]
	used_text[key + c] = true
	return c


func _crew_pair() -> Array:
	var ppl := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.role == "조원" and q.working(now()))
	if ppl.size() < 2:
		return []
	ppl.shuffle()
	return [ppl[0], ppl[1]]


func _radio_smoke(p: Person) -> void:
	var text := _pick_text("smoke", [
		"%s: \"공장장님, 담배 한 대만 피우고 오면 안 될까요? 손이 떨려서요.\"",
		"%s: \"10분만요. 진짜 10분.\" 벌써 라이터를 꺼내 들었다.",
		"%s이(가) 흡연 구역 쪽을 자꾸 쳐다본다. 반장이 어떻게 할지 묻는다.",
	]) % p.name
	var choices := [
		_choice("다녀오게 한다", "go", -1.0, "스트레스 −10, 20분 자리가 빈다"),
		_choice("교대 끝나고 피우라고 한다", "no", -1.0, "스트레스 +6"),
	]
	_new_radio("smoke", p.name, text, p.room, choices)["who"] = p


func _r_smoke(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	if key == "go":
		p.add_stress(-10.0)
		p.busy_until = now() + 20
		p.busy_what = "담배 피우는 중"
		return {"text": "%s이(가) 밖으로 나갔다. 20분 뒤 돌아온다." % p.name, "tone": "info"}
	p.add_stress(6.0)
	return {"text": "%s이(가) 한숨을 쉬고 다시 일한다." % p.name, "tone": "info"}


func _radio_quarrel() -> void:
	var pr := _crew_pair()
	var a: Person = pr[0]
	var b: Person = pr[1]
	var l := duty_leader()
	var text := _pick_text("quarrel", [
		"%s랑 %s이(가) 무전으로 싸워요. 누가 드럼 순서를 바꿨다고요.",
		"%s이(가) %s한테 \"너 아까 거기 없었잖아\"라고 해요. %s은(는) 계속 있었다고 하고요.",
		"%s, %s 둘이 교대 인수인계 때문에 언성이 높아졌어요.",
	])
	text = text.replace("%s", "{}")
	text = text.format([a.name, b.name, b.name], "{}")
	var choices: Array = []
	if l != null:
		var pct := 30.0 + l.nerve * 0.6 - (15.0 if l.has_trait("신참 반장") else 0.0)
		choices.append(_choice("%s이(가) 말린다" % l.name, "leader", pct, "%s 담력 %d%s" % [l.name, l.nerve, " · 신참 반장 −15" if l.has_trait("신참 반장") else ""]))
	choices.append(_choice("그냥 둔다", "leave", -1.0, "둘 다 스트레스 +10"))
	var ev := _new_radio("quarrel", l.name if l != null else "무전", text, a.room, choices)
	ev["a"] = a
	ev["b"] = b


func _r_quarrel(ev: Dictionary, key: String, ok: bool) -> Dictionary:
	var a: Person = ev["a"]
	var b: Person = ev["b"]
	if key == "leader" and ok:
		a.add_stress(-5.0)
		b.add_stress(-5.0)
		return {"text": "반장이 둘을 진정시켰다. 둘 다 조금 풀렸다.", "tone": "good"}
	a.add_stress(10.0)
	b.add_stress(10.0)
	return {"text": "말다툼은 교대 끝까지 이어졌다. 둘 다 스트레스 +10.", "tone": "bad" if key == "leader" else "info"}


func _radio_phone() -> void:
	var l := duty_leader()
	if l == null:
		return
	var text := _pick_text("phone", [
		"조정실 내선이 울려요. 받으면 아무 말도 없고, 기계 소리만 들려요.",
		"내선 번호 0번에서 전화가 와요. 0번은 없는 번호인데요.",
		"전화를 받았는데... 제 목소리가 들려요. 몇 초 늦게.",
	])
	var pct := 20.0 + l.eff_observe() * 0.6
	var choices := [
		_choice("끝까지 들어 본다", "listen", pct, "%s 관찰 %d. 성공하면 관찰 기록 +1, 실패하면 오염 +6" % [l.name, l.eff_observe()]),
		_choice("받지 말고 선을 뽑아라", "pull", -1.0, "스트레스 +3"),
	]
	_new_radio("phone", l.name, text, "control", choices)["who"] = l


func _r_phone(ev: Dictionary, key: String, ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	if key == "pull":
		p.add_stress(3.0)
		return {"text": "선을 뽑았다. 그 뒤로 전화는 울리지 않았다. 울리면 안 되는 거니까.", "tone": "info"}
	if ok:
		records += 1
		return {"text": "기계 소리 사이로 숫자를 세는 목소리가 들렸다. 하나, 둘, 셋, 넷, 다섯, 여섯. 오늘 야간조는 다섯 명이다. 관찰 기록 +1.", "tone": "good"}
	p.add_contam(6.0)
	p.add_stress(8.0)
	return {"text": "%s이(가) 수화기를 한참 들고 있었다. 내려놓은 뒤 무엇을 들었는지 기억하지 못한다. 오염 +6." % p.name, "tone": "bad"}


func _radio_door() -> void:
	var where: String = ["창고", "출하장", "원료 탱크"][rng.randi() % 3]
	var id: String = {"창고": "store", "출하장": "dock", "원료 탱크": "tank"}[where]
	var patrol: Person = null
	for q in people:
		if q.post == "patrol" and q.on_duty and q.on_site and not q.walking():
			patrol = q
	var l := duty_leader()
	var text := "%s 비상문이 열려 있다고 CCTV에 떠요. 아까 분명히 닫혀 있었는데." % where
	var choices: Array = []
	if patrol != null:
		choices.append(_choice("순찰 %s이(가) 가서 닫는다 (%d분)" % [patrol.name, walk_minutes(patrol, id)], "patrol", -1.0, "순찰이 그쪽으로 간다"))
	choices.append(_choice("그냥 둔다", "leave", -1.0, "오늘 밤 그 방에 '한 명 더'가 생길 확률이 오른다"))
	var ev := _new_radio("door", l.name if l != null else "무전", text, id, choices)
	ev["patrol"] = patrol


func _r_door(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var id: String = ev["room"]
	if key == "patrol":
		var p: Person = ev["patrol"]
		_send(p, id, _door_closed.bind(p, id))
		return {"text": "%s이(가) %s(으)로 간다." % [p.name, Defs.ROOMS[id]["name"]], "tone": "info"}
	door_open[id] = true
	return {"text": "문은 열린 채로 있다.", "tone": "info"}


var door_open := {}


func _door_closed(p: Person, id: String) -> void:
	door_open.erase(id)
	if rng.randf() < 0.3:
		records += 1
		_result_radio(p.name, {"text": "문을 닫았다. 문틀 안쪽에 젖은 손자국이 있다. 안에서 민 자국이다. 관찰 기록 +1.", "tone": "good"})
	else:
		_note("%s이(가) %s 비상문을 닫았다." % [p.name, Defs.ROOMS[id]["name"]])


func _radio_cold() -> void:
	var ppl := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.working(now()) and q.room in ["store", "tank", "dock"])
	if ppl.is_empty():
		return
	var p: Person = ppl[0]
	var text := _pick_text("cold", [
		"%s: \"창고가 너무 추워요. 입김이 나와요. 여름인데.\"",
		"%s: \"여기 온도계가 영하를 가리켜요. 손가락이 안 움직여요.\"",
	]) % p.name
	var choices := [
		_choice("난로를 켜 준다", "heat", -1.0, "−20. 피로 −8"),
		_choice("참으라고 한다", "no", -1.0, "피로 +8, 스트레스 +5"),
	]
	_new_radio("cold", p.name, text, p.room, choices)["who"] = p


func _r_cold(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	if key == "heat":
		_pay(20, "난로")
		p.fatigue = maxf(0.0, p.fatigue - 8.0)
		return {"text": "난로를 켰다. 한참 뒤에야 따뜻해졌다. (−20)", "tone": "info"}
	p.fatigue += 8.0
	p.add_stress(5.0)
	return {"text": "%s은(는) 장갑을 두 겹 끼고 버틴다." % p.name, "tone": "info"}


func _radio_headcount() -> void:
	var l := duty_leader()
	if l == null:
		return
	var text := "정기 인원 점검 시간입니다. 무전으로 다 이름 대게 할까요? 2분이면 됩니다."
	var choices := [
		_choice("점검한다", "yes", -1.0, "'한 명 더'가 있으면 무전 대답이 하나 많다"),
		_choice("건너뛴다", "no", -1.0, ""),
	]
	_new_radio("headcount", l.name, text, "control", choices)


func _r_headcount(_ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "no":
		return {"text": "점검을 건너뛰었다.", "tone": "info"}
	if entity_room != "":
		var id := entity_room
		var who := in_room(id).filter(func(q: Person) -> bool: return q.on_duty)
		var r := _detected(who, id)
		r["text"] = "\"%s입니다.\" ... 그리고 같은 방에서 한 번 더, 같은 목소리로 같은 이름이 들렸다.\n" % (who[0].name if who.size() > 0 else "...") + r["text"]
		return r
	var n := on_site_people().filter(func(q: Person) -> bool: return q.on_duty).size()
	return {"text": "%d명 모두 대답했다. 숫자가 맞다." % n, "tone": "good"}


func _radio_filter() -> void:
	var m: Person = null
	for q in people:
		if q.role == "정비원" and q.on_site and q.on_duty:
			m = q
	if m == null:
		return
	var text := "정제실 필터가 막혀 가요. 지금 30분 세우고 갈면 괜찮고, 그냥 두면 오늘 내내 느려질 거예요."
	var choices := [
		_choice("지금 세우고 간다", "fix", -1.0, "정제실 30분 정지"),
		_choice("그냥 돌린다", "no", -1.0, "오늘 정제 −30%"),
	]
	_new_radio("filter", m.name, text, "refine", choices)


var refine_slow_until := -1


func _r_filter(_ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "fix":
		for p in in_room("refine"):
			if p.on_duty:
				p.busy_until = now() + 30
				p.busy_what = "필터 교체 중"
		return {"text": "필터를 갈았다.", "tone": "good"}
	refine_slow_until = now() + (Defs.SHIFT_MIN - t)
	return {"text": "정제실이 오늘 내내 느리다.", "tone": "info"}


func _radio_supply() -> void:
	var off: Person = null
	for q in people:
		if q.role == "관리" and q.on_site:
			off = q
	if off == null:
		return
	var text := "방독 마스크랑 장갑 재고가 거의 없어요. 지금 주문할까요? 60이요."
	var choices := [
		_choice("주문한다", "buy", -1.0, "−60. 이번 주 오염이 덜 쌓인다 (×0.7)"),
		_choice("다음 주에", "no", -1.0, ""),
	]
	_new_radio("supply", off.name, text, "office", choices)


var mask_until_day := 0


func _r_supply(_ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "buy":
		_pay(60, "보호구")
		mask_until_day = day + 7
		return {"text": "보호구를 주문했다. (−60)", "tone": "info"}
	return {"text": "미뤘다.", "tone": "info"}


func _radio_drowsy(p: Person) -> void:
	var l := duty_leader()
	var speaker := l.name if l != null and l != p else "무전"
	var text := "%s이(가) 꾸벅꾸벅 졸고 있어요. 피로 %d." % [p.name, p.fatigue]
	if p.fatigue >= Defs.FATIGUE_SLOW:
		text += " 벌써 손이 느려졌습니다."
	var choices := [
		_choice("깨워서 계속 일하게 한다", "wake", -1.0, "생산은 그대로, 스트레스 +8"),
		_choice("휴게실에서 1시간 쉬게 한다", "rest", -1.0, "피로 −15, 그 자리가 1시간 빈다"),
		_choice("반장이 커피를 돌린다", "coffee", -1.0, "−10. 다 같이 피로 −5"),
	]
	_new_radio("drowsy", speaker, text, p.room, choices)["who"] = p


func _r_drowsy(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	match key:
		"wake":
			p.add_stress(8.0)
			return {"text": "%s이(가) 얼굴을 비비며 다시 일한다." % p.name, "tone": "info"}
		"rest":
			p.busy_until = now() + 60
			p.busy_what = "휴게실에서 쉬는 중"
			_send(p, "rest", Callable())
			crises.append({"kind": "rest", "room": "rest", "left": 60 + walk_minutes(p, "rest"), "who": p})
			p.fatigue = maxf(0.0, p.fatigue - 15.0)
			_note("%s을(를) 1시간 쉬게 했다." % p.name)
			return {"text": "%s이(가) 휴게실로 간다. 1시간 뒤 돌아온다." % p.name, "tone": "info"}
		"coffee":
			_pay(10, "커피")
			for q in on_site_people():
				if q.on_duty:
					q.fatigue = maxf(0.0, q.fatigue - 5.0)
					q.add_stress(-2.0)
			return {"text": "자판기 커피가 돈다. 다들 조금 살아났다. (−10)", "tone": "good"}
	return {"text": "", "tone": "info"}


func _radio_noise() -> void:
	var ppl := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and not q.walking() and q.role != "반장")
	if ppl.is_empty():
		return
	var p: Person = ppl[rng.randi() % ppl.size()]
	var where: String = ["창고", "원료 탱크", "출하장"][rng.randi() % 3]
	var text := _pick_text("noise", [
		"%s 쪽에서 누가 제 이름을 불러요. 지금 거기 아무도 없을 텐데요.",
		"%s 쪽에서 우리 반장님 목소리가 들려요. 반장님은 조정실에 계시잖아요?",
		"%s에서 누가 노래를 불러요. 옛날 노래요. 가사를 모르는데 따라 부르고 싶어요.",
		"%s 쪽 스피커에서 제 이름이 방송돼요. 방송 설비는 꺼져 있는데.",
	]) % where
	var pct := 20.0 + p.eff_observe() * 0.6
	var choices := [
		_choice("가서 확인해 봐라", "check", pct, "성공하면 관찰 기록 +1. 실패하면 스트레스·오염"),
		_choice("대답하지 말고 무시해라", "ignore", -1.0, "스트레스 +3. 아무 일도 없다"),
	]
	var ev := _new_radio("noise", p.name, text, p.room, choices)
	ev["who"] = p


func _r_noise(ev: Dictionary, key: String, ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	if key == "ignore":
		p.add_stress(3.0)
		return {"text": "%s은(는) 대답하지 않았다. 부르는 소리가 곧 그쳤다." % p.name, "tone": "info"}
	if ok:
		records += 1
		_note("%s이(가) 이름을 부르는 소리를 확인했다. 관찰 기록 +1." % p.name)
		return {"text": "아무도 없었다. 바닥에 젖은 발자국이 문 앞에서 끊겨 있다. 관찰 기록 +1.", "tone": "good"}
	p.add_stress(12.0)
	p.add_contam(6.0)
	return {"text": "%s이(가) 무심코 \"네?\" 하고 대답했다. 돌아왔지만 한동안 말이 없다. 스트레스 +12, 오염 +6." % p.name, "tone": "bad"}


func _radio_park() -> void:
	var ppl := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.role != "반장")
	if ppl.is_empty():
		return
	var p: Person = ppl[rng.randi() % ppl.size()]
	var truth := rng.randf() < 0.7
	var text := _pick_text("park", [
		"공장장님, 방금 %s 씨가 정문 쪽으로 걸어오던데요? 혼자서요.",
		"%s 씨 퇴근했어요? 방금 정문 밖으로 나가는 걸 봤는데. 인사도 안 하고.",
		"%s 씨가 경비실 창문을 두드리고 갔어요. 뭐라고 했는데 안 들렸어요.",
	]) % p.name
	var choices := [
		_choice("반장에게 확인시킨다", "check", -1.0, "박 씨 말이 맞으면 데려온다"),
		_choice("박 씨가 잘못 봤겠지", "ignore", -1.0, ""),
	]
	var ev := _new_radio("park", "박 씨", text, "gate", choices)
	ev["who"] = p
	ev["truth"] = truth


func _r_park(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var p: Person = ev["who"]
	var truth: bool = ev["truth"]
	if truth:
		stats["park_right"] += 1
	else:
		stats["park_wrong"] += 1
	if key == "check":
		if truth:
			p.add_contam(3.0)
			records += 1
			return {"text": "박 씨 말이 맞았다. %s은(는) 정문 앞에 서 있었다. 왜 거기 있었는지 기억하지 못한다. 관찰 기록 +1." % p.name, "tone": "good"}
		return {"text": "%s은(는) 내내 자기 자리에 있었다. 박 씨: \"아이고, 제가 잘못 봤나 보네.\"" % p.name, "tone": "info"}
	if truth:
		p.add_contam(10.0)
		p.add_stress(8.0)
		return {"text": "새벽에 %s이(가) 자리로 돌아왔다. 신발이 젖어 있다. 오염 +10." % p.name, "tone": "bad"}
	return {"text": "아무 일도 없었다.", "tone": "info"}


func _radio_visitor() -> void:
	var off: Person = null
	for q in people:
		if q.role == "관리" and q.on_site:
			off = q
	if off == null:
		return
	var text := "폐기물 업체 기사님이 오셨는데, 창고 뒤 드럼 중에 라벨 없는 게 하나 있대요. 어떻게 할까요?"
	var choices := [
		_choice("돈 주고 같이 가져가 달라고 한다", "pay", -1.0, "−40. 깔끔하다"),
		_choice("창고 직원이 열어서 확인한다", "open", 20.0 + 50.0 * 0.6, "성공하면 관찰 기록 +1. 실패하면 오염"),
		_choice("그냥 둔다", "leave", -1.0, ""),
	]
	_new_radio("visitor", off.name, text, "store", choices)


func _r_visitor(_ev: Dictionary, key: String, ok: bool) -> Dictionary:
	match key:
		"pay":
			_pay(40, "폐기")
			return {"text": "기사님이 드럼을 싣고 갔다. (−40)", "tone": "info"}
		"open":
			var ws := in_room("store").filter(func(q: Person) -> bool: return q.on_duty)
			if ok or ws.is_empty():
				records += 1
				return {"text": "안에는 검은 침전물이 바닥에 조금 있었다. 냄새가 없다. 관찰 기록 +1.", "tone": "good"}
			var w: Person = ws[0]
			w.add_contam(8.0)
			return {"text": "%s이(가) 뚜껑을 열자마자 머리가 아프다고 했다. 오염 +8." % w.name, "tone": "bad"}
	return {"text": "드럼은 그대로 창고 뒤에 있다.", "tone": "info"}


func _radio_complaint() -> void:
	var off: Person = null
	for q in people:
		if q.role == "관리" and q.on_site:
			off = q
	if off == null:
		return
	var text := "고객사에서 전화 왔어요. 지난번 드럼 중 몇 개가 색이 이상하대요. 품질 확인서를 보내 달래요."
	var choices := [
		_choice("정제실 사람이 1시간 재검사한다", "check", -1.0, "확실히 넘어가지만 정제가 1시간 느려진다"),
		_choice("문제없다고 답한다", "deny", 60.0, "실패하면 다음 트럭 단가 −10"),
	]
	_new_radio("complaint", off.name, text, "office", choices)


func _r_complaint(_ev: Dictionary, key: String, ok: bool) -> Dictionary:
	if key == "check":
		for p in in_room("refine"):
			if p.on_duty:
				p.busy_until = now() + 60
				p.busy_what = "재검사 중"
				break
		return {"text": "재검사 결과는 정상. 고객사가 넘어갔다.", "tone": "good"}
	if ok:
		return {"text": "고객사가 더 묻지 않았다.", "tone": "good"}
	_pay(10 * Defs.TRUCK_CAP, "클레임")
	return {"text": "고객사가 화가 났다. 위약 −%d." % (10 * Defs.TRUCK_CAP), "tone": "bad"}


# ---------------------------------------------------------------- 낮의 일

func _radio_raw_bad(need: float) -> void:
	var off: Person = null
	for q in people:
		if q.role == "관리" and q.on_site:
			off = q
	var text := "원료 %d드럼이 들어왔는데, 몇 개가 색이 탁해요. 납품 기사는 괜찮다는데요." % need
	var choices := [
		_choice("반품하고 절반만 받는다", "return", -1.0, "오늘 원료가 모자랄 수 있다"),
		_choice("그냥 다 쓴다", "use", -1.0, "오늘 역압 확률 1.5배"),
	]
	var ev := _new_radio("raw_bad", off.name if off != null else "무전", text, "tank", choices)
	ev["need"] = need


func _r_raw_bad(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var need: float = ev["need"]
	if key == "return":
		_buy_raw(floorf(need / 2.0))
		return {"text": "절반만 받았다.", "tone": "info"}
	_buy_raw(need)
	raw_bad_today = true
	return {"text": "다 받았다. 반응실이 오늘 좀 예민할 것이다.", "tone": "info"}


func _radio_no_loader() -> void:
	var l := duty_leader()
	var choices: Array = []
	if l != null:
		choices.append(_choice("%s이(가) 가서 싣는다 (%d분)" % [l.name, walk_minutes(l, "store")], "leader", -1.0, "트럭이 기다린다. 그동안 조정실이 빈다"))
	choices.append(_choice("그냥 보낸다", "skip", -1.0, "이번 트럭은 빈 채로 간다"))
	_new_radio("no_loader", "트럭 기사", "창고에 아무도 없는데요? 싣고 가야 하는데.", "store", choices)


func _r_no_loader(_ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "leader":
		var l := duty_leader()
		_send(l, "store", _leader_loads.bind(l))
		return {"text": "%s이(가) 창고로 간다." % l.name, "tone": "info"}
	_note("트럭이 빈 채로 떠났다.")
	popup.emit("빈 트럭", "dock", BAD)
	return {"text": "트럭이 빈 채로 떠났다.", "tone": "bad"}


func _leader_loads(l: Person) -> void:
	_load_truck(1.0)
	_send(l, "control", Callable())


func _radio_trace() -> void:
	var id: String = anomaly_rooms_last_night[0]
	var l := duty_leader()
	var text := "어젯밤 %s에서 이상한 흔적이 나왔어요. 바닥에 검은 자국이 길게... 치울까요, 조사할까요?" % Defs.ROOMS[id]["name"]
	var choices: Array = []
	var cand := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.role == "조원")
	cand.sort_custom(func(a: Person, b: Person) -> bool: return a.observe > b.observe)
	if not cand.is_empty():
		var p: Person = cand[0]
		choices.append(_choice("%s이(가) 2시간 조사한다" % p.name, "staff", -1.0,
			"관찰 기록 +2. 그동안 %s 자리가 빈다 (생산 감소)" % Defs.POST_NAMES.get(p.post, p.post)))
	var res: Person = null
	for q in people:
		if q.role == "연구원" and q.on_site:
			res = q
	if res != null:
		choices.append(_choice("연구원이 직접 본다", "res", -1.0, "관찰 기록 +3. 오늘 연구가 2시간 멈춘다"))
	choices.append(_choice("치우고 넘어간다", "skip", -1.0, "아무것도 남지 않는다"))
	var ev := _new_radio("trace", l.name if l != null else "무전", text, id, choices)
	ev["staff"] = cand[0] if not cand.is_empty() else null
	anomaly_rooms_last_night.clear()


func _r_trace(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	var id: String = ev["room"]
	match key:
		"staff":
			var p: Person = ev["staff"]
			_send(p, id, _investigate.bind(p, id, 2))
			return {"text": "%s이(가) 조사하러 간다. 도착해서 2시간." % p.name, "tone": "info"}
		"res":
			for q in people:
				if q.role == "연구원" and q.on_site:
					_send(q, id, _investigate.bind(q, id, 3))
			return {"text": "연구원이 간다.", "tone": "info"}
	return {"text": "흔적을 치웠다.", "tone": "info"}


func _investigate(p: Person, id: String, gain: int) -> void:
	p.busy_until = now() + 120
	p.busy_what = "흔적 조사 중"
	crises.append({"kind": "investigate", "room": id, "left": 120, "who": p, "gain": gain})


func _radio_rush() -> void:
	var off: Person = null
	for q in people:
		if q.role == "관리" and q.on_site:
			off = q
	var text := "고객사가 16시 트럭에 %d드럼 더 실어 달래요. 드럼당 %d, 대신 못 맞추면 위약금 %d." % [Defs.RUSH_DRUMS, Defs.RUSH_PRICE, Defs.RUSH_PENALTY]
	text += "\n지금 창고 재고 %d드럼." % stock
	var choices := [
		_choice("받는다", "yes", -1.0, "16시에 재고 %d드럼 이상 있어야 한다. 가동률을 올리면 역압 위험" % (Defs.TRUCK_CAP + Defs.RUSH_DRUMS)),
		_choice("거절한다", "no", -1.0, ""),
	]
	_new_radio("rush", off.name if off != null else "관리실", text, "office", choices)


func _r_rush(_ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "yes":
		rush_order = true
		_note("긴급 주문을 받았다. 16시 트럭에 %d드럼 더." % Defs.RUSH_DRUMS)
		return {"text": "받았다. 16시까지 재고를 채워야 한다.", "tone": "info"}
	return {"text": "거절했다.", "tone": "info"}


func _radio_maint_check() -> void:
	var m: Person = null
	for q in people:
		if q.role == "정비원" and q.on_site:
			m = q
	if m == null:
		return
	var text := "반응기 밸브가 낡았어요. 오늘 2시간 세우고 점검하면 일주일은 역압이 덜 올 겁니다."
	var choices := [
		_choice("세우고 점검한다", "yes", -1.0, "반응실 2시간 정지. 7일 동안 역압 확률 절반"),
		_choice("다음에 한다", "no", -1.0, ""),
	]
	_new_radio("maint", m.name, text, "reactor", choices)


func _r_maint(_ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "yes":
		reactor_down_until = now() + 120
		pressure_mult = 0.5
		pressure_mult_until_day = day + 7
		_note("반응기를 2시간 세우고 점검했다. 7일 동안 역압 확률 절반.")
		return {"text": "점검을 시작했다.", "tone": "good"}
	return {"text": "다음으로 미뤘다.", "tone": "info"}


func _radio_research_done(tgt: String, stage: int) -> void:
	var r: Person = null
	for q in people:
		if q.role == "연구원":
			r = q
	var lines := {
		"extra": ["야간에 인원이 맞지 않는 방이 있습니다. 이제 맵에 '인원 불일치'가 바로 뜹니다.",
			"혼자 일할 때 나타납니다. 회의에서 수칙 「2인 1조」를 켤 수 있습니다. (밤에 두 명씩 묶는 대신 창고가 빈다)",
			"어둠과 관련이 있습니다. 불이 켜진 방에서 인원을 세면 성공률 +15%p."],
		"pressure": ["압력계가 이상합니다. 역압 무전에 이 내용이 같이 뜹니다.",
			"가동률 80%를 넘기면 생깁니다. 회의에서 가동률 옆에 위험도가 보입니다.",
			"계기판이 실제와 반대로 움직입니다. 수칙 「빨간색이면 반대쪽 밸브」: 역압 대응 성공률이 크게 오릅니다."],
	}
	var text: String = "%s 연구 %d단계를 끝냈습니다.\n%s" % [Defs.RESEARCH_NAMES[tgt], stage, lines[tgt][stage - 1]]
	_new_radio("research", r.name if r != null else "연구소", text, "lab", [_choice("알겠다", "ok")])
	_note("연구: %s %d단계." % [Defs.RESEARCH_NAMES[tgt], stage])
	sound.emit("week")


func _r_research(_ev: Dictionary, _key: String, _ok: bool) -> Dictionary:
	return {"text": "", "tone": "none"}


func _add_records_witness(ppl: Array) -> void:
	for p in ppl:
		var person: Person = p
		if rng.randf() * 100.0 < person.eff_observe() * (1.3 if person.has_trait("호기심 많음") else 1.0):
			records += 1


# ---------------------------------------------------------------- 「심부름」 (프롤로그 2일 밤)

func _radio_errand() -> void:
	errand_state = "asked"
	var choices: Array = []
	var cand := on_site_people().filter(func(q: Person) -> bool: return q.on_duty and q.role == "조원")
	cand.sort_custom(func(a: Person, b: Person) -> bool: return a.has_trait("잠을 못 잠") and not b.has_trait("잠을 못 잠"))
	for p in cand.slice(0, 3):
		var person: Person = p
		var why := "요즘 잠을 못 잔다는 그 사람" if person.has_trait("잠을 못 잠") else ""
		var c := _choice("%s을(를) 보낸다" % person.name, "send", -1.0, why)
		c["who"] = person
		choices.append(c)
	choices.append(_choice("거절한다", "refuse", -1.0, "박 씨의 부탁을 들어주지 않는다"))
	_new_radio("errand", "박 씨", "공장장님, 요즘 잠 못 자는 사람 있죠? 그 사람 잠깐 정문으로 보내 주세요. 금방 끝나요.\n...심부름이에요. 이 공장에선 원래 그래요.", "gate", choices)


func _r_errand(ev: Dictionary, key: String, _ok: bool) -> Dictionary:
	if key == "refuse":
		errand_state = "refused"
		_note("박 씨의 부탁을 거절했다.")
		return {"text": "박 씨: \"그럼 할 수 없죠.\" 박 씨는 한참 동안 봉인문 쪽을 바라보았다.", "tone": "info"}
	var p: Person = ev["choices"][_last_choice].get("who")
	if p == null:
		return {"text": "", "tone": "info"}
	errand_state = "away"
	errand_person = p
	p.busy_until = now() + 120
	p.busy_what = "심부름 중"
	_send(p, "gate", Callable())
	crises.append({"kind": "errand", "room": "gate", "left": 120 + walk_minutes(p, "gate"), "who": p})
	_note("%s이(가) 박 씨를 따라 정문 밖으로 나갔다." % p.name)
	return {"text": "%s이(가) 박 씨를 따라 정문 밖으로 나갔다. \"금방 끝나요.\"" % p.name, "tone": "info"}


var _last_choice := 0


func _errand_back(p: Person) -> void:
	errand_state = "back"
	p.errand = true
	p.skill = minf(95.0, p.skill + 15.0)
	p.contam = maxf(p.contam, 30.0)
	p.busy_until = -1
	_send(p, p.post_room() if p.post != "patrol" else "control", Callable())
	_note("%s이(가) 돌아왔다. 아무 일도 없었다고 한다." % p.name)
	popup.emit("%s 돌아옴" % p.name, "gate", Color(0.8, 0.8, 0.9))


# ---------------------------------------------------------------- 프롤로그 안내 (김 반장의 무전)

const TUTORIAL := [
	{"day": 1, "shift": "day", "t": 1, "speaker": "박 씨", "room": "gate",
		"text": "새 공장장님이시구먼. 저번 분은 밤에 오셨는데, 이번엔 낮에 오셨네.\n들어가 보세요. 1조가 지금 출근하고 있어요."},
	{"day": 1, "shift": "day", "t": 25, "speaker": "김 반장", "room": "reactor",
		"text": "1조 반장 김입니다. 공장장님은 지시만 하시면 됩니다. 사람은 제가 움직입니다.\n\n우리 공장은 맵 가운데 줄을 따라 흐릅니다.\n원료 탱크 → 반응실 → 정제실 → 창고 → 출하장 트럭.\n어느 방이든 마우스를 올리면 뭐 하는 곳인지 나옵니다."},
	{"day": 1, "shift": "day", "t": 70, "speaker": "김 반장", "room": "store",
		"text": "창고 사람이 드럼을 옮겨 줘야 정제실이 안 막힙니다. 10시에 첫 트럭이 와요.\n오른쪽 수첩에 오늘 할 일이 적혀 있으니 가끔 보세요."},
	{"day": 1, "shift": "day", "t": 125, "speaker": "김 반장", "room": "dock",
		"text": "방금 트럭 보셨죠? 돈은 저렇게 들어옵니다. 드럼당 65.\n원료값이랑 월급은 교대 끝날 때 나갑니다. 일주일 모아서 적자면 본사가 시끄러워져요."},
	{"day": 1, "shift": "day", "t": 200, "speaker": "김 반장", "room": "control",
		"text": "평소엔 제가 알아서 합니다. 일이 생기면 무전 드릴게요. 그때 고르시면 됩니다.\n\n직접 시키고 싶으시면 왼쪽 명단에서 사람을 누르고 방을 누르세요.\n반응기 가동률은 화면 아래 막대로 언제든 바꿀 수 있습니다. 올리면 돈, 80% 넘기면 위험."},
	{"day": 1, "shift": "night", "t": 2, "speaker": "윤 반장", "room": "control",
		"text": "2조 반장 윤입니다. 오늘 밤은 저희 조예요.\n밤엔 저희 다섯이랑 정문 박 씨뿐입니다. 순찰 한 명이 공장을 돌아요."},
	{"day": 2, "shift": "day", "t": 3, "speaker": "김 반장", "room": "control",
		"text": "어젯밤 고생하셨죠. 밤에 생긴 일은 낮에 수습합니다.\n흔적을 조사하면 관찰 기록이 쌓이고, 연구소가 그걸로 대처법을 찾아요. 대신 조사에 사람을 빼면 생산이 줄어듭니다."},
	{"day": 2, "shift": "day", "t": 90, "speaker": "한 연구원", "room": "lab",
		"text": "연구소 한입니다. 관찰 기록이 모이면 연구해서 근무 수칙을 만들게요. 지금 기록 %RECORDS%개.\n어떤 걸 먼저 연구할지는 아침 회의에서 정해 주세요."},
	{"day": 2, "shift": "day", "t": 240, "speaker": "배 사원", "room": "office",
		"text": "관리실 배입니다. 돈 쓸 데 생기면 회의 때 말씀하세요.\n비상 조명, CCTV, 휴게실 안마의자, 회식... 다 회의의 '설비 투자'에 있어요."},
	{"day": 2, "shift": "night", "t": 2, "speaker": "윤 반장", "room": "control",
		"text": "하나만 알려 드릴게요. 밤에 불 켜진 방을 보시다가, 이름표 없는 사람이 하나 더 서 있으면...\n그게 '한 명 더'입니다. 보이는 대로 누구든 그 방에 보내세요. 둘이 되면 사라져요."},
	{"day": 2, "shift": "night", "t": 90, "special": "errand"},
	{"day": 3, "shift": "day", "t": 3, "speaker": "최 반장", "room": "control",
		"text": "3조 반장 최입니다. 반장 된 지 얼마 안 됐어요. 잘 부탁드립니다.\n근무표는 6일 주기로 돕니다. 주간 이틀, 야간 이틀, 휴무 이틀. 왼쪽 명단 머리에 오늘·내일이 보여요."},
	{"day": 3, "shift": "night", "t": 2, "speaker": "김 반장", "room": "control",
		"text": "오늘 밤은 다시 저희 1조입니다.\n사람이 모자라면 쉬는 사람을 부를 수 있어요. 명단에서 집에 있는 사람을 누르면 됩니다. 오는 데 한 시간쯤, 위반 기록이 남습니다."},
	{"day": 3, "shift": "night", "t": 150, "special": "extra_store"},
]


func _tutorial() -> void:
	for i in TUTORIAL.size():
		var s: Dictionary = TUTORIAL[i]
		if tut_fired.has(i) or s["day"] != day or s["shift"] != shift or t < s["t"]:
			continue
		tut_fired[i] = true
		if s.has("special"):
			match s["special"]:
				"errand":
					_radio_errand()
				"extra_store":
					_force_extra("store")
			continue
		var text: String = s["text"].replace("%RECORDS%", str(records))
		_new_radio("tutorial", s["speaker"], text, s["room"], [_choice("알겠다", "ok")])
		return
	# 프롤로그 1일 밤: 첫 역압 (무조건 넘길 수 있게, 반장 판정이 높다)
	if day == 1 and is_night() and t == 200 and not _has_crisis("pressure"):
		_start_pressure()


func _r_tutorial(_ev: Dictionary, _key: String, _ok: bool) -> Dictionary:
	return {"text": "", "tone": "none"}


func _force_extra(id: String) -> void:
	var ppl := in_room(id).filter(func(q: Person) -> bool: return q.on_duty)
	if ppl.is_empty():
		for q in people:
			if q.on_duty and q.on_site and q.post == "store":
				ppl = [q]
	if ppl.is_empty():
		return
	entity_room = id
	entity_since = now() - Defs.EXTRA_REPORT_DELAY
	entity_reported = true
	anomaly_rooms_tonight.append(id)
	_radio_extra(ppl[0])


# ================================================================ 박 씨의 한마디

func park_comment() -> String:
	var pool: Array = []
	var log_text := " ".join(shift_log)
	if log_text.contains("적발"):
		pool.append("숫자가 맞으니 다행이네요. 세는 거, 잊지 마세요.")
	if log_text.contains("놓쳤다"):
		pool.append("한 명 더 있었다고요? 그런 날도 있죠. 다음엔 불 켜고 세 보세요.")
	if log_text.contains("정전"):
		pool.append("불은 켜 두세요. 옛날부터 그랬어요.")
	if log_text.contains("호출"):
		pool.append("한밤중에 불려 나온 사람 얼굴 봤어요? 너무 자주 부르지 마세요.")
	if log_text.contains("역압") or log_text.contains("배관"):
		pool.append("반응실 계기는 믿지 마세요. 저번 공장장님도 그거 때문에 고생하셨어요.")
	if log_text.contains("심부름"):
		pool.append("고마워요. 금방 끝났죠?")
	if log_text.contains("거절"):
		pool.append("괜찮아요. 다음엔 들어주실 거잖아요.")
	if errand_state == "back" and errand_person != null and not park_used.has("errand_back"):
		pool.append("%s 씨, 요즘 일 잘하죠? 그럴 거예요." % errand_person.name)
	if is_night() and shift_log.size() <= 2:
		pool.append("조용한 밤이었네요. 이런 날도 있어야죠.")
	if not is_night():
		pool.append("낮엔 사람이 많아서 좋아요. 밤엔 세기가 쉬운데.")
		pool.append("트럭 기사가 그러는데, 요즘 우리 물건이 잘 나간대요.")
	pool.append("이번 공장장님은 오래 계셨으면 좋겠네.")
	pool.append("밤을 넘기는 게 이기는 거라고, 우리 어머니가 그랬어요.")
	for line in pool:
		if not park_used.has(line):
			park_used[line] = true
			return line
	return "......"


# ================================================================ 수첩 (지금 할 일)

func notebook() -> Array:
	var out: Array = []
	var crew_l := leader_of(crew_now())
	out.append("[b]%d일차 %s · %d조 (%s)[/b]" % [day, "야간" if is_night() else "주간", crew_now(), crew_l.name if crew_l else "-"])
	if day <= Defs.PROLOGUE_DAYS:
		out.append("[color=#9ab]프롤로그 %d/3일: 김 반장의 무전을 따라가세요[/color]" % day)
	if is_night():
		var left := (Defs.SHIFT_MIN - t) / 60
		out.append("· 아침까지 버티기: %d시간 남음" % left)
		out.append("· 혼자 있는 방, 불 꺼진 방 조심")
	else:
		var done := 0
		for tt in Defs.TRUCK_T:
			if t >= tt:
				done += 1
		out.append("· 트럭 출하 %d/2 (10:00, 16:00) · 창고 재고 %d" % [done, stock])
		if t < Defs.RAW_DELIVERY_T:
			out.append("· 08:30 원료 입고")
		if rush_order:
			out.append("· [color=#fc6]긴급 주문: 16시에 %d드럼 필요[/color]" % (Defs.TRUCK_CAP + Defs.RUSH_DRUMS))
	for c in crises:
		if c["kind"] == "pressure":
			out.append("· [color=#f66]역압! %d분 안에 막아야 한다[/color]" % c["left"])
	if entity_room != "" and (research["extra"] >= 1 or owned.has("cctv")):
		out.append("· [color=#f66]%s 인원 불일치[/color]" % Defs.ROOMS[entity_room]["name"])
	var dark: Array = []
	for id in Defs.ROOMS:
		if not lit[id]:
			dark.append(Defs.ROOMS[id]["name"])
	if not dark.is_empty():
		out.append("· [color=#f96]불 꺼짐: %s[/color]" % ", ".join(dark))
	var wp := week_income - week_cost
	var days_left := Defs.WEEK_DAYS - ((day - 1) % Defs.WEEK_DAYS) - 1
	out.append("")
	out.append("[b]이번 주[/b] 손익 %s%d · 결산까지 %d일" % ["+" if wp >= 0 else "", wp, days_left])
	out.append("  적자로 끝나면 본사 감사가 온다")
	out.append("[b]연구[/b] " + research_line())
	if violations > 0:
		out.append("[b]위반 기록[/b] %d건 (감사 때 돌아온다)" % violations)
	return out
