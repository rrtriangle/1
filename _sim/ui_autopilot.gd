extends SceneTree
## 실제 화면을 띄워 버튼·선택 상자·체크 상자·맵을 무작위로 눌러 가며 끝까지 진행한다 (오류 검사용).
## 사용: godot --headless --script res://_sim/ui_autopilot.gd -- seed=1 wild=1
##   wild=0이면 회의에서 기본 배치 그대로 "근무 시작"만 누른다.

var main: Node
var frames := 0
var presses := 0
var wild := 1
var reports := 0
var last_phase := ""
var meeting_budget := 0
var rng := RandomNumberGenerator.new()


func _init() -> void:
	rng.randomize()
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=")
		if kv.size() == 2 and kv[0] == "seed":
			rng.seed = int(kv[1])
		if kv.size() == 2 and kv[0] == "wild":
			wild = int(kv[1])
	Engine.time_scale = 30.0
	main = load("res://main.tscn").instantiate()
	root.add_child.call_deferred(main)
	process_frame.connect(_tick)


func _tick() -> void:
	frames += 1
	if main == null or not main.is_inside_tree() or main.game == null:
		return
	var g: Game = main.game
	if main.phase != last_phase:
		if main.phase == "report":
			reports += 1
		if main.phase == "meeting":
			meeting_budget = rng.randi_range(0, 4) if wild == 1 else 0
		last_phase = main.phase
	if main.phase == "shift" and main.speed != 3:
		main._set_speed(3)
	if main.phase == "over":
		var texts := []
		for c in main.modal_box.get_children():
			if c is Label:
				texts.append(c.text)
		print("종료: ", " / ".join(texts))
		print("프레임 %d, 입력 %d회, 보고서 %d장, 결산 %d회, 직원 %d명, 방 %d개" % [frames, presses, reports, g.week_history.size(), g.staff.size(), g.rooms.size()])
		quit()
		return
	if frames > 60000:
		print("시간 초과: phase=%s day=%d hour=%d paused=%s modal=%s" % [main.phase, g.day, g.hour, main.paused, main.modal.visible])
		var bs: Array = []
		var os2: Array = []
		_collect(main.modal_box, bs, os2)
		var names: Array = []
		for b in bs:
			names.append(b.text)
		print("  버튼: ", names, " 알림 대기 ", main.alert_queue.size(), " showing ", main.showing_alert, " 역압 대기 ", g.pending_pressure)
		quit(1)
		return
	if frames % 3 != 0:
		return
	if not main.modal.visible:
		# 근무 중: 가끔 직원을 고르고 방·복도를 누른다
		if wild == 1 and main.phase == "shift" and rng.randf() < 0.05:
			presses += 1
			if rng.randf() < 0.5:
				main._on_staff_pressed(g.staff[rng.randi() % g.staff.size()])
			elif rng.randf() < 0.7:
				var ids: Array = g.rooms.keys()
				main._on_room_clicked(ids[rng.randi() % ids.size()])
			else:
				main._on_corridor_clicked(Game.ZONE_ORDER[rng.randi() % Game.ZONE_ORDER.size()])
		return
	var buttons: Array = []
	var others: Array = []
	_collect(main.modal_box, buttons, others)
	if buttons.is_empty():
		return
	presses += 1
	if main.phase == "meeting":
		if meeting_budget > 0:
			meeting_budget -= 1
			if not others.is_empty() and rng.randf() < 0.4:
				var o: Control = others[rng.randi() % others.size()]
				if o is OptionButton:
					var i: int = rng.randi() % int(o.item_count)
					o.select(i)
					o.item_selected.emit(i)
				else:
					o.button_pressed = not o.button_pressed
			else:
				buttons[rng.randi() % buttons.size()].pressed.emit()
			return
		buttons[-1].pressed.emit()  # 근무 시작
		return
	var b: Button = buttons[rng.randi() % buttons.size()] if wild == 1 else buttons[0]
	b.pressed.emit()


func _collect(n: Node, buttons: Array, others: Array) -> void:
	for c in n.get_children():
		if c.is_queued_for_deletion():
			continue
		if c is OptionButton or c is CheckBox:
			if not c.disabled:
				others.append(c)
		elif c is Button:
			if not c.disabled:
				buttons.append(c)
		else:
			_collect(c, buttons, others)
