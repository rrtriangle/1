extends Node
## 화면 구성과 게임 흐름 (교대 회의 → 근무 → 보고서 → 주간 결산).

const HOUR_SEC := 2.5  # 1배속에서 게임 1시간 = 2.5초 (한 교대 30초)

var game: Game
var phase: String = "meeting"  # errand, meeting, shift, report, over
var paused: bool = true
var speed: int = 1
var acc: float = 0.0
var font: SystemFont

var ui: Control
var world: WorldView
var alert_queue: Array = []   # [text, kind]
var showing_alert: bool = false
var top_label: Label
var top_label2: Label
var speed_label: Label
var log_box: RichTextLabel
var staff_list: VBoxContainer
var staff_labels: Dictionary = {}
var rate_label: Label
var rate_slider: HSlider

var modal: Control
var modal_panel: PanelContainer
var modal_box: VBoxContainer

var plan: Dictionary = {}  # Staff -> 방 id (회의에서 고른 배치)
var meeting_tab: int = 0
var sfx: Sfx
var risk_label: Label
var warn_label: Label


func _ready() -> void:
	game = Game.new()
	game.alert.connect(_on_alert)
	game.log_line.connect(_log)
	sfx = Sfx.new()
	add_child(sfx)
	_build_ui()
	_log("[color=gray]의산공장. %d일차 밤.[/color]" % game.day)
	_log("박 씨: \"새 공장장님이시구먼. 저번 분도 밤에 오셨지.\"")
	_log("[color=gray]물류 창고 불이 꺼져 있다. 전력이 모자라면 다른 불을 끄고 켜야 한다.[/color]")
	_begin_meeting()


func _process(delta: float) -> void:
	_move_staff(delta)
	world.queue_redraw()
	_refresh_hud()
	if phase != "shift" or paused:
		return
	acc += delta * speed
	while acc >= HOUR_SEC and phase == "shift" and not paused:
		acc -= HOUR_SEC
		game.tick_hour()
		if game.hour >= Game.HOURS_PER_SHIFT:
			if not showing_alert:
				_finish_shift()
			break


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo:
		match event.keycode:
			KEY_SPACE:
				if phase == "shift" and not modal.visible:
					paused = not paused
			KEY_1:
				_set_speed(1)
			KEY_2:
				_set_speed(2)
			KEY_3:
				_set_speed(3)


# ---------------------------------------------------------------- 화면 만들기

func _build_ui() -> void:
	font = SystemFont.new()
	font.font_names = PackedStringArray(["Malgun Gothic", "맑은 고딕", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "sans-serif"])
	var theme := Theme.new()
	theme.default_font = font
	theme.default_font_size = 14

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

	var root := VBoxContainer.new()
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.add_theme_constant_override("separation", 4)
	ui.add_child(root)

	var top := HBoxContainer.new()
	top.custom_minimum_size = Vector2(0, 40)
	top.add_theme_constant_override("separation", 12)
	root.add_child(top)
	var top_text := VBoxContainer.new()
	top_text.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	top_text.add_theme_constant_override("separation", 0)
	top.add_child(top_text)
	top_label = Label.new()
	top_label.add_theme_font_size_override("font_size", 13)
	top_label.clip_text = true
	top_text.add_child(top_label)
	top_label2 = Label.new()
	top_label2.add_theme_font_size_override("font_size", 12)
	top_label2.add_theme_color_override("font_color", Color(0.95, 0.70, 0.50))
	top_label2.clip_text = true
	top_text.add_child(top_label2)
	speed_label = Label.new()
	speed_label.add_theme_color_override("font_color", Color(0.6, 0.8, 1.0))
	top.add_child(speed_label)
	for pair in [["멈춤/재개", 0], ["1x", 1], ["2x", 2], ["3x", 3]]:
		var b := Button.new()
		b.text = pair[0]
		b.focus_mode = Control.FOCUS_NONE
		b.pressed.connect(_on_speed_button.bind(pair[1]))
		top.add_child(b)

	var mid := HBoxContainer.new()
	mid.size_flags_vertical = Control.SIZE_EXPAND_FILL
	mid.add_theme_constant_override("separation", 8)
	root.add_child(mid)

	var left_scroll := ScrollContainer.new()
	left_scroll.custom_minimum_size = Vector2(250, 0)
	left_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	mid.add_child(left_scroll)
	staff_list = VBoxContainer.new()
	staff_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	staff_list.add_theme_constant_override("separation", 2)
	left_scroll.add_child(staff_list)
	_rebuild_staff_list()

	world = WorldView.new()
	world.game = game
	world.font = font
	world.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	world.size_flags_vertical = Control.SIZE_EXPAND_FILL
	world.room_clicked.connect(_on_room_clicked)
	world.corridor_clicked.connect(_on_corridor_clicked)
	mid.add_child(world)

	log_box = RichTextLabel.new()
	log_box.bbcode_enabled = true
	log_box.scroll_following = true
	log_box.custom_minimum_size = Vector2(270, 0)
	log_box.add_theme_font_size_override("normal_font_size", 12)
	log_box.add_theme_font_size_override("bold_font_size", 12)
	mid.add_child(log_box)

	var bottom := HBoxContainer.new()
	bottom.custom_minimum_size = Vector2(0, 36)
	bottom.add_theme_constant_override("separation", 10)
	root.add_child(bottom)
	var rl := Label.new()
	rl.text = " 반응기 가동률"
	bottom.add_child(rl)
	rate_slider = HSlider.new()
	rate_slider.min_value = 60
	rate_slider.max_value = 120
	rate_slider.step = 10
	rate_slider.value = game.reactor_rate
	rate_slider.custom_minimum_size = Vector2(150, 0)
	rate_slider.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	rate_slider.value_changed.connect(_on_rate_changed)
	bottom.add_child(rate_slider)
	rate_label = Label.new()
	rate_label.custom_minimum_size = Vector2(150, 0)
	bottom.add_child(rate_label)
	var hint := Label.new()
	hint.text = "직원 선택 후 방 클릭: 이동 지시 (다른 구역이면 1시간 걸어간다) · 선택 없이 방·복도 클릭: 조명 · 스페이스 멈춤, 1·2·3 배속"
	hint.add_theme_font_size_override("font_size", 12)
	hint.add_theme_color_override("font_color", Color(0.6, 0.6, 0.66))
	hint.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hint.clip_text = true
	bottom.add_child(hint)
	var snd := CheckBox.new()
	snd.text = "소리"
	snd.button_pressed = true
	snd.focus_mode = Control.FOCUS_NONE
	snd.toggled.connect(func(on: bool) -> void:
		sfx.enabled = on
		sfx.set_night(on and phase == "shift" and game.is_night()))
	bottom.add_child(snd)

	# 모달: 어둡게 덮고 가운데에 불투명한 창
	modal = Control.new()
	modal.set_anchors_preset(Control.PRESET_FULL_RECT)
	modal.mouse_filter = Control.MOUSE_FILTER_STOP
	ui.add_child(modal)
	var dim := ColorRect.new()
	dim.color = Color(0, 0, 0, 0.62)
	dim.set_anchors_preset(Control.PRESET_FULL_RECT)
	modal.add_child(dim)
	var center := CenterContainer.new()
	center.set_anchors_preset(Control.PRESET_FULL_RECT)
	modal.add_child(center)
	modal_panel = PanelContainer.new()
	modal_panel.custom_minimum_size = Vector2(700, 0)
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.13, 0.14, 0.17)
	sb.border_color = Color(0.42, 0.44, 0.52)
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(6)
	sb.set_content_margin_all(14)
	modal_panel.add_theme_stylebox_override("panel", sb)
	center.add_child(modal_panel)
	modal_box = VBoxContainer.new()
	modal_box.add_theme_constant_override("separation", 6)
	modal_panel.add_child(modal_box)
	modal.visible = false


