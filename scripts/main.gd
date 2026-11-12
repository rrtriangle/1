extends Node
## 화면과 흐름: 교대 회의 → 근무(무전) → 보고서 → 다음 회의. 7일마다 주간 결산.

const PANEL_BG := Color(0.11, 0.115, 0.135)
const TEXT := Color(0.9, 0.9, 0.93)
const DIM := Color(0.6, 0.62, 0.68)
const TONE_COL := {"good": Color(0.55, 0.95, 0.6), "bad": Color(1.0, 0.5, 0.45), "perfect": Color(1.0, 0.85, 0.3), "info": Color(0.85, 0.88, 0.95)}

var sim: Sim
var sfx: Sfx
var font: SystemFont
var ui: Control
var world: WorldView
var top_label: RichTextLabel
var speed_label: Label
var notebook_label: RichTextLabel
var log_box: RichTextLabel
var roster_box: VBoxContainer
var roster_buttons := {}
var hint_label: Label
var modal: Control
var modal_box: VBoxContainer
var modal_scroll: ScrollContainer

var phase := "meeting"        # meeting, shift, over
var paused := false
var speed := 1
var radio_open := false
var current_radio: Dictionary = {}
var refresh_acc := 0.0

# 회의에서 고른 계획
var plan_posts := {}
var plan_holds: Array = []
var plan_calls: Array = []
var plan_warn: Label
var last_report := {}
var title_shown := false


func _ready() -> void:
	sim = Sim.new()
	sim.setup()
	sim.log_line.connect(_log)
	sim.sound.connect(_on_sound)
	sfx = Sfx.new()
	add_child(sfx)
	_build_ui()
	sim.popup.connect(world.add_popup)
	sim.highlight.connect(world.focus)
	_open_meeting()


func _process(delta: float) -> void:
	refresh_acc += delta
	if refresh_acc > 0.2:
		refresh_acc = 0.0
		_refresh()
	if phase != "shift" or paused or radio_open or modal.visible:
		return
	sim.advance(delta * speed * 60.0 / Defs.HOUR_SEC)
	if not sim.radio_queue.is_empty():
		_show_radio(sim.pop_radio())
	elif sim.shift_done:
		_on_shift_done()


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo:
		match event.keycode:
			KEY_SPACE:
				if phase == "shift" and not radio_open:
					paused = not paused
			KEY_1:
				speed = 1
			KEY_2:
				speed = 2
			KEY_3:
				speed = 3
			KEY_4:
				speed = 6
			KEY_ESCAPE:
				world.selected = null


# ================================================================ 화면 만들기

