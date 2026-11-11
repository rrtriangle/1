class_name WorldView
extends Control
## 공장 맵을 회색 박스로 그린다. 구역 → 칸(방) → 복도. 조명이 꺼진 방 안은 보이지 않는다.

signal room_clicked(room_id: String)
signal corridor_clicked(zone_id: String)

const OFFS_L := [Vector2(-26, -8), Vector2(26, -8), Vector2(-26, 26), Vector2(26, 26)]
const OFFS_M := [Vector2(0, -8), Vector2(0, 26), Vector2(-18, 9), Vector2(18, 9)]
const BLUE := Color(0.42, 0.52, 0.72)

var game: Game
var font: Font


func _ready() -> void:
	mouse_filter = Control.MOUSE_FILTER_STOP
	clip_contents = true


func slot_position(room_id: String, index: int) -> Vector2:
	var rect: Rect2 = game.rooms[room_id]["rect"]
	var offs: Array = OFFS_L if rect.size.x >= 100.0 else OFFS_M
	return rect.get_center() + Vector2(0, 8) + offs[index % offs.size()]


func corridor_rect(z: String) -> Rect2:
	var zr: Rect2 = Game.ZONE_DEF[z]["rect"]
	return Rect2(zr.position.x + 4.0, zr.end.y - 30.0, zr.size.x - 8.0, 26.0)


func _text(pos: Vector2, s: String, size: int, col: Color, width: float = -1.0) -> void:
	draw_string(font, pos, s, HORIZONTAL_ALIGNMENT_LEFT, width, size, col)


func _dashed_rect(r: Rect2, col: Color) -> void:
	var a := r.position
	var b := Vector2(r.end.x, r.position.y)
	var c := r.end
	var d := Vector2(r.position.x, r.end.y)
	for seg in [[a, b], [b, c], [c, d], [d, a]]:
		draw_dashed_line(seg[0], seg[1], col, 1.0, 5.0)