func _rebuild_staff_list() -> void:
	for c in staff_list.get_children():
		staff_list.remove_child(c)
		c.queue_free()
	staff_labels.clear()
	for s in game.staff:
		var b := Button.new()
		b.text = s.name
		b.alignment = HORIZONTAL_ALIGNMENT_LEFT
		b.focus_mode = Control.FOCUS_NONE
		b.pressed.connect(_on_staff_pressed.bind(s))
		staff_list.add_child(b)
		var l := Label.new()
		l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		l.custom_minimum_size = Vector2(240, 0)
		l.add_theme_font_size_override("font_size", 12)
		staff_list.add_child(l)
		staff_labels[s] = l


func _clear_modal() -> void:
	for c in modal_box.get_children():
		modal_box.remove_child(c)
		c.queue_free()


func _label(parent: Control, text: String, size: int = 14, col: Color = Color(0.9, 0.9, 0.93), width: float = 660.0) -> Label:
	var l := Label.new()
	l.text = text
	l.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	l.custom_minimum_size = Vector2(width, 0)
	l.add_theme_font_size_override("font_size", size)
	l.add_theme_color_override("font_color", col)
	parent.add_child(l)
	return l


func _modal_label(text: String, size: int = 14) -> Label:
	return _label(modal_box, text, size)


func _modal_rich(text: String) -> void:
	var sc := ScrollContainer.new()
	sc.custom_minimum_size = Vector2(700, 0)
	sc.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	var rt := RichTextLabel.new()
	rt.bbcode_enabled = true
	rt.fit_content = true
	rt.custom_minimum_size = Vector2(680, 0)
	for k in ["normal_font_size", "bold_font_size", "italics_font_size"]:
		rt.add_theme_font_size_override(k, 13)
	rt.text = text
	sc.add_child(rt)
	modal_box.add_child(sc)
	# 글이 길면 창이 화면을 넘지 않게 스크롤
	var lines := text.count("\n") + 1
	sc.custom_minimum_size.y = minf(520.0, lines * 21.0 + 16.0)


func _button(parent: Control, text: String, cb: Callable, disabled: bool = false) -> Button:
	var b := Button.new()
	b.text = text
	b.disabled = disabled
	b.pressed.connect(func() -> void: sfx.play("click", -14.0))
	b.pressed.connect(cb)
	parent.add_child(b)
	return b


func _flow(parent: Control) -> HFlowContainer:
	var f := HFlowContainer.new()
	f.add_theme_constant_override("h_separation", 6)
	f.add_theme_constant_override("v_separation", 4)
	parent.add_child(f)
	return f


