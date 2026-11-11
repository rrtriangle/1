class_name Sfx
extends Node
## 효과음. 파일 없이 시작할 때 코드로 합성한다 (임시 소리).

const RATE := 22050
var sounds: Dictionary = {}
var players: Array[AudioStreamPlayer] = []
var hum: AudioStreamPlayer
var enabled: bool = true


func _ready() -> void:
	for i in 6:
		var p := AudioStreamPlayer.new()
		add_child(p)
		players.append(p)
	sounds["click"] = _make([[0.0, 0.04, 900.0, 900.0, "sq", 0.25]])
	sounds["light"] = _make([[0.0, 0.03, 1400.0, 1400.0, "sq", 0.2], [0.05, 0.05, 700.0, 700.0, "sq", 0.15]])
	sounds["alert"] = _make([[0.0, 0.12, 660.0, 660.0, "sq", 0.3], [0.16, 0.12, 660.0, 660.0, "sq", 0.3]])
	sounds["pressure"] = _make([[0.0, 0.25, 400.0, 900.0, "saw", 0.35], [0.28, 0.25, 400.0, 900.0, "saw", 0.35], [0.56, 0.3, 400.0, 1000.0, "saw", 0.35]])
	sounds["ok"] = _make([[0.0, 0.1, 520.0, 520.0, "sin", 0.4], [0.1, 0.18, 780.0, 780.0, "sin", 0.4]])
	sounds["perfect"] = _make([[0.0, 0.09, 660.0, 660.0, "sin", 0.45], [0.09, 0.09, 880.0, 880.0, "sin", 0.45], [0.18, 0.09, 1100.0, 1100.0, "sin", 0.45], [0.27, 0.4, 1320.0, 1320.0, "sin", 0.5]])
	sounds["fail"] = _make([[0.0, 0.5, 220.0, 70.0, "saw", 0.45], [0.0, 0.35, 0.0, 0.0, "noise", 0.35]])
	sounds["outage"] = _make([[0.0, 0.7, 300.0, 40.0, "sin", 0.5], [0.0, 0.08, 0.0, 0.0, "noise", 0.4]])
	sounds["stamp"] = _make([[0.0, 0.09, 110.0, 60.0, "sin", 0.7], [0.0, 0.05, 0.0, 0.0, "noise", 0.5]])
	sounds["vanish"] = _make([[0.0, 1.4, 90.0, 55.0, "sin", 0.5], [0.0, 1.4, 93.0, 58.0, "sin", 0.4], [0.3, 0.9, 1900.0, 1700.0, "sin", 0.08]])
	sounds["awaken"] = _make([[0.0, 0.5, 330.0, 660.0, "sin", 0.4], [0.2, 0.5, 495.0, 990.0, "sin", 0.3]])
	sounds["break"] = _make([[0.0, 0.3, 500.0, 180.0, "sq", 0.3], [0.3, 0.3, 400.0, 140.0, "sq", 0.3]])
	sounds["truck"] = _make([[0.0, 0.6, 70.0, 95.0, "saw", 0.35], [0.0, 0.6, 0.0, 0.0, "noise", 0.12]])
	sounds["build"] = _make([[0.0, 0.05, 0.0, 0.0, "noise", 0.5], [0.12, 0.05, 0.0, 0.0, "noise", 0.5], [0.24, 0.15, 440.0, 660.0, "sin", 0.35]])
	sounds["week"] = _make([[0.0, 0.18, 392.0, 392.0, "sin", 0.4], [0.18, 0.18, 523.0, 523.0, "sin", 0.4], [0.36, 0.4, 659.0, 659.0, "sin", 0.4]])
	sounds["bad"] = _make([[0.0, 0.3, 300.0, 300.0, "sin", 0.4], [0.3, 0.5, 200.0, 200.0, "sin", 0.4]])
	# 밤에 깔리는 낮은 기계음 (2초 루프)
	hum = AudioStreamPlayer.new()
	var h := _make([[0.0, 2.0, 55.0, 55.0, "sin", 0.5], [0.0, 2.0, 110.0, 110.0, "sin", 0.15], [0.0, 2.0, 82.5, 82.5, "sin", 0.12]], false)
	h.loop_mode = AudioStreamWAV.LOOP_FORWARD
	h.loop_end = int(2.0 * RATE)
	hum.stream = h
	hum.volume_db = -22.0
	add_child(hum)


## parts: [시작(초), 길이(초), 시작 주파수, 끝 주파수, 파형, 크기]
func _make(parts: Array, fade: bool = true) -> AudioStreamWAV:
	var total := 0.0
	for p in parts:
		total = maxf(total, p[0] + p[1])
	var n := int(total * RATE)
	var buf := PackedFloat32Array()
	buf.resize(n)
	var seed_v := 12345
	for p in parts:
		var start := int(float(p[0]) * RATE)
		var len := int(float(p[1]) * RATE)
		var phase := 0.0
		for i in len:
			var t := float(i) / maxf(1.0, float(len))
			var f: float = lerpf(p[2], p[3], t)
			phase += f / RATE
			var v := 0.0
			match p[4]:
				"sin":
					v = sin(phase * TAU)
				"sq":
					v = 1.0 if fmod(phase, 1.0) < 0.5 else -1.0
				"saw":
					v = fmod(phase, 1.0) * 2.0 - 1.0
				"noise":
					seed_v = (seed_v * 1103515245 + 12345) & 0x7fffffff
					v = float(seed_v % 2000) / 1000.0 - 1.0
			var env := 1.0
			if fade:
				env = minf(1.0, t * 30.0) * (1.0 - t)
			if start + i < n:
				buf[start + i] += v * env * float(p[5])
	var bytes := PackedByteArray()
	bytes.resize(n * 2)
	for i in n:
		bytes.encode_s16(i * 2, int(clampf(buf[i], -1.0, 1.0) * 30000.0))
	var w := AudioStreamWAV.new()
	w.format = AudioStreamWAV.FORMAT_16_BITS
	w.mix_rate = RATE
	w.stereo = false
	w.data = bytes
	return w


func play(name: String, db: float = -8.0) -> void:
	if not enabled or not sounds.has(name):
		return
	for p in players:
		if not p.playing:
			p.stream = sounds[name]
			p.volume_db = db
			p.play()
			return
	players[0].stream = sounds[name]
	players[0].play()


func set_night(on: bool) -> void:
	if on and enabled:
		if not hum.playing:
			hum.play()
	else:
		hum.stop()