func _draw() -> void:
	if game == null:
		return
	draw_rect(Rect2(Vector2.ZERO, size), Color(0.10, 0.11, 0.13))

	# 구역을 잇는 통로
	for l in Game.ZONE_LINKS:
		var ra: Rect2 = Game.ZONE_DEF[l[0]]["rect"]
		var rb: Rect2 = Game.ZONE_DEF[l[1]]["rect"]
		var pa := ra.get_center().clamp(rb.position, rb.end)
		var pb := rb.get_center().clamp(ra.position, ra.end)
		pa = pa.clamp(ra.position, ra.end)
		var open: bool = game.zones[l[0]]["open"] and game.zones[l[1]]["open"]
		draw_line(pa, pb, Color(0.5, 0.52, 0.6) if open else Color(0.25, 0.26, 0.3), 6.0)

	for z in Game.ZONE_ORDER:
		var zr: Rect2 = Game.ZONE_DEF[z]["rect"]
		var zs: Dictionary = game.zones[z]
		var nm: String = Game.ZONE_DEF[z]["name"]
		if not zs["open"]:
			draw_rect(zr, Color(0.05, 0.05, 0.07))
			draw_rect(zr, Color(0.28, 0.29, 0.34), false, 1.0)
			_text(zr.position + Vector2(8, 17), nm + " · 폐쇄", 12, Color(0.5, 0.5, 0.56))
			if zs["reclaiming"]:
				_text(zr.position + Vector2(8, 160), "정화 작업 중 · 남은 %d교대" % zs["purify_left"], 11, Color(0.85, 0.65, 0.35))
			elif Game.RECLAIM.has(z):
				var d: Dictionary = Game.RECLAIM[z]
				_text(zr.position + Vector2(8, 160), "탈환: %d (회의 → 시설)" % d["cost"], 11, Color(0.42, 0.42, 0.48))
			else:
				_text(zr.position + Vector2(8, 160), "데모에서는 잠겨 있음", 11, Color(0.42, 0.42, 0.48))
			continue
		draw_rect(zr, Color(0.14, 0.15, 0.18))
		draw_rect(zr, Color(0.38, 0.40, 0.47), false, 1.0)
		_text(zr.position + Vector2(8, 17), nm, 12, Color(0.75, 0.76, 0.82))
		# 복도
		var cr := corridor_rect(z)
		var lit: bool = zs["corr_light"]
		draw_rect(cr, Color(0.45, 0.46, 0.50) if lit else Color(0.04, 0.04, 0.06))
		draw_rect(cr, Color(0.38, 0.40, 0.47), false, 1.0)
		_text(cr.position + Vector2(6, 17), "복도" if lit else "복도 · 소등", 11, Color(0.10, 0.10, 0.12) if lit else Color(0.5, 0.5, 0.56))
	# 고정 시설 표시
	var gate_r: Rect2 = Game.ZONE_DEF["gate"]["rect"]
	_text(gate_r.position + Vector2(10, 60), "경비실 (박 씨)", 12, Color(0.6, 0.6, 0.66))
	_text(gate_r.position + Vector2(10, 80), "공장장실은 사무동 2층", 11, Color(0.42, 0.42, 0.48))
	var dock_r: Rect2 = Game.ZONE_DEF["dock"]["rect"]
	_text(dock_r.position + Vector2(10, 60), "트럭 도크", 12, Color(0.6, 0.6, 0.66))
	_text(dock_r.position + Vector2(10, 80), "낮 10시·16시 출하 (1회 12드럼)", 11, Color(0.42, 0.42, 0.48))
	_text(dock_r.position + Vector2(10, 100), "재고 %.0f · 정제실 출구 %.0f" % [game.stock, game.outbox], 11, Color(0.6, 0.6, 0.66))
	var prod_r: Rect2 = Game.ZONE_DEF["prod"]["rect"]
	_text(Vector2(prod_r.end.x - 118, prod_r.position.y + 17), "1호 반응기 · 봉인", 11, Color(0.55, 0.35, 0.35))

	# 빈 칸
	for id in game.slot_order:
		var sl: Dictionary = game.slots[id]
		if sl["room"] != "" or not game.zones[sl["zone"]]["open"]:
			continue
		var r: Rect2 = sl["rect"]
		_dashed_rect(r, Color(0.40, 0.42, 0.48))
		_text(r.position + Vector2(5, 16), "빈 칸", 11, Color(0.5, 0.5, 0.56))
		_text(r.position + Vector2(5, 31), Game.SIZE_NAME[sl["size"]], 11, Color(0.5, 0.5, 0.56))

	# 방
	for id in game.rooms:
		var r: Dictionary = game.rooms[id]
		var rect: Rect2 = r["rect"]
		var lit: bool = r["light"]
		var fill := Color(0.80, 0.82, 0.85) if lit else Color(0.03, 0.03, 0.05)
		if r["type"] == "purify" and not lit:
			fill = Color(0.08, 0.07, 0.05)
		if r["alert"]:
			fill = fill.lerp(Color(0.95, 0.55, 0.20), 0.45)
		draw_rect(rect, fill)
		draw_rect(rect, Color(0.45, 0.46, 0.52), false, 2.0)
		var ink := Color(0.10, 0.10, 0.12) if lit else Color(0.55, 0.55, 0.62)
		_text(rect.position + Vector2(5, 14), r["name"], 12, ink, rect.size.x - 6.0)
		var sub := ""
		if lit or r["cctv"]:
			sub = "인원 %d" % game.headcount(id)
		else:
			sub = "인원 ? 소등"
		if r["cctv"]:
			sub += " CCTV"
		if r["down"]:
			sub = "정지"
		_text(rect.position + Vector2(5, 28), sub, 11, ink if not r["down"] else Color(0.85, 0.4, 0.3), rect.size.x - 6.0)
		if lit or r["cctv"]:
			_draw_equipment(r, rect, lit)
		if r["type"] == "reactor":
			_text(rect.position + Vector2(5, rect.size.y - 5), "가동률 %d%%" % game.reactor_rate, 11, ink)

	# 직원
	for s in game.staff:
		if not s.on_map():
			continue
		var r: Dictionary = game.rooms[s.room]
		var visible_now: bool = r["light"] or r["cctv"]
		if s.transit > 0:
			visible_now = false
			for z in s.transit_path:
				if game.zones[z]["corr_light"]:
					visible_now = true
		if not visible_now:
			continue
		var col := s.color()
		if s.transit > 0 or (r["cctv"] and not r["light"]):
			col = col.darkened(0.35)
		_draw_person(s.pos, col, Color(0.95, 0.80, 0.25), s.contam_stage())
		var ink := Color(0.10, 0.10, 0.12) if (r["light"] and s.transit == 0) else Color(0.75, 0.75, 0.8)
		draw_string(font, s.pos + Vector2(-30, 20), s.name, HORIZONTAL_ALIGNMENT_CENTER, 60, 11, ink)
		if game.selected == s:
			draw_arc(s.pos, 13.0, 0.0, TAU, 24, Color(0.20, 0.55, 1.0), 2.0)

	# 현장파 감사관: 명단에 있는 외부인
	if game.auditor_room != "" and game.rooms.has(game.auditor_room) and game.rooms[game.auditor_room]["light"]:
		var p := slot_position(game.auditor_room, game.present_in(game.auditor_room).size())
		_draw_person(p, Color(0.35, 0.36, 0.40), Color(0.92, 0.92, 0.95))
		draw_string(font, p + Vector2(-30, 20), "감사관", HORIZONTAL_ALIGNMENT_CENTER, 60, 11, Color(0.10, 0.10, 0.12))

	# "한 명 더": 다른 직원과 똑같은 색, 이름표가 없다. 불이 켜져 있어야 보인다.
	for id in game.rooms:
		var r: Dictionary = game.rooms[id]
		if r["extra"] and r["light"] and r["victim"] != null:
			var v: Staff = r["victim"]
			_draw_person(v.pos + Vector2(22, -4), BLUE, Color(0.95, 0.80, 0.25))