# ---------------------------------------------------------------- 「심부름」과 교대 회의

func _begin_meeting() -> void:
	if game.errand_pending():
		_open_errand()
	else:
		_open_meeting()


func _open_errand() -> void:
	phase = "errand"
	paused = true
	_clear_modal()
	_modal_label("박 씨의 부탁", 20)
	_modal_label("박 씨: \"요즘 잠 못 자는 사람 있죠? 그 사람 보내세요. 금방 끝나요.\"", 15)
	_modal_label("보낸 사람은 오늘 밤 근무에서 빠집니다.", 12)
	var row := _flow(modal_box)
	var cands := game.errand_candidates()
	cands.sort_custom(func(a: Staff, b: Staff) -> bool: return a.name == "이 주임" and b.name != "이 주임")
	for s in cands:
		_button(row, "%s을(를) 보낸다" % s.name, _do_errand.bind(s))
	_button(row, "거절한다", _do_errand.bind(null))
	modal.visible = true


func _do_errand(s: Staff) -> void:
	var text := game.do_errand(s)
	_clear_modal()
	_modal_label(text, 15)
	_button(modal_box, "확인", _open_meeting)


func _open_meeting(keep_plan: bool = false) -> void:
	phase = "meeting"
	paused = true
	game.check_applicants()
	if keep_plan:
		for s in game.staff:
			if s.gone():
				plan.erase(s)
			elif not plan.has(s) or not s.can_be_assigned() or (plan[s] != "off" and not game.rooms.has(plan[s])):
				plan[s] = "off"
	else:
		plan = game.suggest_plan()
	_clear_modal()
	_modal_label("%d일차 %s조 교대 회의 · %d주차" % [game.day, game.shift, game.week()], 19)
	var status := "밤에는 이상현상이 본격적으로 나타납니다. 혼자 두는 방과 어두운 방을 조심하세요." if game.is_night() else "낮에는 생산과 출하에 집중하는 시간입니다. 트럭은 10시와 16시에 옵니다."
	if game.audit_stage != 0 or not game.regular_next.is_empty():
		status = game.audit_status_text()
		if game.in_audit():
			status += " — " + Game.AUDITOR_INFO[game.auditor["type"]]["watch"]
	_label(modal_box, status, 12, Color(0.95, 0.70, 0.50) if game.audit_stage != 0 else Color(0.7, 0.7, 0.76))

	var tabs := TabContainer.new()
	tabs.custom_minimum_size = Vector2(820, 400)
	modal_box.add_child(tabs)
	_tab_assign(_tab(tabs, "배치"))
	_tab_facility(_tab(tabs, "시설·전력"))
	_tab_people(_tab(tabs, "인사·근로시간"))
	_tab_hq(_tab(tabs, "본사·감사"))
	tabs.current_tab = clampi(meeting_tab, 0, 3)
	tabs.tab_changed.connect(func(i: int) -> void: meeting_tab = i)

	risk_label = _modal_label("", 13)
	warn_label = _label(modal_box, "", 13, Color(0.95, 0.55, 0.45))
	_update_plan_labels()
	_button(modal_box, "근무 시작", _start_shift)
	modal.visible = true


func _tab(tabs: TabContainer, title: String) -> VBoxContainer:
	var sc := ScrollContainer.new()
	sc.name = title
	sc.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	tabs.add_child(sc)
	var m := MarginContainer.new()
	m.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for side in ["margin_left", "margin_right", "margin_top", "margin_bottom"]:
		m.add_theme_constant_override(side, 8)
	sc.add_child(m)
	var v := VBoxContainer.new()
	v.add_theme_constant_override("separation", 5)
	m.add_child(v)
	return v


func _tab_assign(v: VBoxContainer) -> void:
	var ids: Array = ["off"]
	ids.append_array(game.assignable_rooms())
	var grid := GridContainer.new()
	grid.columns = 4
	grid.add_theme_constant_override("h_separation", 12)
	grid.add_theme_constant_override("v_separation", 3)
	v.add_child(grid)
	for s in game.staff:
		if s.gone():
			continue
		_label(grid, "%s (%s)" % [s.name, ", ".join(s.traits)], 13, Color(0.9, 0.9, 0.93), 190.0)
		_label(grid, "운영 %d 관찰 %d 정신 %d 체력 %d\n%s" % [s.operation, s.observation, s.mind, s.stamina, s.short_status()], 11, Color(0.72, 0.72, 0.78), 250.0)
		var chk := game.work_check(s)
		var wt := "주 %d/%d" % [s.shifts_week, game.legal_limit()]
		var wc := Color(0.6, 0.8, 0.6)
		if chk == "연장":
			wt += "\n연장수당"
			wc = Color(0.9, 0.8, 0.5)
		elif chk != "":
			wt += "\n" + chk
			wc = Color(0.95, 0.5, 0.45)
		_label(grid, wt, 11, wc, 80.0)
		var opt := OptionButton.new()
		opt.custom_minimum_size = Vector2(210, 0)
		for i in ids.size():
			opt.add_item(game.room_label(ids[i]), i)
		var idx := maxi(0, ids.find(plan.get(s, "off")))
		if not s.can_be_assigned():
			idx = 0
			opt.disabled = true
		opt.select(idx)
		plan[s] = ids[idx]
		opt.item_selected.connect(_on_plan_changed.bind(s, ids))
		grid.add_child(opt)
	_label(v, "덜 일한 사람부터, 근로시간을 어기지 않게 미리 골라 둔 배치입니다. 반응실 → 정제실 → 물류 창고가 한 줄이고, 창고에 사람이 없으면 정제실 출구(%d드럼)에서 막힙니다." % Game.OUTBOX_CAP, 11, Color(0.6, 0.6, 0.66))


