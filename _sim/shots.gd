extends SceneTree
## 화면 확인용 스크린샷. 가상 디스플레이에서 실행한다.
## 사용: xvfb-run godot --script res://_sim/shots.gd -- out=/경로

var main: Node
var out := "user://"
var step := 0
var wait := 0
var shots := 0


func _init() -> void:
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=")
		if kv.size() == 2 and kv[0] == "out":
			out = kv[1]
	main = load("res://main.tscn").instantiate()
	root.add_child.call_deferred(main)
	process_frame.connect(_tick)


func _shot(name: String) -> void:
	var img := root.get_viewport().get_texture().get_image()
	img.save_png("%s/%02d_%s.png" % [out, shots, name])
	shots += 1
	print("shot ", name)


func _press_first_choice() -> void:
	var bs: Array = []
	_collect(main.modal_box, bs)
	for b in bs:
		if not (b is CheckBox) and not (b is OptionButton):
			b.emit_signal("pressed")
			return


func _collect(n: Node, o: Array) -> void:
	for c in n.get_children():
		if c is BaseButton and not c.disabled:
			o.append(c)
		_collect(c, o)


func _tick() -> void:
	if main == null or not main.is_inside_tree():
		return
	wait += 1
	if wait < 20:
		return
	wait = 0
	match step:
		0:
			_shot("meeting_day1")
			main._start_shift()
			step = 1
		1:
			# 첫 안내 무전
			if main.radio_open:
				_shot("radio_tutorial")
				step = 2
		2:
			# 안내를 몇 개 넘긴다
			if main.radio_open:
				_press_first_choice()
				if main.current_radio.get("room", "") == "dock":
					_shot("radio_dock")
			elif main.sim.t > 100:
				step = 3
		3:
			Engine.time_scale = 1.0
			if main.radio_open:
				_press_first_choice()
			elif main.sim.t >= 130:
				_shot("map_day1")
				step = 4
			else:
				Engine.time_scale = 20.0
		4:
			# 1일 밤의 역압까지 빨리 간다
			Engine.time_scale = 30.0
			if main.modal.visible and not main.radio_open:
				if main.phase == "meeting" and main.sim.is_night():
					Engine.time_scale = 1.0
					_shot("meeting_night1")
					main._start_shift()
				else:
					_press_first_choice()
			elif main.radio_open:
				if main.current_radio.get("kind", "") == "pressure":
					Engine.time_scale = 1.0
					_shot("radio_pressure")
					step = 5
				else:
					_press_first_choice()
		5:
			_press_first_choice()
			step = 6
		6:
			if main.radio_open or main.modal.visible:
				Engine.time_scale = 1.0
				_shot("result")
				_press_first_choice()
			step = 7
		7:
			Engine.time_scale = 1.0
			_shot("map_night1")
			quit()
