class_name WorldView
extends Control
## 공장 맵. 도형으로 그린 임시 아트. 사람은 복도를 따라 실제로 걸어 다닌다.

signal room_clicked(id: String)
signal person_clicked(p: Person)

const CREW_COL := {0: Color(0.85, 0.85, 0.9), 1: Color(0.95, 0.6, 0.25), 2: Color(0.35, 0.6, 0.95), 3: Color(0.45, 0.8, 0.45)}
const SUIT := Color(0.32, 0.36, 0.42)

var sim: Sim
var font: Font
var selected: Person = null
var focus_room := ""
var focus_until := 0.0
var popups: Array = []      # [{text, pos, col, life}]
var clock := 0.0
var hover_room := ""
var _person_spots: Array = []  # [pos, Person] 클릭용


func _ready() -> void:
	custom_minimum_size = Defs.MAP_SIZE
	mouse_filter = Control.MOUSE_FILTER_STOP
	tooltip_text = " "


func add_popup(text: String, room: String, col: Color) -> void:
	var p := Defs.room_center(room) if Defs.ROOMS.has(room) else Defs.MAP_SIZE / 2.0
	p.y -= 40.0
	for q in popups:
		if q["pos"].distance_to(p) < 30.0:
			p.y -= 26.0
	popups.append({"text": text, "pos": p, "col": col, "life": 3.0})


func focus(room: String) -> void:
	if room == "" or not Defs.ROOMS.has(room):
		return
	focus_room = room
	focus_until = clock + 4.0


func _process(delta: float) -> void:
	clock += delta
	for q in popups.duplicate():
		q["life"] -= delta
		q["pos"].y -= delta * 14.0
		if q["life"] <= 0.0:
			popups.erase(q)
	queue_redraw()


func _text(pos: Vector2, s: String, size: int, col: Color, width: float = -1.0, align := HORIZONTAL_ALIGNMENT_LEFT) -> void:
	draw_string(font, pos, s, align, width, size, col)


func _draw() -> void:
	if sim == null or font == null:
		return
	var night := sim.is_night()
	draw_rect(Rect2(Vector2.ZERO, Defs.MAP_SIZE), Color(0.10, 0.11, 0.13) if not night else Color(0.04, 0.045, 0.06))
	# 복도
	var hall_col := Color(0.18, 0.19, 0.22) if not night else Color(0.09, 0.095, 0.11)
	for y in Defs.HALL_Y:
		draw_rect(Rect2(0, y - 22, Defs.MAP_SIZE.x, 44), hall_col)
	draw_rect(Rect2(Defs.LINK_X - 20, Defs.HALL_Y[0], 40, Defs.HALL_Y[1] - Defs.HALL_Y[0]), hall_col)
	_text(Vector2(Defs.MAP_SIZE.x - 90, Defs.HALL_Y[0] + 5), "복도", 12, Color(0.45, 0.47, 0.5))
	_text(Vector2(Defs.MAP_SIZE.x - 90, Defs.HALL_Y[1] + 5), "복도", 12, Color(0.45, 0.47, 0.5))
	# 생산 흐름 화살표
	_draw_flow()
	# 방
	for id in Defs.ROOMS:
		_draw_room(id, night)
	# 사람
	_person_spots.clear()
	var counts := {}
	for p in sim.people:
		if not p.on_site or p.gone:
			continue
		var pos := p.pos
		if not p.walking() and Defs.ROOMS.has(p.room):
			var i: int = counts.get(p.room, 0)
			counts[p.room] = i + 1
			pos = _slot(p.room, i)
			if not sim.lit[p.room]:
				# 불 꺼진 방은 안이 보이지 않는다
				_person_spots.append([pos, p])
				continue
		_draw_person(pos, p)
		_person_spots.append([pos, p])
	# '한 명 더': 불이 켜져 있으면 사람이 하나 더 보인다 (이름표가 없다)
	if sim.entity_room != "" and sim.lit[sim.entity_room]:
		var i2: int = counts.get(sim.entity_room, 0)
		var ep := _slot(sim.entity_room, i2)
		var ppl := sim.in_room(sim.entity_room)
		var col: Color = CREW_COL[ppl[0].crew] if ppl.size() > 0 else CREW_COL[1]
		_draw_figure(ep, col, 0, false)
	# 박 씨
	var gp := Defs.room_center("gate") + Vector2(-70, 10)
	_draw_figure(gp, Color(0.25, 0.25, 0.3), 0, false, Color(0.2, 0.22, 0.35))
	_text(gp + Vector2(-14, 26), "박 씨", 11, Color(0.75, 0.75, 0.85))
	# 팝업
	for q in popups:
		var a: float = clampf(q["life"], 0.0, 1.0)
		var c: Color = q["col"]
		c.a = a
		var s: String = q["text"]
		var w := font.get_string_size(s, HORIZONTAL_ALIGNMENT_LEFT, -1, 20).x
		draw_rect(Rect2(q["pos"] + Vector2(-w / 2 - 6, -20), Vector2(w + 12, 28)), Color(0, 0, 0, 0.6 * a))
		_text(q["pos"] + Vector2(-w / 2, 0), s, 20, c)