func _tab_facility(v: VBoxContainer) -> void:
	_label(v, "전력 %d / %d  ·  예산 %d" % [game.power_used(), game.power_cap, roundi(game.money)], 14)
	_label(v, "조명 (괄호는 전력). 근무 중에는 맵에서 방이나 복도를 눌러도 됩니다.", 11, Color(0.6, 0.6, 0.66))
	var lf := _flow(v)
	for id in game.rooms:
		var r: Dictionary = game.rooms[id]
		var cb := CheckBox.new()
		cb.text = "%s (%d)" % [r["name"], game.room_power(id)]
		cb.button_pressed = r["light"]
		cb.toggled.connect(_on_light_toggled.bind("room", id))
		lf.add_child(cb)
	for z in Game.ZONE_ORDER:
		if not game.zones[z]["open"]:
			continue
		var cb2 := CheckBox.new()
		cb2.text = "%s 복도 (1)" % Game.ZONE_DEF[z]["name"]
		cb2.button_pressed = game.zones[z]["corr_light"]
		cb2.toggled.connect(_on_light_toggled.bind("corr", z))
		lf.add_child(cb2)

	_label(v, "빈 칸에 방 짓기", 13, Color(0.75, 0.85, 1.0))
	var any := false
	for id in game.slot_order:
		var opts := game.build_options(id)
		if opts.is_empty():
			continue
		any = true
		var row := _flow(v)
		var sl: Dictionary = game.slots[id]
		_label(row, "%s · %s 칸" % [Game.ZONE_DEF[sl["zone"]]["name"], Game.SIZE_NAME[sl["size"]]], 12, Color(0.8, 0.8, 0.85), 150.0)
		for t in opts:
			var d: Dictionary = Game.ROOM_TYPES[t]
			_button(row, "%s %d" % [d["name"], d["cost"]], _on_build.bind(id, t), game.money < d["cost"])
	if not any:
		_label(v, "지을 수 있는 빈 칸이 없습니다.", 11, Color(0.6, 0.6, 0.66))
	_label(v, "생산동 휴게실은 가깝지만 시끄럽고(휴식 효과 75%) 반응실 사고가 번집니다. 사무동은 조용하지만 복도를 걸어가야 합니다.", 11, Color(0.6, 0.6, 0.66))

	_label(v, "설치물", 13, Color(0.75, 0.85, 1.0))
	var inst := _flow(v)
	for id in game.rooms:
		var r: Dictionary = game.rooms[id]
		if r["type"] == "purify" or r["type"] == "smoke":
			continue
		if not r["cctv"]:
			_button(inst, "%s CCTV %d" % [r["name"], Game.CCTV_COST], _on_install.bind(id, "cctv"), game.money < Game.CCTV_COST)
		if not r["em"]:
			_button(inst, "%s 비상 조명 %d" % [r["name"], Game.EM_COST], _on_install.bind(id, "em"), game.money < Game.EM_COST)
	_label(v, "CCTV: 불이 꺼져도 인원수가 보이고, 명단과 안 맞으면 경고합니다 (전력 1). 비상 조명: 정전에도 꺼지지 않습니다.", 11, Color(0.6, 0.6, 0.66))

	_label(v, "폐쇄 구역 탈환", 13, Color(0.75, 0.85, 1.0))
	for z in Game.RECLAIM_ORDER:
		if game.zones[z]["open"]:
			continue
		var d: Dictionary = Game.RECLAIM[z]
		var why := game.reclaim_block(z)
		var row2 := _flow(v)
		_button(row2, "%s 탈환 %d" % [Game.ZONE_DEF[z]["name"], d["cost"]], _on_reclaim.bind(z), why != "")
		_label(row2, "정화 %d교대(2명 이상) → 빈 칸, 정원 +%d, 전력 +%d%s" % [d["shifts"], d["to"], Game.POWER_PER_ZONE,
			"  ·  " + why if why != "" else ""], 11, Color(0.7, 0.7, 0.76), 520.0)