func _build_ui() -> void:
	font = SystemFont.new()
	font.font_names = PackedStringArray(["Malgun Gothic", "맑은 고딕", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "sans-serif"])
	var theme := Theme.new()
	theme.default_font = font
	theme.default_font_size = 16
	var layer := CanvasLayer.new()
	add_child(layer)
	ui = Control.new()
	ui.theme = theme
	ui.set_anchors_preset(Control.PRESET_FULL_RECT)
	layer.add_child(ui)
	var bg := ColorRect.new()
	bg.color = Color(0.07, 0.075, 0.09)
	bg.set_anchors_preset(Control.PRESET_FULL_RECT)
	ui.add_child(bg)

	# 위: 날짜·시각·돈·배속
	var top := HBoxContainer.new()
	top.position = Vector2(10, 6)
	top.size = Vector2(1900, 40)
	top.add_theme_constant_override("separation", 10)
	ui.add_child(top)
	top_label = RichTextLabel.new()
	top_label.bbcode_enabled = true
	top_label.fit_content = true
	top_label.scroll_active = false
	top_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	top_label.add_theme_font_size_override("normal_font_size", 20)
	top_label.add_theme_font_size_override("bold_font_size", 20)
	top.add_child(top_label)
	speed_label = Label.new()
	speed_label.add_theme_color_override("font_color", Color(0.6, 0.8, 1.0))
	top.add_child(speed_label)
	for pair in [["멈춤", 0], ["1x", 1], ["2x", 2], ["3x", 3]]:
		var b := Button.new()
		b.text = pair[0]
		b.focus_mode = Control.FOCUS_NONE
		b.pressed.connect(_on_speed_button.bind(pair[1]))
		top.add_child(b)
	var snd := CheckBox.new()
	snd.text = "소리"
	snd.button_pressed = true
	snd.focus_mode = Control.FOCUS_NONE
	snd.toggled.connect(func(on: bool) -> void:
		sfx.enabled = on
		sfx.set_night(on and sim.is_night()))
	top.add_child(snd)

	# 왼쪽: 직원 명단
	var left := ScrollContainer.new()
	left.position = Vector2(8, 54)
	left.size = Vector2(362, 1020)
	left.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	ui.add_child(left)
	roster_box = VBoxContainer.new()
	roster_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	roster_box.add_theme_constant_override("separation", 2)
	left.add_child(roster_box)

	# 가운데: 맵
	world = WorldView.new()
	world.sim = sim
	world.font = font
	world.position = Vector2(378, 54)
	world.size = Defs.MAP_SIZE
	world.room_clicked.connect(_on_room_clicked)
	world.person_clicked.connect(_on_person_pressed)
	ui.add_child(world)
	hint_label = Label.new()
	hint_label.position = Vector2(378, 990)
	hint_label.size = Vector2(1140, 80)
	hint_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint_label.add_theme_color_override("font_color", DIM)
	hint_label.add_theme_font_size_override("font_size", 15)
	ui.add_child(hint_label)

	# 오른쪽: 수첩과 무전 기록
	var right := VBoxContainer.new()
	right.position = Vector2(1528, 54)
	right.size = Vector2(384, 1020)
	right.add_theme_constant_override("separation", 6)
	ui.add_child(right)
	var nb_title := Label.new()
	nb_title.text = "공장장 수첩"
	nb_title.add_theme_font_size_override("font_size", 18)
	nb_title.add_theme_color_override("font_color", Color(0.95, 0.85, 0.55))
	right.add_child(nb_title)
	notebook_label = RichTextLabel.new()
	notebook_label.bbcode_enabled = true
	notebook_label.custom_minimum_size = Vector2(380, 400)
	notebook_label.add_theme_font_size_override("normal_font_size", 15)
	notebook_label.add_theme_font_size_override("bold_font_size", 15)
	right.add_child(notebook_label)
	var lg := Label.new()
	lg.text = "무전·사건 기록"
	lg.add_theme_font_size_override("font_size", 18)
	lg.add_theme_color_override("font_color", Color(0.7, 0.8, 0.95))
	right.add_child(lg)
	log_box = RichTextLabel.new()
	log_box.bbcode_enabled = true
	log_box.scroll_following = true
	log_box.size_flags_vertical = Control.SIZE_EXPAND_FILL
	log_box.add_theme_font_size_override("normal_font_size", 14)
	right.add_child(log_box)

	# 모달 (회의·무전·결산)
	modal = Control.new()
	modal.set_anchors_preset(Control.PRESET_FULL_RECT)
	modal.mouse_filter = Control.MOUSE_FILTER_STOP
	modal.visible = false
	ui.add_child(modal)
	var dim := ColorRect.new()
	dim.color = Color(0, 0, 0, 0.55)
	dim.set_anchors_preset(Control.PRESET_FULL_RECT)
	modal.add_child(dim)
	var center := CenterContainer.new()
	center.set_anchors_preset(Control.PRESET_FULL_RECT)
	modal.add_child(center)
	var panel := PanelContainer.new()
	var sb := StyleBoxFlat.new()
	sb.bg_color = PANEL_BG
	sb.border_color = Color(0.35, 0.37, 0.42)
	sb.set_border_width_all(2)
	sb.set_content_margin_all(22)
	panel.add_theme_stylebox_override("panel", sb)
	center.add_child(panel)
	modal_scroll = ScrollContainer.new()
	modal_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	panel.add_child(modal_scroll)
	modal_box = VBoxContainer.new()
	modal_box.add_theme_constant_override("separation", 10)
	modal_scroll.add_child(modal_box)


func _open_modal(width: float, height: float) -> void:
	for c in modal_box.get_children():
		c.queue_free()
	modal_scroll.custom_minimum_size = Vector2(width, height)
	modal_box.custom_minimum_size = Vector2(width - 20, 0)
	modal_scroll.scroll_vertical = 0
	modal.visible = true


## 무전처럼 짧은 창은 내용 높이에 맞춘다
func _fit_modal(width: float) -> void:
	await get_tree().process_frame
	var h := modal_box.get_combined_minimum_size().y + 8.0
	modal_scroll.custom_minimum_size = Vector2(width, minf(h, 940.0))


func _close_modal() -> void:
	modal.visible = false
	for c in modal_box.get_children():
		c.queue_free()


func _label(parent: Control, text: String, size: int = 16, col: Color = TEXT) -> Label:
	var l := Label.new()
	l.text = text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	l.add_theme_font_size_override("font_size", size)
	l.add_theme_color_override("font_color", col)
	l.custom_minimum_size = Vector2(200, 0)
	parent.add_child(l)
	return l