func _slot(id: String, i: int) -> Vector2:
	var r: Rect2 = Defs.ROOMS[id]["rect"]
	var cols := maxi(1, int((r.size.x - 30) / 40))
	return r.position + Vector2(26 + (i % cols) * 40, r.size.y - 70 - (i / cols) * 48)


func _draw_room(id: String, night: bool) -> void:
	var d: Dictionary = Defs.ROOMS[id]
	var r: Rect2 = d["rect"]
	var is_lit: bool = sim.lit[id]
	var base := Color(0.20, 0.22, 0.26)
	if night:
		base = Color(0.17, 0.18, 0.21)
	if not is_lit:
		base = Color(0.02, 0.02, 0.03)
	draw_rect(r, base)
	var border := Color(0.38, 0.40, 0.45)
	if hover_room == id:
		border = Color(0.7, 0.75, 0.85)
	draw_rect(r, border, false, 2.0)
	# 문
	var door := Defs.door_point(id)
	var dy := r.end.y if d["hall"] == 0 or r.position.y < door.y else r.position.y
	draw_line(Vector2(door.x - 14, dy), Vector2(door.x + 14, dy), base.lightened(0.25), 4.0)
	_draw_equipment(id, r, is_lit)
	# 이름과 상태
	var tc := Color(0.92, 0.92, 0.95) if is_lit else Color(0.5, 0.5, 0.55)
	_text(r.position + Vector2(10, 22), d["name"], 17, tc)
	_text(r.position + Vector2(10, 42), _status(id), 13, Color(0.75, 0.8, 0.85) if is_lit else Color(0.45, 0.45, 0.5), r.size.x - 16)
	if not is_lit:
		_text(r.position + Vector2(10, r.size.y / 2), "불 꺼짐", 18, Color(1.0, 0.6, 0.4))
	# 위기 막대
	for c in sim.crises:
		if c["room"] != id:
			continue
		var total := Defs.PRESSURE_WINDOW if c["kind"] == "pressure" else 60
		var k := clampf(float(c["left"]) / total, 0.0, 1.0)
		var col := Color(1.0, 0.3, 0.25) if c["kind"] == "pressure" else Color(0.6, 0.8, 1.0)
		var br := Rect2(r.position + Vector2(8, 50), Vector2(r.size.x - 16, 8))
		draw_rect(br, Color(0, 0, 0, 0.6))
		draw_rect(Rect2(br.position, Vector2(br.size.x * k, br.size.y)), col)
		if c["kind"] == "pressure":
			_text(br.position + Vector2(0, 24), "역압! %d분 남음" % c["left"], 15, col)
	if sim.entity_room == id and sim.research["extra"] >= 1:
		_text(r.position + Vector2(10, 64), "인원 불일치", 15, Color(1.0, 0.4, 0.4))
	# 안내 강조
	if focus_room == id and clock < focus_until:
		var a := 0.5 + 0.5 * sin(clock * 6.0)
		draw_rect(r.grow(4), Color(1.0, 0.85, 0.3, a), false, 4.0)


func _status(id: String) -> String:
	match id:
		"tank":
			return "원료 %d/%d" % [sim.tank, Defs.TANK_CAP]
		"reactor":
			if sim.reactor_down_until > sim.now():
				return "정지 (%d분)" % (sim.reactor_down_until - sim.now())
			return "가동률 %d%% · 미정제 %.1f" % [sim.rate, sim.buffer]
		"refine":
			var s := "출구 %d/%d" % [sim.outbox, Defs.OUTBOX_CAP]
			if sim.outbox >= Defs.OUTBOX_CAP - 0.05:
				s += " 막힘!"
			return s
		"store":
			return "재고 %d드럼" % sim.stock
		"dock":
			if sim.is_night():
				return "트럭은 낮에만"
			for tt in Defs.TRUCK_T:
				if sim.t < tt:
					var m: int = 8 * 60 + tt
					return "다음 트럭 %02d:%02d" % [m / 60, m % 60]
			return "오늘 트럭 끝"
		"lab":
			return "관찰 기록 %d" % sim.records
		"control":
			return "무전·CCTV"
		"gate":
			return "출입 기록"
		"rest":
			return "피로 회복"
	return ""