func _tab_people(v: VBoxContainer) -> void:
	_label(v, "정원 %d명 · 현재 %d명" % [game.to_cap, game.alive_count()], 14)
	if not game.applicants.is_empty():
		_label(v, "지원자", 13, Color(0.75, 0.85, 1.0))
		for a in game.applicants:
			_button(v, "채용: %s %d세 · 운영 %d 관찰 %d 정신 %d 체력 %d %s" % [a.name, a.age, a.operation, a.observation,
				a.mind, a.stamina, ("(" + ", ".join(a.traits) + ")") if a.traits.size() > 0 else ""], _on_hire.bind(a), game.alive_count() >= game.to_cap)
	elif game.ad_arrive_day >= 0:
		_label(v, "공고 진행 중: %d일차에 지원자 도착" % game.ad_arrive_day, 12)
	else:
		_button(v, "일반 공고 내기 (−%d, %d일 뒤 지원자)" % [Game.AD_COST, Game.AD_DELAY_DAYS], _on_post_ad, not game.can_post_ad())

	_label(v, "오염 관리", 13, Color(0.75, 0.85, 1.0))
	_label(v, "오염은 25마다 한 단계: 다크서클(피로 회복 느림) → 초점 없는 눈(동료 스트레스) → 검은 무늬(그 방에 '한 명 더'가 잘 생김) → 변이(100, 같은 방 동료가 물든다). 쉬는 교대마다 −%.1f, 휴직 중 −%d." % [Game.OFF_DECAY, Game.LEAVE_DECAY], 11, Color(0.6, 0.6, 0.66))
	var any_c := false
	for s in game.staff:
		if s.gone() or s.contam_stage() == 0:
			continue
		any_c = true
		var row := _flow(v)
		var col := Color(0.95, 0.55, 0.45) if s.contam_stage() >= 3 else Color(0.85, 0.8, 0.6)
		_label(row, "%s · 오염 %d · %s" % [s.name, roundi(s.contamination), s.contam_name()], 12, col, 230.0)
		if s.leave_days > 0:
			_label(row, "휴직 중 (%d일 남음)" % s.leave_days, 12, Color(0.7, 0.7, 0.76), 150.0)
		else:
			_button(row, "휴직 %d일 (오염 50 이상)" % Game.LEAVE_DAYS, _on_contam.bind(s, "leave"), not game.can_leave(s))
		_button(row, "정밀 검진·정화 %d (오염 −%d)" % [Game.PURIFY_STAFF_COST, Game.PURIFY_STAFF_AMOUNT], _on_contam.bind(s, "purify"), not game.can_purify_staff(s))
	if not any_c:
		_label(v, "오염된 직원이 없습니다.", 11, Color(0.6, 0.6, 0.66))

	_label(v, "근로시간 장부", 13, Color(0.75, 0.85, 1.0))
	_label(v, "주 %d교대까지 기본, %d교대째는 연장수당, 그 뒤는 위반. 교대 사이에는 한 교대를 쉬어야 합니다. 야간 근무자는 수당 1.5배. 위반은 장부에 남아 감사 때 지적이 됩니다." % [
		Game.LEGAL_SHIFTS, Game.LEGAL_SHIFTS + 1], 11, Color(0.6, 0.6, 0.66))
	var grid := GridContainer.new()
	grid.columns = 3
	grid.add_theme_constant_override("h_separation", 16)
	v.add_child(grid)
	for s in game.staff:
		if s.gone():
			continue
		_label(grid, s.name, 12, Color(0.9, 0.9, 0.93), 90.0)
		_label(grid, "이번 주 %d / %d교대%s" % [s.shifts_week, game.legal_limit(), " · 직전 교대 근무" if s.worked_prev else ""], 12, Color(0.8, 0.8, 0.85), 220.0)
		_label(grid, "출근 기록이 두 번씩 찍혀 있다" if s.has_trait("심부름") else "", 11, Color(0.95, 0.70, 0.50), 260.0)
	_label(v, "이번 주 위반 %d건 · 아직 감사를 받지 않은 위반 %d건" % [game.viol_week, game.violations_open], 12,
		Color(0.95, 0.55, 0.45) if game.violations_open > 0 else Color(0.7, 0.7, 0.76))
	_button(v, "특별연장근로 인가 신청 (이번 주 한도 +1, 본사 신뢰도 −%d)" % Game.EXT_TRUST_COST, _on_prep.bind("ext"), not game.can_extend())


func _tab_hq(v: VBoxContainer) -> void:
	var hist: PackedStringArray = []
	for p in game.week_history:
		hist.append("%+d" % roundi(p))
	_label(v, "본사 신뢰도 %d  ·  예산 %d  ·  이번 주 영업 손익 %+d (투자비 제외)" % [game.trust, roundi(game.money), roundi(game.week_profit())], 14)
	_label(v, "주간 손익: %s" % (" / ".join(hist) if hist.size() > 0 else "아직 없음"), 12, Color(0.7, 0.7, 0.76))
	_label(v, "감사", 13, Color(0.75, 0.85, 1.0))
	_label(v, game.audit_status_text(), 13, Color(0.95, 0.70, 0.50))
	_label(v, "적자 주 다음에는 본사 감사가 옵니다. 정기감사는 4주차와 8주차, 2주 전에 감사관 유형과 함께 예고됩니다. 감사에서 떨어지면 2주 뒤 특별감사, 거기서도 떨어지면 공장장이 교체됩니다.", 11, Color(0.6, 0.6, 0.66))
	if game.in_audit():
		var info: Dictionary = Game.AUDITOR_INFO[game.auditor["type"]]
		_label(v, "%s\n주로 보는 것: %s\n통과 보상: %s" % [info["arrive"], info["watch"], info["reward"]], 12)
	var prep := _flow(v)
	if not game.hidden.is_empty() and not game.in_audit():
		_button(prep, "숨긴 사고 %d건 정정 보고 (지적 1건 확정)" % game.hidden.size(), _on_prep.bind("correct"))
	elif not game.hidden.is_empty():
		_label(v, "숨긴 사고 %d건. 감사 중에는 정정할 수 없습니다." % game.hidden.size(), 12)
	if game.audit_stage == 2:
		if not game.special_known:
			_button(prep, "관찰 기록 %d → 특별감사관 성향 알아내기" % Game.RECORDS_FOR_INTEL, _on_prep.bind("intel"), game.records < Game.RECORDS_FOR_INTEL)
		if not game.intel_doc:
			_button(prep, "관찰 기록 %d → 감사 대응 자료 (지적 −1)" % Game.RECORDS_FOR_INTEL, _on_prep.bind("docs"), game.records < Game.RECORDS_FOR_INTEL)
		_label(v, "안전 운전 기록(가동률 80%% 이하, 무사고): %s" % ("유지 중 (지적 −1)" if game.grace_safe else "깨짐"), 12)
	_label(v, "안전 의무 이행 기록", 13, Color(0.75, 0.85, 1.0))
	_label(v, "위험성평가: 이번 주 %s · 지난주 %s   (QC실에 한 교대 배치하면 그 주에 인정. 위험도 예보도 자세해집니다)" % [
		"실시" if game.risk_week else "미실시", "실시" if game.risk_last_week else "미실시"], 12)
	_label(v, "실종이나 부상 2명 이상은 중대사고입니다. 위험성평가, 근로시간 준수, 작업중지 요구 수용 기록이 있으면 면책되고, 없으면 특별감사로 직행합니다.", 11, Color(0.6, 0.6, 0.66))