func _rich(parent: Control, text: String, size: int = 16) -> RichTextLabel:
	var r := RichTextLabel.new()
	r.bbcode_enabled = true
	r.fit_content = true
	r.scroll_active = false
	r.add_theme_font_size_override("normal_font_size", size)
	r.add_theme_font_size_override("bold_font_size", size)
	r.text = text
	parent.add_child(r)
	return r


func _button(parent: Control, text: String, cb: Callable, disabled: bool = false) -> Button:
	var b := Button.new()
	b.text = text
	b.disabled = disabled
	b.focus_mode = Control.FOCUS_NONE
	b.alignment = HORIZONTAL_ALIGNMENT_LEFT
	b.pressed.connect(cb)
	parent.add_child(b)
	return b


func _sep(parent: Control) -> void:
	var s := HSeparator.new()
	parent.add_child(s)


# ================================================================ 화면 갱신

func _refresh() -> void:
	var tone := "[color=#9cf]주간[/color]" if not sim.is_night() else "[color=#b9f]야간[/color]"
	var l := sim.leader_of(sim.crew_now())
	var wp := sim.week_income - sim.week_cost
	top_label.text = "[b]%d일차 %s %s[/b]  ·  %d조 근무 (%s)   ·   예산 [b]%s[/b]   ·   이번 주 %s%s" % [
		sim.day, tone, sim.clock_text(), sim.crew_now(), l.name if l else "-",
		_money(sim.money), "+" if wp >= 0 else "", _money(wp)]
	if phase == "shift":
		speed_label.text = "멈춤 (스페이스)" if paused else "%d배속" % speed
	else:
		speed_label.text = "회의 중"
	notebook_label.text = "\n".join(sim.notebook())
	if world.selected != null:
		hint_label.text = "%s 선택됨. 방을 누르면 그 방으로 보낸다 (불 꺼진 방이면 가서 켠다). 오른쪽 클릭이나 Esc로 취소." % world.selected.name
	elif phase == "shift":
		hint_label.text = "왼쪽 명단이나 맵에서 사람을 누른 뒤 방을 누르면 직접 지시. 쉬는 사람을 누르면 호출. 방 위에 마우스를 올리면 설명. 스페이스 멈춤 · 1·2·3 배속"
	_refresh_roster()


func _money(v: float) -> String:
	var n := int(round(v))
	var s := str(absi(n))
	var out := ""
	while s.length() > 3:
		out = "," + s.substr(s.length() - 3) + out
		s = s.substr(0, s.length() - 3)
	return ("-" if n < 0 else "") + s + out


func _refresh_roster() -> void:
	if roster_buttons.is_empty():
		_build_roster()
	var now := sim.now()
	for p in roster_buttons:
		var b: Button = roster_buttons[p]
		var person: Person = p
		var st: String = ["", " · 다크서클", " · 초점 없는 눈", " · 검은 무늬", " · 변이"][person.stage()]
		var where := person.status_text(now)
		if person.injured_until_day > sim.day:
			where = "부상 (%d일차까지 쉼)" % person.injured_until_day
		b.text = "%s  %s\n   피로 %d · 스트레스 %d · 오염 %d%s" % [person.name, where, person.fatigue, person.stress, person.contam, st]
		var on := person.on_site
		b.modulate = Color(1, 1, 1) if on else Color(0.62, 0.62, 0.68)
		if person == world.selected:
			b.modulate = Color(1.0, 0.95, 0.6)
		if person.gone:
			b.modulate = Color(0.5, 0.35, 0.35)
	for c in [1, 2, 3]:
		var h: Label = roster_box.get_node_or_null("crew%d" % c)
		if h != null:
			var duty := Defs.duty_of(c, sim.day)
			var names := {"day": "오늘 주간", "night": "오늘 야간", "off": "오늘 휴무"}
			var nxt := Defs.duty_of(c, sim.day + 1)
			var names2 := {"day": "내일 주간", "night": "내일 야간", "off": "내일 휴무"}
			var mark := "▶ " if c == sim.crew_now() else ""
			h.text = "%s%d조 · %s · %s" % [mark, c, names[duty], names2[nxt]]
			h.add_theme_color_override("font_color", Color(1.0, 0.9, 0.55) if c == sim.crew_now() else DIM)


func _build_roster() -> void:
	for c in [1, 2, 3, 0]:
		var h := Label.new()
		h.name = "crew%d" % c
		h.text = "상근 (낮에만)" if c == 0 else "%d조" % c
		h.add_theme_font_size_override("font_size", 16)
		h.add_theme_color_override("font_color", DIM)
		roster_box.add_child(h)
		for p in sim.people:
			if p.crew != c:
				continue
			var b := Button.new()
			b.alignment = HORIZONTAL_ALIGNMENT_LEFT
			b.focus_mode = Control.FOCUS_NONE
			b.add_theme_font_size_override("font_size", 14)
			b.custom_minimum_size = Vector2(350, 46)
			b.tooltip_text = _person_tip(p)
			b.pressed.connect(_on_person_pressed.bind(p))
			roster_box.add_child(b)
			roster_buttons[p] = b