## 사람: 작업복 몸통 + 머리 + 안전모. "한 명 더"도 똑같이 그린다.
## stage: 오염 단계. 1 다크서클, 2 초점 없는 눈, 3 검은 무늬, 4 변이
func _draw_person(p: Vector2, suit: Color, helmet: Color, stage: int = 0) -> void:
	draw_rect(Rect2(p.x - 6.0, p.y - 2.0, 12.0, 11.0), suit)
	draw_rect(Rect2(p.x - 6.0, p.y + 3.0, 12.0, 2.0), suit.lightened(0.35))
	var skin := Color(0.93, 0.80, 0.68)
	if stage >= 4:
		skin = Color(0.62, 0.62, 0.66)
	draw_circle(p + Vector2(0, -6), 5.0, skin)
	draw_arc(p + Vector2(0, -7), 5.0, PI, TAU, 10, helmet, 3.0)
	var ink := Color(0.08, 0.08, 0.10)
	if stage == 1:
		draw_line(p + Vector2(-3, -4), p + Vector2(-1, -4), Color(0.35, 0.25, 0.30), 1.0)
		draw_line(p + Vector2(1, -4), p + Vector2(3, -4), Color(0.35, 0.25, 0.30), 1.0)
	elif stage == 2:
		draw_circle(p + Vector2(-2, -5), 1.2, Color(0.95, 0.95, 0.97))
		draw_circle(p + Vector2(2, -5), 1.2, Color(0.95, 0.95, 0.97))
	elif stage >= 3:
		draw_circle(p + Vector2(-2, -5), 1.3, ink)
		draw_circle(p + Vector2(2, -5), 1.3, ink)
		draw_line(p + Vector2(-4, 0), p + Vector2(-2, 7), ink, 1.5)
		draw_line(p + Vector2(3, -1), p + Vector2(5, 6), ink, 1.5)
		if stage >= 4:
			draw_line(p + Vector2(-1, -2), p + Vector2(1, 8), ink, 1.5)
			draw_line(p + Vector2(-5, -6), p + Vector2(-2, -3), ink, 1.0)