func _update_plan_labels() -> void:
	risk_label.text = "오늘 %s 위험도 예보: %s" % ["밤" if game.is_night() else "낮", game.risk_forecast(plan)]
	var w := game.plan_warnings(plan)
	warn_label.text = "" if w.is_empty() else "근로시간 위반이 되는 배치: " + ", ".join(w)


func _on_plan_changed(index: int, s: Staff, ids: Array) -> void:
	plan[s] = ids[index]
	_update_plan_labels()


func _on_light_toggled(on: bool, what: String, id: String) -> void:
	var ok := game.set_room_light(id, on) if what == "room" else game.set_corr_light(id, on)
	if not ok:
		_log("전력이 모자라다. 다른 조명을 먼저 꺼야 한다. (%d/%d)" % [game.power_used(), game.power_limit()])
	_open_meeting(true)


func _on_build(slot_id: String, t: String) -> void:
	if game.build(slot_id, t):
		sfx.play("build")
	_open_meeting(true)


func _on_install(id: String, what: String) -> void:
	game.install(id, what)
	_open_meeting(true)


func _on_reclaim(z: String) -> void:
	if game.start_reclaim(z):
		sfx.play("build")
	_open_meeting(true)


func _on_contam(s: Staff, what: String) -> void:
	if what == "leave":
		game.start_leave(s)
	else:
		game.purify_staff(s)
	_open_meeting(true)


func _on_hire(a: Staff) -> void:
	game.hire(a)
	_rebuild_staff_list()
	_open_meeting(true)


func _on_post_ad() -> void:
	game.post_ad()
	_open_meeting(true)


func _on_prep(what: String) -> void:
	match what:
		"correct":
			game.correct_reports()
		"intel":
			game.buy_intel()
		"docs":
			game.submit_docs()
		"ext":
			game.request_extension()
	_open_meeting(true)


func _start_shift() -> void:
	var any := false
	for s in game.staff:
		if plan.has(s) and s.can_be_assigned() and not s.gone():
			s.room = plan[s]
		else:
			s.room = "off"
		if s.room != "off":
			any = true
	showing_alert = false
	alert_queue.clear()
	game.start_shift()
	modal.visible = false
	phase = "shift"
	acc = 0.0
	sfx.set_night(game.is_night())
	if not any:
		# 아무도 출근하지 않은 교대는 바로 넘긴다
		_log("[color=gray]아무도 출근하지 않았다. 교대를 건너뛴다.[/color]")
		for h in Game.HOURS_PER_SHIFT:
			game.tick_hour()
		# 빈 교대 중에 뜬 알림(쉬는 직원의 붕괴 등)은 기록에만 남긴다
		alert_queue.clear()
		showing_alert = false
		_finish_shift()
		return
	paused = false


# ---------------------------------------------------------------- 근무 중

func _move_staff(delta: float) -> void:
	var counts := {}
	for s in game.staff:
		if not s.on_map():
			continue
		var idx: int = counts.get(s.room, 0)
		counts[s.room] = idx + 1
		var target: Vector2 = world.slot_position(s.room, idx)
		var px := 130.0 if s.transit > 0 else 320.0
		s.pos = s.pos.move_toward(target, px * delta * maxf(1.0, float(speed)))


func _on_speed_button(v: int) -> void:
	if v == 0:
		if phase == "shift" and not modal.visible:
			paused = not paused
	else:
		_set_speed(v)


func _set_speed(v: int) -> void:
	speed = v
	if phase == "shift" and not modal.visible:
		paused = false


func _on_staff_pressed(s: Staff) -> void:
	game.selected = null if game.selected == s else s