func _person_tip(p: Person) -> String:
	var s := "%s (%d세, %s)\n숙련 %d · 관찰 %d · 담력 %d" % [p.name, p.age, p.role, p.skill, p.observe, p.nerve]
	for t in p.traits:
		s += "\n%s: %s" % [t, Defs.TRAIT_INFO.get(t, "")]
	s += "\n\n피로 70 이상: 능력 −20%\n스트레스 100: 붕괴 (그 교대 일 못 함)\n오염: 25 다크서클 · 50 초점 없는 눈 · 75 검은 무늬 · 100 변이"
	return s


func _log(text: String) -> void:
	log_box.append_text("[color=gray]%s[/color] %s\n" % [sim.clock_text(), text])


func _on_sound(n: String) -> void:
	match n:
		"night":
			sfx.set_night(true)
		"day":
			sfx.set_night(false)
		"radio":
			sfx.play("alert", -12.0)
		_:
			sfx.play(n)


func _on_speed_button(v: int) -> void:
	if v == 0:
		if phase == "shift":
			paused = not paused
	else:
		speed = v
		paused = false


# ================================================================ 직접 지시·호출

func _on_person_pressed(p: Person) -> void:
	if p == null:
		world.selected = null
		return
	if phase != "shift" or radio_open:
		return
	if p.gone:
		return
	if p.on_site:
		world.selected = null if world.selected == p else p
		return
	# 쉬는 사람: 호출
	if p.is_staff() and sim.is_night() and p.role != "정비원":
		_log("%s은(는) 상근직이라 밤에는 부르지 않는다." % p.name)
		return
	if p.arrive_at >= 0:
		_log("%s은(는) 이미 오는 중이다." % p.name)
		return
	if not sim.available(p):
		_log("%s은(는) 다쳐서 쉬는 중이다." % p.name)
		return
	paused = true
	_open_modal(640, 0)
	_label(modal_box, "%s 호출" % p.name, 22, Color(0.95, 0.85, 0.55))
	_label(modal_box, "집에서 쉬고 있는 %s을(를) 부른다. 약 %d분 뒤 정문을 지나고, 거기서 걸어 들어온다.\n피로 %d → %d · 위반 기록 +1 · 수당 −%d" % [
		p.name, Defs.CALL_TRAVEL_MIN, p.fatigue, p.fatigue + Defs.HOLD_FATIGUE, Defs.CALL_PAY])
	var row := HBoxContainer.new()
	modal_box.add_child(row)
	var post := "patrol" if sim.is_night() else "assist"
	_button(row, "부른다 (%s)" % Defs.POST_NAMES[post], func() -> void:
		sim._call_in(p, "공장장 지시", Defs.CALL_TRAVEL_MIN, post)
		_close_modal()
		paused = false)
	_button(row, "그만둔다", func() -> void:
		_close_modal()
		paused = false)
	_fit_modal(640)


func _on_room_clicked(id: String) -> void:
	if phase != "shift" or radio_open:
		return
	if world.selected == null:
		return
	var msg := sim.order(world.selected, id)
	_log(msg)
	world.selected = null


# ================================================================ 무전

func _show_radio(ev: Dictionary) -> void:
	if ev.is_empty():
		return
	radio_open = true
	current_radio = ev
	world.focus(ev.get("room", ""))
	_open_modal(760, 0)
	var kind: String = ev["kind"]
	var head := "무전 · %s · %s" % [ev["clock"], ev["speaker"]]
	if kind == "tutorial":
		head = "%s · %s" % [ev["speaker"], ev["clock"]]
	_label(modal_box, head, 18, Color(0.6, 0.8, 1.0) if kind != "tutorial" else Color(0.95, 0.85, 0.55))
	var col: Color = TEXT
	if ev.has("tone"):
		col = TONE_COL.get(ev["tone"], TEXT)
	_label(modal_box, ev["text"], 20, col)
	_sep(modal_box)
	for i in ev["choices"].size():
		var c: Dictionary = ev["choices"][i]
		var label: String = c["label"]
		if c["pct"] >= 0.0:
			label += "   — 성공 %d%%" % int(c["pct"])
		var b := _button(modal_box, label, _choose.bind(i), c.get("disabled", false))
		b.add_theme_font_size_override("font_size", 18)
		b.custom_minimum_size = Vector2(700, 40)
		if c["why"] != "":
			_label(modal_box, "      " + c["why"], 14, DIM)
	_fit_modal(760)