## 방마다 설비를 도형으로 그린다 (임시 아트)
func _draw_equipment(r: Dictionary, rect: Rect2, lit: bool) -> void:
	var a := 0.55 if lit else 0.25
	var steel := Color(0.42, 0.45, 0.52, a)
	var dark := Color(0.25, 0.27, 0.32, a)
	var o := rect.position
	var w := rect.size.x
	var h := rect.size.y
	match r["type"]:
		"reactor":
			var c := o + Vector2(w - 26.0, h - 44.0)
			draw_circle(c, 20.0, steel)
			draw_arc(c, 20.0, 0.0, TAU, 24, dark, 2.0)
			draw_rect(Rect2(c.x - 3.0, c.y - 30.0, 6.0, 12.0), dark)
			var hot := Color(0.9, 0.35, 0.2, a) if (r["alert"] or game.reactor_rate > 100) else Color(0.3, 0.7, 0.4, a)
			draw_circle(c, 6.0, Color(0.1, 0.1, 0.12, a))
			draw_line(c, c + Vector2(0, -5).rotated(deg_to_rad(float(game.reactor_rate - 90) * 2.5)), hot, 2.0)
		"refine":
			for i in 2:
				var x := o.x + w - 16.0 - i * 14.0
				draw_rect(Rect2(x, o.y + 40.0, 10.0, h - 46.0), steel)
				draw_rect(Rect2(x, o.y + 40.0, 10.0, 4.0), dark)
			var full := clampf(game.outbox / maxf(1.0, Game.OUTBOX_CAP * game.rooms_of("refine").size()), 0.0, 1.0)
			draw_rect(Rect2(o.x + 4.0, o.y + h - 8.0, (w - 8.0) * full, 4.0), Color(0.85, 0.6, 0.25, a + 0.2))
		"warehouse":
			var n := mini(12, int(ceil(game.stock / 5.0)))
			for i in n:
				var p := o + Vector2(w - 12.0 - (i % 4) * 13.0, h - 12.0 - (i / 4) * 13.0)
				draw_circle(p, 5.5, Color(0.25, 0.45, 0.62, a + 0.15))
				draw_arc(p, 5.5, 0.0, TAU, 12, dark, 1.0)
		"rest":
			draw_rect(Rect2(o.x + 8.0, o.y + h - 20.0, w - 16.0, 12.0), Color(0.55, 0.42, 0.32, a))
			draw_rect(Rect2(o.x + 8.0, o.y + h - 28.0, w - 16.0, 8.0), Color(0.45, 0.34, 0.26, a))
		"qc":
			draw_rect(Rect2(o.x + 6.0, o.y + h - 18.0, w - 12.0, 8.0), steel)
			draw_circle(o + Vector2(w * 0.5, h - 24.0), 4.0, Color(0.4, 0.75, 0.6, a))
		"smoke":
			draw_rect(Rect2(o.x + w * 0.5 - 5.0, o.y + h - 26.0, 10.0, 16.0), steel)
		"purify":
			for i in 5:
				draw_line(o + Vector2(20.0 + i * 40.0, 40.0), o + Vector2(40.0 + i * 40.0, h - 8.0), Color(0.7, 0.55, 0.2, 0.35), 3.0)


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT:
		for id in game.rooms:
			var rect: Rect2 = game.rooms[id]["rect"]
			if rect.has_point(event.position):
				room_clicked.emit(id)
				accept_event()
				return
		for z in Game.ZONE_ORDER:
			if game.zones[z]["open"] and corridor_rect(z).has_point(event.position):
				corridor_clicked.emit(z)
				accept_event()
				return