func _on_room_clicked(id: String) -> void:
	var s: Staff = game.selected
	if s != null and phase == "shift" and s.on_map() and s.can_work() and Game.ASSIGN_TYPES.has(game.rooms[id]["type"]):
		if s.room != id and s.transit == 0:
			var far := game.zone_of(s.room) != game.zone_of(id)
			game.order_move(s, id)
			_log("직접 지시: %s → %s%s" % [s.name, game.rooms[id]["name"], " (복도로 이동 중)" if far else ""])
		return
	var r: Dictionary = game.rooms[id]
	if game.set_room_light(id, not r["light"]):
		sfx.play("light")
		_log("%s 조명을 %s. (전력 %d/%d)" % [r["name"], "켰다" if r["light"] else "껐다", game.power_used(), game.power_limit()])
	else:
		sfx.play("bad", -12.0)
		_log("[color=#e98]전력이 모자라 %s 조명을 켤 수 없다. 다른 불을 먼저 꺼야 한다.[/color]" % r["name"])


func _on_corridor_clicked(z: String) -> void:
	var nm: String = Game.ZONE_DEF[z]["name"]
	if game.set_corr_light(z, not game.zones[z]["corr_light"]):
		sfx.play("light")
		_log("%s 복도 조명을 %s. (전력 %d/%d)" % [nm, "켰다" if game.zones[z]["corr_light"] else "껐다", game.power_used(), game.power_limit()])
	else:
		_log("[color=#e98]전력이 모자라 %s 복도 조명을 켤 수 없다.[/color]" % nm)


func _on_rate_changed(v: float) -> void:
	game.reactor_rate = int(v)


# ---------------------------------------------------------------- 보고서와 결산

func _finish_shift() -> void:
	phase = "report"
	paused = true
	sfx.set_night(false)
	var text := game.end_shift()
	_clear_modal()
	_modal_rich(text)
	if game.shift_incidents.is_empty():
		_button(modal_box, "결재", _file.bind(false))
	else:
		var row := _flow(modal_box)
		_button(row, "사실대로 기재하고 결재", _file.bind(false))
		_button(row, "사고를 빼고 결재", _file.bind(true))
		var serious := false
		for inc in game.shift_incidents:
			if inc["serious"]:
				serious = true
		if serious:
			_label(modal_box, "중대사고입니다. 보고하면 바로 조사가 옵니다. 빼고 결재하면 '무단 이탈' 등으로 처리되지만, 감사에서 들키면 훨씬 무겁습니다.", 12, Color(0.95, 0.55, 0.45))
		elif game.in_audit():
			_modal_label("감사 중에는 보고한 사고가 바로 지적이 됩니다. 숨긴 사고를 들키면 가중 처벌.", 12)
	modal.visible = true


func _file(conceal: bool) -> void:
	sfx.play("stamp", -4.0)
	var inv := game.file_report(conceal)
	if inv != "":
		_clear_modal()
		_modal_rich(inv)
		_button(modal_box, "확인", _after_file)
		modal.visible = true
		return
	_after_file()


func _after_file() -> void:
	if game.week_ended:
		var text := game.settle_week()
		sfx.play("week" if game.week_history[-1] >= 0.0 and not game.replaced else "bad")
		_clear_modal()
		_modal_rich(text)
		_button(modal_box, "확인", _after_report)
		modal.visible = true
		return
	_after_report()


func _after_report() -> void:
	modal.visible = false
	if game.is_defeat():
		match game.defeat_reason():
			"교체":
				_end_screen("공장장이 교체됩니다.", "특별감사 결과를 받은 본사는 새 공장장을 보내기로 했습니다. 박 씨는 오늘도 정문을 지키고 있습니다.")
			"해임":
				_end_screen("본사가 신뢰를 거뒀습니다.", "해임 통보가 팩스로 왔습니다. 박 씨: \"이번 분도 오래는 못 계셨네.\"")
			_:
				_end_screen("공장에 일할 사람이 남지 않았습니다.", "본사는 새 공장장을 보내기로 했습니다. 박 씨는 오늘도 정문을 지키고 있습니다.")
		return
	if game.is_victory():
		var extra := ""
		if game.audit_stage != 0:
			extra = " 다만 본사의 감시는 아직 끝나지 않았습니다. (%s)" % game.audit_status_text()
		_end_screen("8주를 버텼습니다.", "의산공장은 아직 돌아가고 있습니다. 박 씨: \"이번 공장장님은 오래 계시겠네요.\"" + extra)
		return
	if not game.is_night():
		_log("[color=gray]%d일차 아침.[/color]" % game.day)
	_begin_meeting()


func _end_screen(title: String, body: String) -> void:
	phase = "over"
	paused = true
	_clear_modal()
	_modal_label(title, 22)
	_modal_label(body, 15)
	var hist: PackedStringArray = []
	for p in game.week_history:
		hist.append("%+d" % roundi(p))
	var opened := 0
	for z in Game.RECLAIM_ORDER:
		if game.zones[z]["open"]:
			opened += 1
	_modal_label("예산 %d · 본사 신뢰도 %d · 주간 손익 %s" % [game.money, game.trust, " / ".join(hist)], 13)
	_modal_label("감사 %d회 (정기 %d회) · 특별감사 %d회 · 근로시간 위반 %d건 · 중대사고 조사 %d회 · 완벽한 차단 %d회" % [
		game.audits_held, game.regular_held, game.specials_held, game.violations_total, game.serious_count, game.perfect_count], 13)
	_modal_label("출하 %d드럼 · 탈환 구역 %d곳 · 관찰 기록 %d · 남은 직원 %d명" % [game.total_shipped, opened, game.records, game.alive_count()], 13)
	_button(modal_box, "다시 시작", func() -> void: get_tree().reload_current_scene())
	modal.visible = true