func _choose(i: int) -> void:
	var ev := current_radio
	var c: Dictionary = ev["choices"][i]
	var r := sim.resolve(ev, i)
	if not ev["kind"] in ["tutorial", "result", "research"]:
		_log("[color=#9cf]→ %s[/color]" % c["label"])
	if r["tone"] == "none" or r["text"] == "":
		_close_radio()
		return
	_open_modal(760, 0)
	var big := {"good": "성공", "bad": "실패", "perfect": "★ 완벽한 차단", "info": "지시"}
	_label(modal_box, big.get(r["tone"], ""), 30 if r["tone"] == "perfect" else 24, TONE_COL.get(r["tone"], TEXT))
	_label(modal_box, r["text"], 19, TEXT)
	_log("[color=%s]%s[/color]" % [TONE_COL.get(r["tone"], TEXT).to_html(false), r["text"].replace("\n", " ")])
	_button(modal_box, "계속", _close_radio)
	_fit_modal(760)
	if r["tone"] == "good":
		sfx.play("ok")
	elif r["tone"] == "bad":
		sfx.play("bad")


func _close_radio() -> void:
	_close_modal()
	radio_open = false
	current_radio = {}
	if not sim.radio_queue.is_empty():
		_show_radio(sim.pop_radio())


# ================================================================ 교대 끝·회의

func _on_shift_done() -> void:
	last_report = sim.shift_report()
	var was_night := sim.is_night()
	var week := sim.advance_to_next_shift()
	phase = "meeting"
	world.selected = null
	sfx.set_night(false)
	if sim.finished:
		_end_screen(week)
		return
	if was_night and sim.day == Defs.PROLOGUE_DAYS + 1 and not title_shown:
		title_shown = true
		_title_card(week)
		return
	if not week.is_empty():
		_week_screen(week)
		return
	_open_meeting()


func _title_card(week: Dictionary) -> void:
	_open_modal(800, 420)
	_label(modal_box, "", 20)
	_label(modal_box, "박 씨: \"고생하셨어요. 이제 좀 조용할 거예요.\"", 22, Color(0.85, 0.85, 0.95))
	_label(modal_box, "", 20)
	_label(modal_box, "야간 근무", 56, Color(0.95, 0.85, 0.55))
	_label(modal_box, "프롤로그 끝. 여기서부터 1주차입니다. 이제 안내 없이 무전이 옵니다.", 18, DIM)
	sfx.play("week")
	_button(modal_box, "계속", func() -> void:
		if week.is_empty():
			_open_meeting()
		else:
			_week_screen(week))


func _week_screen(week: Dictionary) -> void:
	_open_modal(800, 520)
	var profit: float = week["profit"]
	_label(modal_box, "%d주차 결산" % week["week"], 28, Color(0.95, 0.85, 0.55))
	_label(modal_box, "들어온 돈 +%s\n나간 돈 −%s\n손익 %s%s" % [_money(week["income"]), _money(week["cost"]), "+" if profit >= 0 else "", _money(profit)], 20)
	if profit >= 0:
		_label(modal_box, "흑자. 본사는 조용하다.", 20, TONE_COL["good"])
	else:
		_label(modal_box, "적자. 원래는 다음 주에 본사 감사관이 온다. (감사는 v0.7에서 다시 만든다. 이번 데모에서는 오지 않는다)", 20, TONE_COL["bad"])
	if week["violations"] > 0:
		_label(modal_box, "근무표 위반 기록 %d건: 대타(붙잡기·호출)를 쓸 때마다 남는다. 감사가 오면 이 장부를 본다." % week["violations"], 17, DIM)
	sfx.play("week")
	_button(modal_box, "다음 회의로", _open_meeting)


