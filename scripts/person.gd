class_name Person
extends RefCounted
## 직원 한 명. 능력치·상태와 지금 어디 있는지.

var name: String
var role: String          # 반장, 조원, 연구원, 정비원, 관리
var crew: int             # 1~3, 상근직은 0
var age: int
var skill: float          # 숙련: 생산 속도, 설비 대응 성공률
var observe: float        # 관찰: 이상현상 적발, 관찰 기록
var nerve: float          # 담력: 스트레스를 덜 받는다
var traits: PackedStringArray

var fatigue := 10.0
var stress := 10.0
var contam := 0.0

var post := ""            # 이번 교대에 맡은 자리
var room := ""            # 지금 있는 방. 걷는 중이면 ""
var pos := Vector2.ZERO
var path: Array[Vector2] = []
var dest := ""
var on_arrive: Callable   # 도착하면 부른다
var on_site := false      # 공장 안에 있다
var on_duty := false      # 이번 교대 근무자
var leaving := false      # 교대해 줄 사람을 기다렸다가 퇴근한다
var busy_until := -1      # 이 시각(절대 분)까지 자리를 비운다 (쉬기·조사·불 켜기)
var busy_what := ""
var arrive_at := -1       # 호출: 정문에 도착하는 시각(절대 분)
var injured_until_day := 0
var broken := false       # 스트레스 100: 이번 교대는 일을 못 한다
var ignored := 0          # 한 명 더를 무시하라는 지시를 받은 횟수
var errand := false       # 「심부름」을 다녀왔다
var gone := false
var extra_shifts := 0     # 대타로 더 일한 횟수
var patrol_i := 0
var patrol_wait := 0
var contam_k := 1.0       # 보호구가 있으면 오염이 덜 쌓인다


func _init(d: Dictionary) -> void:
	name = d["name"]
	role = d["role"]
	crew = d["crew"]
	age = d.get("age", 30)
	skill = d["skill"]
	observe = d["observe"]
	nerve = d["nerve"]
	traits = PackedStringArray(d["traits"])


func has_trait(t: String) -> bool:
	return traits.has(t)


func stage() -> int:
	return clampi(int(contam / 25.0), 0, 4)


func eff_skill() -> float:
	return skill * (0.8 if fatigue >= Defs.FATIGUE_SLOW else 1.0)


func eff_observe() -> float:
	return observe * (0.8 if fatigue >= Defs.FATIGUE_SLOW else 1.0)


func walking() -> bool:
	return not path.is_empty()


func is_staff() -> bool:
	return crew == 0


## 지금 자기 자리에서 일하고 있는가
func working(now: int) -> bool:
	return on_site and not walking() and not broken and busy_until <= now and room == post_room()


func post_room() -> String:
	match post:
		"assist":
			return "refine"
		"patrol":
			return room  # 순찰은 지금 있는 방에서 잠깐 머문다
	return post


func add_stress(v: float) -> void:
	var k := 1.5 - nerve / 100.0
	if has_trait("신입") or has_trait("예민함"):
		k += 0.2
	if has_trait("고집불통"):
		k -= 0.2
	stress = clampf(stress + v * k if v > 0 else stress + v, 0.0, 150.0)


func add_contam(v: float) -> void:
	contam = clampf(contam + (v * contam_k if v > 0 else v), 0.0, 100.0)


func status_text(now: int) -> String:
	if gone:
		return "실종"
	if not on_site:
		if arrive_at >= 0:
			return "호출 받고 오는 중"
		return "집"
	if broken:
		return "붕괴 (일 못 함)"
	if walking():
		return "→ " + Defs.ROOMS[dest]["name"] if dest != "" else "이동 중"
	if busy_until > now:
		return busy_what
	if leaving:
		return "교대 기다림"
	if post == "patrol":
		return "순찰 중 (" + Defs.ROOMS[room]["name"] + ")"
	return Defs.POST_NAMES.get(post, post)