# ---------------------------------------------------------------- 알림

func _on_alert(text: String, kind: String) -> void:
	var color := "white"
	match kind:
		"pressure", "break", "cctv":
			color = "#e98"
		"vanish":
			color = "#f55"
		"awaken", "perfect":
			color = "#fd6"
		"mutate":
			color = "#c8f"
	_log("[color=%s]%s[/color]" % [color, text])
	match kind:
		"pressure":
			sfx.play("pressure")
		"perfect":
			sfx.play("perfect", -4.0)
		"vanish", "mutate":
			sfx.play("vanish", -4.0)
		"awaken":
			sfx.play("awaken")
		"break":
			sfx.play("break")
		_:
			if text.begins_with("정전"):
				sfx.play("outage", -4.0)
			elif text.contains("손상"):
				sfx.play("fail", -4.0)
			else:
				sfx.play("alert")
	if phase != "shift":
		return  # 회의 중 사건(「심부름」 거절 등)은 기록만 남긴다
	paused = true
	alert_queue.append([text, kind])
	if not showing_alert:
		_show_next_alert()


func _show_next_alert() -> void:
	if alert_queue.is_empty():
		showing_alert = false
		modal.visible = false
		_resume_after_alert()
		return
	showing_alert = true
	var item: Array = alert_queue.pop_front()
	var text: String = item[0]
	var kind: String = item[1]
	_clear_modal()
	var l := _modal_label(text, 16)
	if kind == "perfect" or kind == "awaken":
		l.add_theme_color_override("font_color", Color(1.0, 0.85, 0.4))
	if kind == "pressure":
		_modal_label("포스트잇: \"숫자 믿지 마.\"", 13)
		_button(modal_box, "계기대로 감압하게 둔다 (성공 %d%%)" % roundi(game.pressure_odds(false) * 100.0), _close_pressure.bind(0))
		_button(modal_box, "직접 지시: 반대쪽 밸브를 열어라 (성공 %d%%, 틀리면 부상 위험 2배)" % roundi(game.pressure_odds(true) * 100.0), _close_pressure.bind(1))
		var st := game.stopwork_staff()
		if st != null:
			_button(modal_box, "%s의 작업중지 요구를 받아들인다 (사고 없음, 이번 교대 반응실 정지)" % st.name, _close_pressure.bind(2))
			_modal_label("%s: \"세워야 합니다. 수칙대로요.\" 요구를 무시했다가 사고가 나면 기록에 남습니다." % st.name, 12)
	elif kind == "cctv":
		_modal_label("직원을 골라 다른 방으로 보내면 '그것'과 떨어뜨릴 수 있습니다.", 12)
		_button(modal_box, "확인", _show_next_alert)
	else:
		_button(modal_box, "확인", _show_next_alert)
	modal.visible = true


func _close_pressure(choice: int) -> void:
	game.resolve_pressure(choice)  # 결과 알림이 큐에 추가될 수 있다
	_show_next_alert()


func _resume_after_alert() -> void:
	if phase != "shift":
		return
	if game.hour >= Game.HOURS_PER_SHIFT:
		_finish_shift()
	else:
		paused = false


func _log(text: String) -> void:
	if sfx != null:
		if text.contains("트럭이"):
			sfx.play("truck", -10.0)
		elif text.contains("압력이 안정됐다") or text.contains("작업을 멈췄다"):
			sfx.play("ok")
	if log_box != null:
		log_box.append_text(text + "\n")


# ---------------------------------------------------------------- HUD

func _refresh_hud() -> void:
	top_label.text = "  %d일차(%d주) %s %s · 예산 %d (주 %+d) · 신뢰도 %d · 전력 %d/%d · 재고 %.0f · 기록 %d · 정원 %d/%d · %s" % [
		mini(game.day, Game.WIN_DAY), mini(game.week(), 8), game.shift, game.clock_text(),
		roundi(game.money), roundi(game.week_profit()), game.trust, game.power_used(), game.power_limit(),
		game.stock, game.records, game.alive_count(), game.to_cap, _phase_name()]
	top_label2.text = "  " + game.audit_status_text()
	speed_label.text = "멈춤" if paused else "%dx" % speed
	rate_label.text = "%d%%%s" % [game.reactor_rate, " (80% 초과: 역압)" if game.reactor_rate > 80 else ""]
	for s in game.staff:
		if not staff_labels.has(s):
			continue
		var l: Label = staff_labels[s]
		var where := "휴무"
		if s.on_map():
			where = game.rooms[s.room]["name"]
		var sel := "▶ " if game.selected == s else ""
		l.text = "%s%s · %s · 주 %d교대\n%s" % [sel, s.name, where, s.shifts_week, s.short_status()]


func _phase_name() -> String:
	match phase:
		"errand":
			return "박 씨의 부탁"
		"meeting":
			return "교대 회의"
		"shift":
			return "근무 중"
		"report":
			return "보고서"
	return "종료"