func _end_screen(week: Dictionary) -> void:
	phase = "over"
	_open_modal(900, 700)
	_label(modal_box, "데모 v0.6 끝 (%d일)" % Defs.DEMO_LAST_DAY, 30, Color(0.95, 0.85, 0.55))
	var lines: Array = []
	lines.append("예산 %s (시작 %s)" % [_money(sim.money), _money(Defs.START_MONEY)])
	if not week.is_empty():
		lines.append("마지막 주 손익 %s" % _money(week["profit"]))
	lines.append("'한 명 더' 적발 %d번 · 역압 막음 %d번 / 사고 %d번 · ★ 완벽한 차단 %d번" % [sim.stats["detected"], sim.stats["pressure_ok"], sim.stats["pressure_bad"], sim.stats["perfect"]])
	lines.append("대타: 호출 %d번 · 붙잡기 %d번 (위반 기록 %d)" % [sim.stats["calls"], sim.stats["holds"], sim.violations])
	lines.append("연구: 한 명 더 %d단계 · 역압 %d단계" % [sim.research["extra"], sim.research["pressure"]])
	lines.append("박 씨 말이 맞은 횟수 %d / 틀린 횟수 %d" % [sim.stats["park_right"], sim.stats["park_wrong"]])
	var gone := sim.people.filter(func(p: Person) -> bool: return p.gone)
	if gone.size() > 0:
		lines.append("사라진 사람: " + ", ".join(gone.map(func(p: Person) -> String: return p.name)))
	_label(modal_box, "\n".join(lines), 19)
	_label(modal_box, "\n플레이해 주셔서 고맙습니다. 무엇이 이해가 안 됐는지, 어디서 지루했는지 알려 주세요.", 17, DIM)


