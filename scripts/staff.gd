class_name Staff
extends RefCounted
## 직원 한 명의 데이터와 상태.

var name: String = ""
var age: int = 30
var operation: float = 50.0    # 공정 운영
var observation: float = 50.0  # 관찰력
var mind: float = 50.0         # 정신력
var stamina: float = 50.0      # 체력
var traits: PackedStringArray = PackedStringArray()
var intro: String = ""

var stress: float = 0.0         # 0~150
var fatigue: float = 0.0        # 0~100
var contamination: float = 0.0  # 0~100

## 배치된 방: "off"(휴무), "reactor", "refine", "warehouse", "rest"
var room: String = "off"
var pos: Vector2 = Vector2.ZERO
## 정상, 각성, 붕괴, 실신, 부상, 실종, 퇴사
var state: String = "정상"
var down_days: int = 0
var consecutive_nights: int = 0
var notes: PackedStringArray = PackedStringArray()

## 한 명 더 단계: 0 없음, 1 이상 행동 시작(경고), 2 이상 행동 심화(다시 방치되면 실종 위험)
var omen: int = 0
var clean_nights: int = 0       # 노출 없이 지난 밤 (2번이면 omen이 한 단계 내려간다)
var exposed_tonight: bool = false
var omen_start: int = 0         # 교대 시작 때의 단계. 실종은 이미 경고를 두 번 받은 뒤에만
var away: int = 0               # 「심부름」 등으로 자리를 비운 교대 수

## 근로시간 장부
var shifts_week: int = 0        # 이번 주에 찍힌 교대 수
var worked_prev: bool = false   # 바로 앞 교대에 일했는가 (연속 교대 금지)
var worked_now: bool = false

## 복도 이동: transit > 0이면 방에 없고 복도를 걷는 중
var leave_days: int = 0         # 휴직 남은 날 (오염 관리)
var transit: int = 0
var transit_hops: int = 0
var transit_path: Array = []
var smoked: bool = false

var snap_stress: float = 0.0
var snap_fatigue: float = 0.0
var snap_contam: float = 0.0


func _init(n: String, a: int, op: float, ob: float, mi: float, st: float, tr: PackedStringArray, intro_text: String) -> void:
	name = n
	age = a
	operation = op
	observation = ob
	mind = mi
	stamina = st
	traits = tr
	intro = intro_text


func has_trait(t: String) -> bool:
	return traits.has(t)


func gone() -> bool:
	return state == "실종" or state == "퇴사"


func can_work() -> bool:
	return not gone() and state != "붕괴" and state != "실신" and state != "부상"


func can_be_assigned() -> bool:
	return not gone() and state != "실신" and state != "부상" and away == 0 and leave_days == 0


## 오염 단계: 0 정상, 1 다크서클, 2 초점 없는 눈, 3 검은 무늬, 4 변이
func contam_stage() -> int:
	if contamination >= 100.0:
		return 4
	return int(contamination / 25.0)


func contam_name() -> String:
	return ["정상", "다크서클", "초점 없는 눈", "검은 무늬", "변이"][contam_stage()]


func on_map() -> bool:
	return room != "off" and not gone()


func stress_mult() -> float:
	var m: float = 1.5 - mind / 100.0
	if has_trait("고집불통"):
		m *= 0.85
	return m


## 스트레스 증감. 양수는 정신력으로 보정하고, 각성 중에는 오르지 않는다.
func add_stress(amount: float) -> void:
	if amount > 0.0:
		if state == "각성":
			return
		amount *= stress_mult()
	stress = clampf(stress + amount, 0.0, 150.0)


func add_fatigue(amount: float) -> void:
	if amount > 0.0 and has_trait("꾸준함"):
		amount *= 0.8  # 꾸준함: 덜 지친다
	fatigue = clampf(fatigue + amount, 0.0, 100.0)


func add_contamination(amount: float) -> void:
	contamination = clampf(contamination + amount, 0.0, 100.0)


func take_snapshot() -> void:
	snap_stress = stress
	snap_fatigue = fatigue
	snap_contam = contamination
	notes.clear()


func color() -> Color:
	match state:
		"각성":
			return Color(0.95, 0.80, 0.30)
		"붕괴":
			return Color(0.85, 0.30, 0.30)
	# 평상시 색. "한 명 더"도 이 색으로 그려진다.
	return Color(0.42, 0.52, 0.72)


func short_status() -> String:
	if gone():
		return state
	var s := "스트레스 %d · 피로 %d · 오염 %d" % [roundi(stress), roundi(fatigue), roundi(contamination)]
	if state != "정상":
		s += " · [%s]" % state
	if contam_stage() > 0:
		s += " · " + contam_name()
	if leave_days > 0:
		s += " · 휴직 %d일" % leave_days
	if omen > 0:
		s += " · ⚠이상 행동"
	if away > 0:
		s += " · 자리 비움"
	if transit > 0:
		s += " · 이동 중"
	return s