func _draw_flow() -> void:
	var y := 480.0
	var chain := [["tank", "reactor"], ["reactor", "refine"], ["refine", "store"]]
	for pair in chain:
		var a: Rect2 = Defs.ROOMS[pair[0]]["rect"]
		var b: Rect2 = Defs.ROOMS[pair[1]]["rect"]
		var x0 := a.end.x
		var x1 := b.position.x
		if pair[0] == "reactor":
			x1 = Defs.ROOMS["control"]["rect"].position.x
		draw_line(Vector2(x0, y), Vector2(x1, y), Color(0.5, 0.55, 0.35), 3.0)
	# 창고 → 출하장
	var s: Rect2 = Defs.ROOMS["store"]["rect"]
	var dk: Rect2 = Defs.ROOMS["dock"]["rect"]
	draw_line(Vector2(s.get_center().x + 40, s.end.y), Vector2(dk.get_center().x + 40, dk.position.y), Color(0.5, 0.55, 0.35), 3.0)
	# 움직이는 점: 지금 흐르고 있으면
	var flowing := sim.reactor_down_until <= sim.now() and sim.tank > 0.5
	if flowing and sim.shift_done == false:
		var k := fmod(clock * 0.4, 1.0)
		var xa: float = Defs.ROOMS["tank"]["rect"].end.x
		var xb: float = Defs.ROOMS["store"]["rect"].position.x
		draw_circle(Vector2(lerpf(xa, xb, k), y), 4.0, Color(0.8, 0.9, 0.5))


func _draw_equipment(id: String, r: Rect2, is_lit: bool) -> void:
	var a := 0.55 if is_lit else 0.15
	var c := Color(0.55, 0.6, 0.65, a)
	var cx := r.get_center().x
	match id:
		"tank":
			var h := (r.size.y - 110) * clampf(sim.tank / Defs.TANK_CAP, 0.0, 1.0)
			draw_rect(Rect2(r.position.x + 40, r.position.y + 70, 70, r.size.y - 110), c, false, 2.0)
			draw_rect(Rect2(r.position.x + 40, r.end.y - 40 - h, 70, h), Color(0.5, 0.45, 0.3, a))
		"reactor":
			draw_circle(Vector2(r.end.x - 60, r.position.y + 110), 34.0, c)
			draw_circle(Vector2(r.end.x - 60, r.position.y + 110), 34.0, Color(0.3, 0.3, 0.35, a), false, 2.0)
			var ang := -PI * 0.75 + PI * 1.5 * clampf(sim.rate / 120.0, 0.0, 1.0)
			if sim._has_crisis("pressure"):
				ang += sin(clock * 20.0) * 0.4
			draw_line(Vector2(r.end.x - 60, r.position.y + 110), Vector2(r.end.x - 60, r.position.y + 110) + Vector2.from_angle(ang) * 26.0, Color(0.9, 0.3, 0.3, a + 0.3), 3.0)
		"refine":
			for i in 3:
				draw_rect(Rect2(r.end.x - 110 + i * 32, r.position.y + 70, 20, 110), c)
			for i in int(sim.outbox):
				draw_rect(Rect2(r.position.x + 12 + i * 22, r.position.y + 150, 18, 22), Color(0.35, 0.55, 0.7, a + 0.2))
		"store":
			var n := int(sim.stock)
			for i in mini(n, 30):
				draw_rect(Rect2(r.position.x + 90 + (i % 5) * 20, r.position.y + 160 - (i / 5) * 18, 16, 15), Color(0.35, 0.55, 0.7, a + 0.2))
		"dock":
			if not sim.is_night():
				for tt in Defs.TRUCK_T:
					if absi(sim.t - tt) < 20:
						draw_rect(Rect2(r.end.x - 140, r.position.y + 70, 120, 60), Color(0.6, 0.6, 0.65, a + 0.2))
						draw_rect(Rect2(r.end.x - 40, r.position.y + 85, 25, 45), Color(0.5, 0.5, 0.55, a + 0.2))
		"rest":
			draw_rect(Rect2(r.end.x - 100, r.position.y + 90, 80, 30), Color(0.45, 0.35, 0.3, a))
		"control":
			draw_rect(Rect2(r.position.x + 20, r.position.y + 70, 110, 40), Color(0.25, 0.35, 0.4, a + 0.2))
		"lab":
			draw_rect(Rect2(r.end.x - 90, r.position.y + 80, 70, 40), Color(0.4, 0.45, 0.5, a))
		"maint":
			draw_rect(Rect2(r.end.x - 70, r.position.y + 80, 50, 60), Color(0.45, 0.4, 0.3, a))
		"office":
			draw_rect(Rect2(r.end.x - 90, r.position.y + 80, 70, 35), Color(0.4, 0.38, 0.32, a))
		"gate":
			draw_rect(Rect2(r.end.x - 70, r.position.y + 60, 50, 50), Color(0.3, 0.32, 0.4, a))