func _open_meeting() -> void:
	phase = "meeting"
	paused = false
	var d := sim.day
	var sh := sim.shift
	var c := sim.crew_now()
	var leader := sim.leader_of(c)
	_open_modal(1180, 900)
	_label(modal_box, "%d일차 %s 교대 회의" % [d, "야간" if sh == "night" else "주간"], 28, Color(0.95, 0.85, 0.55))
	_label(modal_box, "이번 근무: %d조 (%s)   ·   %s" % [c, leader.name if leader else "-", "20:00 ~ 08:00" if sh == "night" else "08:00 ~ 20:00"], 18, DIM)
	var tip := _meeting_tip(d, sh)
	if tip != "":
		var tl := _label(modal_box, tip, 17, Color(0.95, 0.85, 0.55))
		tl.add_theme_color_override("font_color", Color(0.95, 0.85, 0.55))

	# 지난 교대 보고서
	if not last_report.is_empty():
		_sep(modal_box)
		_label(modal_box, "지난 교대 보고서", 20, Color(0.7, 0.8, 0.95))
		var inc: float = last_report["income"]
		var cost: float = last_report["cost"]
		_rich(modal_box, "들어온 돈 [color=#9f9]+%s[/color]   나간 돈 [color=#f96]−%s[/color]   →   [b]%s%s[/b]" % [_money(inc), _money(cost), "+" if inc - cost >= 0 else "", _money(inc - cost)], 18)
		var lg: Array = last_report["log"]
		if lg.size() > 0:
			_label(modal_box, "· " + "\n· ".join(lg.slice(maxi(0, lg.size() - 12))), 15, TEXT)
		var ch: Array = last_report["people"]
		if ch.size() > 0:
			_label(modal_box, "눈여겨볼 사람: " + " / ".join(ch), 15, Color(1.0, 0.75, 0.6))
		_label(modal_box, "박 씨: \"%s\"" % last_report["park"], 16, Color(0.8, 0.8, 0.92))
		_label(modal_box, "결재: 공장장 ______ (인)", 14, DIM)

	# 근무표
	_sep(modal_box)
	_label(modal_box, "근무표 (6일 주기: 주간 2 · 야간 2 · 휴무 2)", 20, Color(0.7, 0.8, 0.95))
	var sched := ""
	for cc in [1, 2, 3]:
		var row := "%d조 %s   " % [cc, sim.leader_of(cc).name if sim.leader_of(cc) else ""]
		for k in 6:
			var du := Defs.duty_of(cc, d + k)
			var mark: String = {"day": "주", "night": "야", "off": "휴"}[du]
			row += ("[%s] " % mark) if k == 0 else (mark + " ")
		sched += row + "\n"
	_label(modal_box, sched.strip_edges() + "\n(맨 앞 [ ]가 오늘. 오늘부터 6일)", 16, TEXT)

	# 배치
	_sep(modal_box)
	_label(modal_box, "배치: 누가 어디서 일하나", 20, Color(0.7, 0.8, 0.95))
	plan_posts = sim.default_posts(c, sh)
	plan_holds.clear()
	plan_calls.clear()
	var post_ids := ["control", "reactor", "refine", "store", "assist", "rest"] if sh == "day" else ["control", "reactor", "refine", "store", "patrol", "rest"]
	for p in sim.crew_members(c):
		var row := HBoxContainer.new()
		modal_box.add_child(row)
		var nl := _label(row, "%s  (숙련 %d · 관찰 %d · 피로 %d)" % [p.name, p.skill, p.observe, p.fatigue], 16)
		nl.custom_minimum_size = Vector2(430, 0)
		if not sim.available(p):
			_label(row, "부상: %d일차까지 쉼" % p.injured_until_day, 16, TONE_COL["bad"])
			continue
		var ob := OptionButton.new()
		ob.focus_mode = Control.FOCUS_NONE
		for i in post_ids.size():
			ob.add_item(Defs.POST_NAMES[post_ids[i]], i)
		ob.selected = post_ids.find(plan_posts.get(p, "assist"))
		ob.item_selected.connect(func(i: int) -> void:
			plan_posts[p] = post_ids[i]
			_update_plan_warn(sh))
		row.add_child(ob)
	plan_warn = _label(modal_box, "", 16, Color(1.0, 0.7, 0.5))
	_update_plan_warn(sh)

	# 방침
	_sep(modal_box)
	_label(modal_box, "방침", 20, Color(0.7, 0.8, 0.95))
	var rrow := HBoxContainer.new()
	modal_box.add_child(rrow)
	_label(rrow, "반응기 가동률", 16).custom_minimum_size = Vector2(160, 0)
	var sl := HSlider.new()
	sl.min_value = 60
	sl.max_value = 120
	sl.step = 5
	sl.value = sim.rate
	sl.custom_minimum_size = Vector2(320, 24)
	sl.focus_mode = Control.FOCUS_NONE
	rrow.add_child(sl)
	var rl := _label(rrow, "", 16)
	rl.custom_minimum_size = Vector2(560, 0)
	var upd := func(v: float) -> void:
		sim.rate = int(v)
		rl.text = _rate_text(int(v))
	sl.value_changed.connect(upd)
	upd.call(sl.value)
	if sh == "night" and sim.research["extra"] >= 2:
		var pc := CheckBox.new()
		pc.text = "수칙 「2인 1조」: 밤에 반응실·정제실을 두 명씩 (창고는 빈다 → 정제실 출구가 막힌다)"
		pc.button_pressed = sim.pair_rule
		pc.focus_mode = Control.FOCUS_NONE
		pc.toggled.connect(func(on: bool) -> void:
			sim.pair_rule = on
			plan_posts = sim.default_posts(c, sh)
			_open_meeting_keep())
		modal_box.add_child(pc)
	if sh == "day":
		var trow := HBoxContainer.new()
		modal_box.add_child(trow)
		_label(trow, "오늘 연구 주제", 16).custom_minimum_size = Vector2(160, 0)
		var tob := OptionButton.new()
		tob.focus_mode = Control.FOCUS_NONE
		tob.add_item("한 명 더 (%d/3단계)" % sim.research["extra"], 0)
		tob.add_item("역압 (%d/3단계)" % sim.research["pressure"], 1)
		tob.selected = 0 if sim.research_target == "extra" else 1
		tob.item_selected.connect(func(i: int) -> void: sim.research_target = "extra" if i == 0 else "pressure")
		trow.add_child(tob)
		_label(trow, "  관찰 기록 %d개 (밤에 이상한 일을 겪거나 조사하면 쌓인다)" % sim.records, 15, DIM)

	# 대타
	_sep(modal_box)
	_label(modal_box, "대타 (급할 때만: 위반 기록 +1, 수당 −%d, 피로 +%d)" % [Defs.CALL_PAY, Defs.HOLD_FATIGUE], 20, Color(0.7, 0.8, 0.95))
	var outgoing := sim.people.filter(func(q: Person) -> bool: return q.on_site and q.on_duty and not q.is_staff() and q.crew != c and not q.gone)
	if outgoing.size() > 0:
		_label(modal_box, "붙잡기: 퇴근하는 사람을 남긴다 (바로 일할 수 있지만 이미 지쳐 있다)", 15, DIM)
		var fl := HFlowContainer.new()
		modal_box.add_child(fl)
		for q in outgoing:
			var cb := CheckBox.new()
			cb.text = "%s (피로 %d)" % [q.name, q.fatigue]
			cb.focus_mode = Control.FOCUS_NONE
			cb.toggled.connect(func(on: bool) -> void:
				if on:
					plan_holds.append(q)
				else:
					plan_holds.erase(q))
			fl.add_child(cb)
	var off := sim.callable_people().filter(func(q: Person) -> bool: return q.crew != c)
	if off.size() > 0:
		_label(modal_box, "미리 부르기: 쉬는 조에서 한 명 더 나오게 한다 (교대 시작과 함께 온다)", 15, DIM)
		var fl2 := HFlowContainer.new()
		modal_box.add_child(fl2)
		for q in off.slice(0, 8):
			var cb2 := CheckBox.new()
			cb2.text = "%s (%d조, 피로 %d)" % [q.name, q.crew, q.fatigue]
			cb2.focus_mode = Control.FOCUS_NONE
			cb2.toggled.connect(func(on: bool) -> void:
				if on:
					plan_calls.append(q)
				else:
					plan_calls.erase(q))
			fl2.add_child(cb2)

	_sep(modal_box)
	var go := _button(modal_box, "결재하고 근무 시작", _start_shift)
	go.add_theme_font_size_override("font_size", 22)
	go.custom_minimum_size = Vector2(400, 50)


