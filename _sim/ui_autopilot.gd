extends SceneTree
## 실제 화면을 띄워 버튼·체크 상자·맵을 무작위로 눌러 가며 데모 끝까지 진행한다 (오류 검사용).
## 사용: godot --headless --script res://_sim/ui_autopilot.gd -- seed=1

var main: Node
var frames := 0
var presses := 0
var orders := 0
var rng := RandomNumberGenerator.new()
var last_day := 0


func _init() -> void:
	rng.randomize()
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=")
		if kv.size() == 2 and kv[0] == "seed":
			rng.seed = int(kv[1])
	Engine.time_scale = 40.0
	Engine.max_fps = 0
	main = load("res://main.tscn").instantiate()
	root.add_child.call_deferred(main)
	process_frame.connect(_tick)


func _tick() -> void:
	frames += 1
	if main == null or not main.is_inside_tree() or main.sim == null:
		return
	if main.speed != 6:
		main.speed = 6
	if main.phase == "over":
		_finish("데모 끝까지 진행")
		return
	if frames > 400000:
		_finish("프레임 한도")
		return
	if main.sim.day != last_day:
		last_day = main.sim.day
		print("%d일차 · 예산 %d · 기록 %d" % [main.sim.day, main.sim.money, main.sim.records])
	if frames % 3 != 0:
		return
	if main.paused and not main.modal.visible and main.phase == "shift":
		main.paused = false
	if main.modal.visible:
		var buttons: Array = []
		_collect(main.modal_box, buttons)
		var plain := buttons.filter(func(b: BaseButton) -> bool: return not (b is CheckBox) and not (b is OptionButton))
		var checks := buttons.filter(func(b: BaseButton) -> bool: return b is CheckBox)
		if checks.size() > 0 and rng.randf() < 0.05:
			var c: CheckBox = checks[rng.randi() % checks.size()]
			c.button_pressed = not c.button_pressed
			presses += 1
			return
		if plain.size() > 0:
			var b: BaseButton = plain[rng.randi() % plain.size()]
			b.emit_signal("pressed")
			presses += 1
		return
	# 근무 중: 가끔 사람을 골라 아무 방으로 보낸다
	if main.phase == "shift" and rng.randf() < 0.01:
		var ppl: Array = main.sim.on_site_people()
		if ppl.size() > 0:
			var p: Person = ppl[rng.randi() % ppl.size()]
			main._on_person_pressed(p)
			var ids: Array = Defs.ROOMS.keys()
			main._on_room_clicked(ids[rng.randi() % ids.size()])
			orders += 1
	if main.phase == "shift" and rng.randf() < 0.002:
		var off: Array = main.sim.callable_people()
		if off.size() > 0:
			main._on_person_pressed(off[0])


func _collect(n: Node, out: Array) -> void:
	for c in n.get_children():
		if c is BaseButton and not c.disabled and c.is_visible_in_tree():
			out.append(c)
		_collect(c, out)


func _finish(why: String) -> void:
	var s: Sim = main.sim
	print("%s: %d일차, 프레임 %d, 누름 %d, 직접 지시 %d" % [why, s.day, frames, presses, orders])
	print("예산 %d · 적발 %d · 역압 막음 %d / 사고 %d · 호출 %d · 붙잡기 %d · 실종 %d" % [s.money, s.stats["detected"], s.stats["pressure_ok"], s.stats["pressure_bad"], s.stats["calls"], s.stats["holds"], s.stats["vanished"]])
	quit()