func _draw_person(pos: Vector2, p: Person) -> void:
	var col: Color = CREW_COL[p.crew]
	_draw_figure(pos, col, p.stage(), p.role == "반장")
	if p == selected:
		draw_arc(pos + Vector2(0, -2), 17.0, 0, TAU, 24, Color(1.0, 0.95, 0.4), 2.0)
	var nm := p.name.split(" ")[0]
	_text(pos + Vector2(-18, 26), p.name, 11, Color(0.85, 0.85, 0.9) if not p.broken else Color(1, 0.5, 0.5))
	if p.busy_until > sim.now():
		_text(pos + Vector2(-8, -18), "…", 14, Color(0.8, 0.8, 0.9))


func _draw_figure(p: Vector2, helmet: Color, stage: int, leader: bool, suit: Color = SUIT) -> void:
	draw_rect(Rect2(p.x - 7.0, p.y - 2.0, 14.0, 13.0), suit)
	draw_rect(Rect2(p.x - 7.0, p.y + 4.0, 14.0, 2.0), suit.lightened(0.35))
	var skin := Color(0.93, 0.80, 0.68)
	if stage >= 4:
		skin = Color(0.62, 0.62, 0.66)
	draw_circle(p + Vector2(0, -7), 6.0, skin)
	draw_arc(p + Vector2(0, -8), 6.0, PI, TAU, 10, helmet, 4.0)
	if leader:
		draw_rect(Rect2(p.x - 7.0, p.y + 1.0, 14.0, 3.0), Color(0.95, 0.85, 0.3))
	var ink := Color(0.08, 0.08, 0.10)
	if stage == 1:
		draw_line(p + Vector2(-3, -5), p + Vector2(-1, -5), Color(0.35, 0.25, 0.30), 1.5)
		draw_line(p + Vector2(1, -5), p + Vector2(3, -5), Color(0.35, 0.25, 0.30), 1.5)
	elif stage == 2:
		draw_circle(p + Vector2(-2, -6), 1.4, Color(0.95, 0.95, 0.97))
		draw_circle(p + Vector2(2, -6), 1.4, Color(0.95, 0.95, 0.97))
	elif stage >= 3:
		draw_circle(p + Vector2(-2, -6), 1.5, ink)
		draw_circle(p + Vector2(2, -6), 1.5, ink)
		draw_line(p + Vector2(-5, 0), p + Vector2(-3, 8), ink, 1.5)
		draw_line(p + Vector2(4, -1), p + Vector2(6, 7), ink, 1.5)


func room_at(pos: Vector2) -> String:
	for id in Defs.ROOMS:
		var r: Rect2 = Defs.ROOMS[id]["rect"]
		if r.has_point(pos):
			return id
	return ""


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion:
		hover_room = room_at(event.position)
	if event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT:
		for spot in _person_spots:
			if spot[0].distance_to(event.position) < 14.0:
				person_clicked.emit(spot[1])
				return
		var id := room_at(event.position)
		if id != "":
			room_clicked.emit(id)
	if event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_RIGHT:
		person_clicked.emit(null)


func _get_tooltip(at: Vector2) -> String:
	var id := room_at(at)
	if id == "":
		return ""
	var d: Dictionary = Defs.ROOMS[id]
	var s := "%s\n하는 일: %s\n필요 인원: %s\n지금: %s" % [d["name"], d["does"], d["who"], _status(id)]
	if not sim.lit[id]:
		s += "\n불이 꺼져 있다: 작업 −40%, '한 명 더'가 잘 생긴다. 사람을 보내면 켠다"
	return s