func _open_meeting_keep() -> void:
	call_deferred("_open_meeting")


func _rate_text(v: int) -> String:
	var s := "%d%%" % v
	if v <= 80:
		s += "  · 안전 (역압 없음)"
	else:
		s += "  · 80% 초과: 역압 위험"
		if sim.research["pressure"] >= 2:
			var k := Defs.PRESSURE_K * pow((v - 80) / 20.0, 2.0) * Defs.PRESSURE_NIGHT * sim.pressure_mult
			s += " (밤 시간당 %.1f%%)" % (k * 100.0)
	var per_day := v / 100.0 * 24.0
	s += "  · 하루 약 %d드럼 → 약 %s" % [per_day, _money(per_day * (Defs.SELL_PRICE - Defs.RAW_PRICE))]
	return s


func _update_plan_warn(sh: String) -> void:
	if plan_warn == null:
		return
	var counts := {}
	for p in plan_posts:
		counts[plan_posts[p]] = counts.get(plan_posts[p], 0) + 1
	var w: Array = []
	if counts.get("reactor", 0) == 0:
		w.append("반응실이 비었다: 생산이 없다")
	if counts.get("refine", 0) == 0 and counts.get("assist", 0) == 0:
		w.append("정제실이 비었다: 드럼이 안 나온다")
	if counts.get("store", 0) == 0:
		w.append("창고가 비었다: 정제실 출구(4드럼)가 차면 막힌다" + (", 트럭에 실을 사람이 없다" if sh == "day" else ""))
	if sh == "night":
		var alone: Array = []
		for id in ["reactor", "refine", "store"]:
			if counts.get(id, 0) == 1:
				alone.append(Defs.ROOMS[id]["name"])
		if alone.size() > 0:
			w.append("밤에 혼자 있는 방: %s ('한 명 더'가 생기는 조건)" % ", ".join(alone))
		if counts.get("patrol", 0) == 0:
			w.append("순찰이 없다: 정전 때 불을 켜러 갈 사람이 없다")
	plan_warn.text = "\n".join(w.map(func(x: String) -> String: return "! " + x))


func _meeting_tip(d: int, sh: String) -> String:
	var tips := {
		"1day": "첫 근무입니다. 기본 배치 그대로 \"결재하고 근무 시작\"을 누르세요. 김 반장이 무전으로 안내합니다.",
		"1night": "밤입니다. 낮에 일한 1조는 퇴근하고 2조가 들어옵니다. 순찰 한 명이 공장을 돕니다.\n밤에 혼자 있는 방은 위험합니다. 아래 경고를 보세요.",
		"2day": "아침 회의입니다. 지난밤 보고서를 보고 오늘 낮 계획을 정합니다. 연구 주제도 여기서 고릅니다.",
		"2night": "가동률을 80% 넘게 올리면 돈을 더 벌지만 역압이 잘 옵니다. 밤엔 1.5배.",
		"3day": "오늘은 3조입니다. 근무표 표에서 조마다 언제 일하는지 볼 수 있습니다.",
		"3night": "대타: 사람이 모자라면 퇴근하는 사람을 붙잡거나, 쉬는 사람을 부를 수 있습니다. 근무 중에도 왼쪽 명단에서 쉬는 사람을 누르면 부를 수 있습니다.",
	}
	return tips.get("%d%s" % [d, sh], "")


func _start_shift() -> void:
	# 붙잡은 사람·미리 부른 사람의 자리: 주간은 정제 보조, 야간은 순찰
	var extra_post := "assist" if sim.shift == "day" else "patrol"
	for q in plan_holds:
		if not plan_posts.has(q):
			plan_posts[q] = extra_post
	var plan := {"posts": plan_posts.duplicate(), "holds": plan_holds.duplicate(), "calls": [], "rate": sim.rate,
		"pair": sim.pair_rule, "target": sim.research_target}
	sim.begin_shift(plan)
	for q in plan_calls:
		sim._call_in(q, "회의에서 미리 부름", 0, extra_post)
	sfx.play("stamp")
	_close_modal()
	last_report = {}
	phase = "shift"
	paused = false
	_log("[color=#fc6]── %d일차 %s 시작 · %d조 ──[/color]" % [sim.day, "야간" if sim.is_night() else "주간", sim.crew_now()])
